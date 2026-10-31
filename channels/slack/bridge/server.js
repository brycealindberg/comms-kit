// server.js

// Sentry error monitoring
const Sentry = require('@sentry/node');
if (process.env.SENTRY_DSN_VPS) {
  Sentry.init({
    dsn: process.env.SENTRY_DSN_VPS,
    environment: process.env.SENTRY_ENVIRONMENT || 'development',
    serverName: 'slack-bridge',
    tracesSampleRate: 0,
    beforeSend(event) {
      const url = event.request?.url || '';
      if (url.includes('/health')) return null;
      return event;
    },
  });
}

const express = require('express');
const crypto = require('crypto');
const { TokenStore } = require('./lib/token-store');
const { MessageBuffer } = require('./lib/message-buffer');
const { WorkspacePoller } = require('./lib/poller');
const { validateToken, refreshToken } = require('./lib/slack-client');
const { inferExtension, isTranscribableFile, whisperTranscribe } = require('./lib/transcription');
const { buildPostMessagePayload } = require('./lib/send-payload');
const { evaluateTokenUpload, validateTokenForTeam } = require('./lib/team-guard');
const { attemptRefresh: refreshWorkspaceToken, checkWorkspace } = require('./lib/token-refresh');
const {
  admitSend,
  buildHealthPayload,
  createBoundedMap,
  createConcurrencyGate,
  createSlidingWindowLimiter,
  envPositiveInt,
  isDetailedHealthAuthorized,
  runLimitedWork,
  splitTranscriptionWork,
} = require('./lib/security-limits');

const app = express();
// Global JSON body parser (100 KB default) for every route EXCEPT /files/upload. This
// middleware runs before route handlers, so a route-level limit alone would be too late —
// the 100 KB cap would reject a large (base64) upload body first. Skip that one path here
// and give it its own 25 MB parser on the route (see POST /files/upload).
const jsonParser = express.json();
const uploadJsonParser = express.json({ limit: '25mb' });
app.use((req, res, next) => (req.path === '/files/upload' ? next() : jsonParser(req, res, next)));

const PORT = process.env.PORT || 3101;
const ADMIN_SECRET = process.env.ADMIN_SECRET || '';
const BRIDGE_API_KEY = process.env.BRIDGE_API_KEY || '';
const OPENAI_API_KEY = process.env.OPENAI_API_KEY || '';
const BIND_HOST = process.env.BIND_HOST || '127.0.0.1';

// Core state
const tokenStore = new TokenStore();
const messageBuffer = new MessageBuffer({ maxPerChannel: 500, maxTotal: 10000 });
const pollers = new Map(); // teamId -> WorkspacePoller
const startTime = Date.now();
const TRANSCRIPTION_CACHE_MAX_ENTRIES = envPositiveInt('SLACK_TRANSCRIPTION_CACHE_MAX_ENTRIES', 500);
const transcriptionCache = createBoundedMap(TRANSCRIPTION_CACHE_MAX_ENTRIES); // fileId -> transcription result
const OPENAI_AUDIO_LIMIT_BYTES = 25 * 1024 * 1024; // Whisper request limit
const TRANSCRIPTION_MAX_NEW_PER_REQUEST = envPositiveInt('SLACK_TRANSCRIPTION_MAX_NEW_PER_REQUEST', 10);
const TRANSCRIPTION_RATE_LIMIT = envPositiveInt('SLACK_TRANSCRIPTION_RATE_LIMIT_PER_MINUTE', 10);
const TRANSCRIPTION_CONCURRENCY = envPositiveInt('SLACK_TRANSCRIPTION_CONCURRENCY', 1);
const transcriptionLimiter = createSlidingWindowLimiter({
  max: TRANSCRIPTION_RATE_LIMIT,
  windowMs: 60 * 1000,
});
const transcriptionGate = createConcurrencyGate(TRANSCRIPTION_CONCURRENCY);

function coerceCount(value) {
  if (value === undefined || value === null || value === '') return null;
  const count = Number(value);
  return Number.isFinite(count) ? Math.max(0, count) : null;
}

function channelReadState(chInfo = {}, msgs = []) {
  const nativeUnread = coerceCount(
    chInfo.unread_count_display ?? chInfo.unread_count ?? chInfo.unreadCountDisplay ?? chInfo.unreadCount,
  );
  if (nativeUnread !== null) {
    return { unreadCount: nativeUnread, readState: nativeUnread > 0 ? 'unread' : 'read', readSource: 'native' };
  }

  const lastReadValue = chInfo.last_read;
  if (lastReadValue !== undefined && lastReadValue !== null && lastReadValue !== '') {
    const lastRead = Number(lastReadValue);
    if (Number.isFinite(lastRead)) {
      const pending = msgs.filter((msg) => !msg.is_from_me && Number(msg.ts) > lastRead).length;
      return { unreadCount: pending, readState: pending > 0 ? 'unread' : 'read', readSource: 'last_read' };
    }
  }
  return { unreadCount: null, readState: 'unknown', readSource: 'none' };
}


// Rate limiter for /send and /files/upload — global sliding window, max 20 sends per minute
const sendLimiter = createSlidingWindowLimiter({ max: 20, windowMs: 60 * 1000 });


// --- Middleware ---

/**
 * Constant-time string compare with length guard.
 * timingSafeEqual throws on unequal-length buffers, so we pre-check length
 * and still run a dummy compare on mismatch to keep timing roughly even.
 */
function safeCompare(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string') return false;
  const aBuf = Buffer.from(a);
  const bBuf = Buffer.from(b);
  if (aBuf.length !== bBuf.length) {
    // Dummy compare to avoid short-circuit timing leak
    crypto.timingSafeEqual(aBuf, aBuf);
    return false;
  }
  return crypto.timingSafeEqual(aBuf, bBuf);
}

// Baseline API-key auth — required on every non-health route.
function apiKeyAuth(req, res, next) {
  if (req.path === '/health') return next();
  const provided = req.headers['x-api-key'];
  if (!provided || !safeCompare(String(provided), BRIDGE_API_KEY)) {
    return res.status(401).json({ error: 'Unauthorized' });
  }
  next();
}

app.use(apiKeyAuth);

function adminAuth(req, res, next) {
  if (!ADMIN_SECRET) {
    return res.status(500).json({ error: 'ADMIN_SECRET not configured' });
  }
  const secret = req.headers['x-admin-secret'];
  if (!secret || !safeCompare(String(secret), ADMIN_SECRET)) {
    return res.status(401).json({ error: 'Unauthorized' });
  }
  next();
}

// --- Helpers ---

/**
 * Attempt to refresh a workspace's token using client.boot. See
 * lib/token-refresh.js — refuses to store a refreshed token whose team
 * doesn't match teamId.
 * Returns { success, token, cookie, error }
 */
function attemptRefresh(teamId, options = {}) {
  return refreshWorkspaceToken(teamId, { store: tokenStore, refreshFn: refreshToken, ...options });
}

// One token check against the session stored now. A re-pair during the check ends it as
// 'superseded', and the check then leaves the new session alone (lib/token-refresh.js).
function checkStoredWorkspace(teamId) {
  return checkWorkspace(teamId, {
    store: tokenStore,
    refreshFn: refreshToken,
    validateFn: (token, cookie, tid) => validateTokenForTeam(tid, token, cookie, validateToken),
  });
}

function isPollerActive(teamId) {
  const ws = tokenStore.getWorkspace(teamId);
  return !!ws && ws.status === 'connected' && !!pollers.get(teamId)?.running;
}

// Act on one check outcome. 'superseded': /admin/tokens already started the new poller.
function applyCheckOutcome(teamId, outcome, wasActive) {
  const ws = tokenStore.getWorkspace(teamId);
  if (!ws) return;
  if (outcome === 'valid') {
    // Self-heal: a workspace that got stopped by a transient failure (e.g. a
    // rate-limit burst wrongly marked needs_reauth) still has a valid token.
    // Restart its poller instead of leaving it dark until a manual re-pair.
    if (!wasActive) {
      startPoller(teamId);
      console.log(`[${ws.name}] Re-validated, poller restarted`);
    }
  } else if (outcome === 'refreshed') {
    startPoller(teamId);
    console.log(`[${ws.name}] Auto-refreshed successfully, poller restarted`);
  } else if (outcome === 'needs_reauth') {
    console.error(`[${ws.name}] Auto-refresh failed, marking needs_reauth`);
    if (pollers.has(teamId)) pollers.get(teamId).stop();
  }
}

function startPoller(teamId) {
  const ws = tokenStore.getWorkspace(teamId);
  if (!ws || ws.status === 'needs_reauth') return;

  // Stop existing poller if any
  if (pollers.has(teamId)) {
    pollers.get(teamId).stop();
  }

  const poller = new WorkspacePoller({
    teamId,
    name: ws.name,
    token: ws.token,
    cookie: ws.cookie,
    buffer: messageBuffer,
    onStatusChange: (tid, status) => {
      tokenStore.updateStatus(tid, status);
      console.log(`[${ws.name}] Status changed to: ${status}`);
    },
    refreshFn: async (tid) => {
      const result = await attemptRefresh(tid, { maxRetries: 1 });
      if (result.success) {
        // Also update the poller reference so future calls use fresh token
        return result;
      }
      return result;
    },
  });

  pollers.set(teamId, poller);
  poller.start().catch(e => console.error(`Failed to start poller for ${ws.name}: ${e.message}`));
}

function transcriptionCacheKey(teamId, fileId) {
  return `${teamId}:${fileId}`;
}

function getCachedTranscription(teamId, fileId) {
  if (!teamId || !fileId) return null;
  return transcriptionCache.get(transcriptionCacheKey(teamId, fileId)) || null;
}

function decorateAttachment(attachment, teamId) {
  const isVoiceNote = isTranscribableFile(attachment);
  const cached = getCachedTranscription(teamId, attachment.id);
  return {
    ...attachment,
    is_voice_note: isVoiceNote,
    transcription: cached?.text || null,
  };
}

function decorateMessage(message) {
  const attachments = (message.attachments || []).map(attachment => decorateAttachment(attachment, message.team_id));
  const voiceAttachment = attachments.find(a => a.is_voice_note);
  const transcribedAttachment = attachments.find(a => a.is_voice_note && a.transcription);

  return {
    ...message,
    attachments,
    isVoiceNote: !!voiceAttachment,
    transcription: transcribedAttachment?.transcription || null,
  };
}

function pollerCandidates(workspaceFilter) {
  const candidates = [];
  for (const [teamId, poller] of pollers) {
    if (workspaceFilter && teamId !== workspaceFilter && poller.name !== workspaceFilter) continue;
    candidates.push(poller);
  }
  return candidates;
}

function findPollerForChannel(channelId, workspaceFilter) {
  for (const poller of pollerCandidates(workspaceFilter)) {
    if (poller.channelMap.has(channelId)) return poller;
  }
  return null;
}

async function findSlackFile(fileId, workspaceFilter) {
  for (const poller of pollerCandidates(workspaceFilter)) {
    try {
      const result = await poller.client.files.info({ file: fileId, team_id: poller.teamId });
      if (result.file) return { poller, file: result.file };
    } catch (e) {
      if (e.data?.error === 'file_not_found') continue;
      throw e;
    }
  }
  return null;
}

async function downloadSlackFile(poller, file) {
  const downloadUrl = file.url_private_download || file.url_private;
  if (!downloadUrl) {
    const err = new Error('No download URL available for this file');
    err.statusCode = 404;
    throw err;
  }

  const ws = tokenStore.getWorkspace(poller.teamId);
  if (!ws?.token || !ws?.cookie) {
    const err = new Error('Workspace credentials unavailable');
    err.statusCode = 500;
    throw err;
  }

  const fileRes = await fetch(downloadUrl, {
    headers: {
      Authorization: `Bearer ${ws.token}`,
      Cookie: `d=${ws.cookie}`,
    },
  });

  if (!fileRes.ok) {
    const err = new Error(`Slack returned ${fileRes.status}`);
    err.statusCode = fileRes.status;
    throw err;
  }

  return Buffer.from(await fileRes.arrayBuffer());
}

function assertWithinWhisperLimit(file, size) {
  const effectiveSize = Number(size || file.size || 0);
  if (effectiveSize > OPENAI_AUDIO_LIMIT_BYTES) {
    const err = new Error(`File exceeds Whisper 25MB limit (${effectiveSize} bytes)`);
    err.statusCode = 413;
    throw err;
  }
}

function cacheTranscription(teamId, file, result, extra = {}) {
  const entry = {
    team_id: teamId,
    fileId: file.id,
    text: result.text,
    language: result.language,
    duration: result.duration,
    name: file.name || null,
    mimetype: file.mimetype || file.type || null,
    extension: inferExtension(file) || null,
    size: file.size || null,
    user: file.user || null,
    created: file.created || null,
    ...extra,
  };
  transcriptionCache.set(transcriptionCacheKey(teamId, file.id), entry);
  return entry;
}

// --- Routes ---

// Health
app.get('/health', (req, res) => {
  const workspaces = [];
  for (const ws of tokenStore.listWorkspaces()) {
    const poller = pollers.get(ws.team_id);
    workspaces.push(poller ? poller.getStatus() : {
      team_id: ws.team_id,
      name: ws.name,
      status: ws.status,
      channels: 0,
      buffered_messages: 0,
    });
  }

  const base = {
    uptime_seconds: Math.floor((Date.now() - startTime) / 1000),
    total_messages: messageBuffer.totalMessages(),
    workspaces,
  };
  const detailed = isDetailedHealthAuthorized(req.headers['x-api-key'], BRIDGE_API_KEY, safeCompare);
  res.json(buildHealthPayload(base, { detailed }));
});

// List workspaces
app.get('/workspaces', (req, res) => {
  const workspaces = tokenStore.listWorkspaces().map(ws => {
    const poller = pollers.get(ws.team_id);
    return {
      team_id: ws.team_id,
      name: ws.name,
      status: poller?.running ? 'connected' : ws.status,
      last_valid: ws.last_valid,
    };
  });
  res.json(workspaces);
});

// List channels (optionally filtered by workspace)
app.get('/channels', (req, res) => {
  const workspaceFilter = req.query.workspace;
  const channels = [];

  for (const [teamId, poller] of pollers) {
    if (workspaceFilter && teamId !== workspaceFilter && poller.name !== workspaceFilter) continue;

    for (const [chId, chInfo] of poller.channelMap) {
      const msgs = messageBuffer.getMessages(chId);
      const readState = channelReadState(chInfo, msgs);
      channels.push({
        ...chInfo,
        workspace: poller.name,
        team_id: teamId,
        message_count: msgs.length,
        last_activity: msgs.length > 0 ? msgs[msgs.length - 1].ts : null,
        // A late reply into a full buffer can leave both of the fields
        // above unchanged (it evicts the oldest to stay at the cap, and
        // is not the newest ts either) -- this changes on every real
        // insertion regardless, so a poller that falls back to it never
        // misses one (Codex review 2026-10-04, issue 1).
        revision: messageBuffer.getRevision(chId),
        unread_count: readState.unreadCount,
        unread_count_display: readState.unreadCount,
        read_state: readState.readState,
        read_source: readState.readSource,
      });
    }
  }

  res.json(channels);
});

// Get messages for a channel
app.get('/channels/:channelId/messages', (req, res) => {
  const { channelId } = req.params;
  const since = req.query.since || '0';
  const limit = parseInt(req.query.limit) || 200;

  const msgs = messageBuffer.getMessages(channelId, { since, limit });
  res.json(msgs.map(decorateMessage));
});

// List contacts / users (optionally filtered by workspace)
app.get('/contacts', (req, res) => {
  const workspaceFilter = req.query.workspace;
  const nameFilter = (req.query.name || '').toLowerCase();
  const contacts = [];

  for (const [teamId, poller] of pollers) {
    if (workspaceFilter && teamId !== workspaceFilter && poller.name !== workspaceFilter) continue;

    for (const [userId, userInfo] of poller.userCache) {
      if (userInfo.is_bot) continue;
      if (nameFilter && !userInfo.real_name.toLowerCase().includes(nameFilter) && !userInfo.name.toLowerCase().includes(nameFilter)) continue;
      contacts.push({
        ...userInfo,
        workspace: poller.name,
        team_id: teamId,
      });
    }
  }

  res.json(contacts);
});

// Send a message (rate-limited: 20/min globally)
app.post('/send', async (req, res) => {
  const { channel, text, workspace, thread_ts } = req.body;
  if (!channel || !text) {
    return res.status(400).json({ error: 'channel and text required' });
  }

  const admitted = admitSend({ pollers, channel, workspace, limiter: sendLimiter });
  if (!admitted.poller) {
    return res.status(admitted.status).json({ error: admitted.error });
  }
  const { poller } = admitted;

  try {
    const payload = buildPostMessagePayload({ channel, text, teamId: poller.teamId, threadTs: thread_ts });
    const result = await poller.client.chat.postMessage(payload);
    res.json({ ok: true, ts: result.ts, channel: result.channel });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// Upload a local file into a Slack channel/DM (rate-limited: 20/min globally, shared with /send).
// Body: { channel, workspace?, filename, initial_comment?, data_base64 } — the file arrives
// base64-encoded because JSON can't carry raw bytes. uploadV2 handles Slack's external-upload flow.
app.post('/files/upload', uploadJsonParser, async (req, res) => {
  const { channel, workspace, filename, initial_comment, data_base64 } = req.body;
  // data_base64 must be a string: Buffer.from(nonString, 'base64') throws a synchronous
  // TypeError, which in this async handler would become an unhandled rejection (Express 4
  // doesn't catch async throws) — so reject non-strings here rather than let it reach Buffer.from.
  if (!channel || !filename || typeof data_base64 !== 'string' || !data_base64) {
    return res.status(400).json({ error: 'channel, filename, and data_base64 (string) required' });
  }

  // Same gate and same limit as /send.
  const admitted = admitSend({ pollers, channel, workspace, limiter: sendLimiter });
  if (!admitted.poller) {
    return res.status(admitted.status).json({ error: admitted.error });
  }
  const { poller } = admitted;

  // Buffer.from(..., 'base64') never throws — it silently drops invalid chars — so guard on
  // an empty decode rather than a try/catch, which would be dead code here.
  const buffer = Buffer.from(data_base64, 'base64');
  if (buffer.length === 0) {
    return res.status(400).json({ error: 'data_base64 decoded to empty' });
  }

  try {
    const result = await poller.client.files.uploadV2({
      channel_id: channel,
      file: buffer,
      filename,
      initial_comment,
      team_id: poller.teamId,
    });
    // uploadV2 returns `files` as an array of per-batch results ({ ok, files: [...] }),
    // not a flat file list. Flatten to the actual file objects so callers read files[].id
    // directly (tolerate a future SDK version that already returns flat file objects).
    const files = (result.files || []).flatMap((entry) =>
      entry && Array.isArray(entry.files) ? entry.files : [entry],
    );
    res.json({ ok: true, files });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// Get file info or download a file by Slack file ID
app.get('/files/:fileId', async (req, res) => {
  const { fileId } = req.params;
  const workspaceFilter = req.query.workspace;
  const download = req.query.download !== 'false'; // default: stream the file

  // Find the right workspace poller
  let targetPoller;
  if (workspaceFilter) {
    for (const [tid, p] of pollers) {
      if (tid === workspaceFilter || p.name === workspaceFilter) {
        targetPoller = p;
        break;
      }
    }
  } else {
    // Try each workspace until one has the file
    targetPoller = pollers.values().next().value;
  }

  if (!targetPoller) {
    return res.status(404).json({ error: 'Workspace not found or no pollers running' });
  }

  try {
    const result = await targetPoller.client.files.info({ file: fileId, team_id: targetPoller.teamId });
    const file = result.file;

    if (!download) {
      return res.json({
        id: file.id,
        name: file.name,
        mimetype: file.mimetype,
        size: file.size,
        url_private: file.url_private,
        url_private_download: file.url_private_download,
        created: file.created,
        user: file.user,
      });
    }

    // Proxy the file download
    const downloadUrl = file.url_private_download || file.url_private;
    if (!downloadUrl) {
      return res.status(404).json({ error: 'No download URL available for this file' });
    }

    // Fetch from Slack with auth headers (same token + cookie the client uses)
    const ws = tokenStore.getWorkspace(targetPoller.teamId);
    const fileRes = await fetch(downloadUrl, {
      headers: {
        'Authorization': `Bearer ${ws.token}`,
        'Cookie': `d=${ws.cookie}`,
      },
    });

    if (!fileRes.ok) {
      return res.status(fileRes.status).json({ error: `Slack returned ${fileRes.status}` });
    }

    res.set('Content-Type', file.mimetype || 'application/octet-stream');
    res.set('Content-Disposition', `attachment; filename="${file.name}"`);
    if (file.size) res.set('Content-Length', String(file.size));

    // Stream the response body to the client
    const { Readable } = require('stream');
    const readable = Readable.fromWeb(fileRes.body);
    readable.pipe(res);
  } catch (e) {
    if (e.data?.error === 'file_not_found') {
      return res.status(404).json({ error: 'File not found' });
    }
    res.status(500).json({ error: e.message });
  }
});

// Transcribe a Slack audio/clip file by file ID
app.get('/files/:fileId/transcribe', async (req, res) => {
  if (!OPENAI_API_KEY) {
    return res.status(500).json({ error: 'OPENAI_API_KEY not configured on bridge' });
  }

  const { fileId } = req.params;
  const workspaceFilter = req.query.workspace;

  try {
    const found = await findSlackFile(fileId, workspaceFilter);
    if (!found) {
      return res.status(404).json({ error: 'File not found' });
    }

    const { poller, file } = found;
    if (!isTranscribableFile(file)) {
      return res.status(400).json({ error: 'File is not a transcribable Slack audio or clip file' });
    }

    const cached = getCachedTranscription(poller.teamId, file.id);
    if (cached) {
      return res.json({ ...cached, cached: true });
    }

    const entry = await runLimitedWork({ limiter: transcriptionLimiter, gate: transcriptionGate }, async () => {
      assertWithinWhisperLimit(file, file.size);
      const buffer = await downloadSlackFile(poller, file);
      assertWithinWhisperLimit(file, buffer.length);
      console.log(`Transcribing Slack file ${fileId} (${buffer.length} bytes, ${file.mimetype || file.filetype || 'unknown'})...`);
      const result = await whisperTranscribe(buffer, file, { apiKey: OPENAI_API_KEY });
      return cacheTranscription(poller.teamId, file, result, { workspace: poller.name });
    });

    res.json({ ...entry, cached: false });
  } catch (e) {
    console.error(`Transcription failed for Slack file ${fileId}: ${e.message}`);
    res.status(e.statusCode || 500).json({ error: `Transcription failed: ${e.message}` });
  }
});

// Bulk transcribe all Slack audio/clip files buffered for a channel
app.get('/channels/:channelId/transcribe', async (req, res) => {
  if (!OPENAI_API_KEY) {
    return res.status(500).json({ error: 'OPENAI_API_KEY not configured on bridge' });
  }

  const { channelId } = req.params;
  const workspaceFilter = req.query.workspace;
  const targetPoller = findPollerForChannel(channelId, workspaceFilter);

  if (!targetPoller) {
    return res.status(404).json({ error: 'Channel not found in any connected workspace' });
  }

  const msgs = messageBuffer.getMessages(channelId);
  const voiceFiles = [];
  for (const msg of msgs) {
    for (const attachment of msg.attachments || []) {
      if (isTranscribableFile(attachment)) {
        voiceFiles.push({ msg, file: attachment });
      }
    }
  }

  if (voiceFiles.length === 0) {
    return res.json({ channelId, transcriptions: [], count: 0 });
  }

  const split = splitTranscriptionWork(voiceFiles, {
    maxNew: Math.min(
      Number.parseInt(String(req.query.max_new || TRANSCRIPTION_MAX_NEW_PER_REQUEST), 10) || TRANSCRIPTION_MAX_NEW_PER_REQUEST,
      TRANSCRIPTION_MAX_NEW_PER_REQUEST
    ),
    isCached: ({ file }) => !!getCachedTranscription(targetPoller.teamId, file.id),
  });

  const results = [];
  for (const { msg, file: attachment } of split.cached) {
    const cached = getCachedTranscription(targetPoller.teamId, attachment.id);
    results.push({ ...cached, messageTs: msg.ts, cached: true });
  }

  for (const { msg, file: attachment } of split.toProcess) {
    try {
      const entry = await runLimitedWork({ limiter: transcriptionLimiter, gate: transcriptionGate }, async () => {
        const file = {
          ...attachment,
          id: attachment.id,
          mimetype: attachment.mimetype || attachment.type,
        };
        assertWithinWhisperLimit(file, file.size);
        const buffer = await downloadSlackFile(targetPoller, file);
        assertWithinWhisperLimit(file, buffer.length);
        const result = await whisperTranscribe(buffer, file, { apiKey: OPENAI_API_KEY });
        return cacheTranscription(targetPoller.teamId, file, result, {
          workspace: targetPoller.name,
          channelId,
          messageTs: msg.ts,
          sender: msg.sender_name || null,
        });
      });
      results.push({ ...entry, cached: false });
    } catch (e) {
      results.push({
        fileId: attachment.id,
        messageTs: msg.ts,
        error: e.message,
      });
    }
  }

  for (const { msg, file: attachment } of split.skipped) {
    results.push({
      fileId: attachment.id,
      messageTs: msg.ts,
      skipped: true,
      error: `not transcribed in this request; max_new=${split.maxNew}`,
    });
  }

  res.json({
    channelId,
    transcriptions: results,
    count: results.length,
    total: split.total,
    skipped: split.skipped.length,
    max_new: split.maxNew,
  });
});

// List files from a channel (queries Slack API directly)
app.get('/channels/:channelId/files', async (req, res) => {
  const { channelId } = req.params;
  const workspaceFilter = req.query.workspace;

  // Find the right workspace poller
  let targetPoller;
  for (const [tid, p] of pollers) {
    if (workspaceFilter && tid !== workspaceFilter && p.name !== workspaceFilter) continue;
    if (p.channelMap.has(channelId)) {
      targetPoller = p;
      break;
    }
  }

  if (!targetPoller) {
    return res.status(404).json({ error: 'Channel not found in any connected workspace' });
  }

  try {
    const result = await targetPoller.client.files.list({
      channel: channelId,
      count: parseInt(req.query.limit) || 100,
      team_id: targetPoller.teamId,
    });

    const files = (result.files || []).map(f => ({
      id: f.id,
      name: f.name,
      mimetype: f.mimetype,
      size: f.size,
      created: f.created,
      user: f.user,
      title: f.title,
      filetype: f.filetype,
      url_private: f.url_private,
      url_private_download: f.url_private_download,
    }));

    res.json({ files, paging: result.paging });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// --- Admin Routes ---

// Push tokens from auth CLI
app.post('/admin/tokens', adminAuth, async (req, res) => {
  const { workspace, team_id, token, cookie } = req.body;

  // Validates the token AND refuses to accept it if its own team (per
  // auth.test) doesn't match the team_id it's being saved under — see
  // lib/team-guard.js.
  const outcome = await evaluateTokenUpload({ workspace, team_id, token, cookie, validateFn: validateToken });
  if (!outcome.ok) {
    return res.status(outcome.status).json({ error: outcome.error });
  }

  // Store and start polling
  tokenStore.setWorkspace(team_id, outcome.workspace);
  startPoller(team_id);

  res.json(outcome.response);
});

// Force token refresh (legacy endpoint — validates only)
app.post('/admin/refresh', adminAuth, async (req, res) => {
  const results = [];
  for (const ws of tokenStore.listWorkspaces()) {
    const validation = await validateTokenForTeam(ws.team_id, ws.token, ws.cookie, validateToken);
    if (validation.valid) {
      tokenStore.updateStatus(ws.team_id, 'connected');
      results.push({ team_id: ws.team_id, name: ws.name, status: 'valid' });
    } else {
      tokenStore.updateStatus(ws.team_id, 'needs_reauth');
      if (pollers.has(ws.team_id)) pollers.get(ws.team_id).stop();
      results.push({ team_id: ws.team_id, name: ws.name, status: 'needs_reauth', error: validation.error });
    }
  }
  res.json(results);
});

// Force token refresh via client.boot (auto-heals expired tokens)
app.post('/admin/refresh-tokens', adminAuth, async (req, res) => {
  const targetTeamId = req.body.team_id;
  const workspaces = tokenStore.listWorkspaces().filter(
    ws => !targetTeamId || ws.team_id === targetTeamId
  );

  const results = [];
  for (const { team_id, name } of workspaces) {
    const wasActive = isPollerActive(team_id);
    const outcome = await checkStoredWorkspace(team_id);
    applyCheckOutcome(team_id, outcome, wasActive);
    results.push({ team_id, name, status: outcome === 'valid' ? 'already_valid' : outcome });
  }

  res.json({ ok: true, results });
});

// --- Auto-Refresh Loop ---

const REFRESH_INTERVAL = 6 * 60 * 60 * 1000; // 6 hours

setInterval(async () => {
  console.log('Running auto-refresh check...');
  // Ids only: each check reads the session stored at that moment, because one slow refresh
  // can take 15 minutes and a re-pair can land in between.
  for (const { team_id } of tokenStore.listWorkspaces()) {
    const wasActive = isPollerActive(team_id);
    const outcome = await checkStoredWorkspace(team_id);
    applyCheckOutcome(team_id, outcome, wasActive);
  }
}, REFRESH_INTERVAL);

// --- Startup ---

// Sentry error handler — must be after all routes, before app.listen
if (process.env.SENTRY_DSN_VPS) {
  app.use(Sentry.expressErrorHandler());
}

// Fail-closed: refuse to start without BRIDGE_API_KEY. No local-dev bypass.
if (!BRIDGE_API_KEY) {
  console.error('FATAL: BRIDGE_API_KEY required — refusing to start without it.');
  process.exit(1);
}

app.listen(PORT, BIND_HOST, () => {
  console.log(`Slack bridge listening on ${BIND_HOST}:${PORT}`);

  // Start pollers for all connected workspaces
  for (const ws of tokenStore.listWorkspaces()) {
    if (ws.status === 'connected') {
      startPoller(ws.team_id);
    } else {
      console.log(`[${ws.name}] Skipped — status: ${ws.status}`);
    }
  }
});
