#!/usr/bin/env node
// READ-ONLY thread reader for the Slack bridge. Run ON the VPS from any dir:
//   node slack-thread-read.js CHANNEL_ID WORKSPACE_NAME [TOP_LEVEL_LIMIT] [json|feed]
// `json` (used by `send --since`): one JSON object, each top-level message with its
// newest reply time (`latest_reply`), and no conversations.replies calls.
// `feed` (used by `comms unread` through `threads --json`): one JSON object with every
// top-level message and every thread reply, so a reply in an old thread is not missed.
// Loads ENCRYPTION_KEY from the running bridge's /proc/<pid>/environ (never printed),
// decrypts config.json through the bridge's own TokenStore, then calls
// conversations.history (+ conversations.replies for any message with reply_count > 0).
// No .mark calls, no writes. Prints ts, local time (TZ), sender name, text (trimmed).
const fs = require('fs');
const { execSync } = require('child_process');
// SLACK_BRIDGE_DIR is the bridge folder on the server. slack_bridge.py sets it from the config key
// slack.vps_bridge_dir on every ssh run; offline fixtures set it to stand in for the bridge.
const BRIDGE = process.env.SLACK_BRIDGE_DIR;
// 5th arg: `full` -> optional older-than ts (the `more` hint); `thread` -> the thread root ts.
const [channel, wsName, limitArg, mode, extraArg] = process.argv.slice(2);
if (!channel || !wsName) { console.error('usage: CHANNEL_ID WORKSPACE_NAME [LIMIT]'); process.exit(2); }
if (!BRIDGE) { console.error('SLACK_BRIDGE_DIR is not set (slack_bridge.py sets it from slack.vps_bridge_dir)'); process.exit(2); }
const limit = parseInt(limitArg || '30', 10);

if (!process.env.ENCRYPTION_KEY) {
  const pids = execSync('pgrep -f "node .*slack-bridge/server.js" || true').toString().trim().split(/\s+/).filter(Boolean);
  for (const pid of pids) {
    try {
      const env = fs.readFileSync(`/proc/${pid}/environ`).toString().split('\0');
      const hit = env.find(e => e.startsWith('ENCRYPTION_KEY='));
      if (hit) { process.env.ENCRYPTION_KEY = hit.slice('ENCRYPTION_KEY='.length); break; }
    } catch (_) {}
  }
}
if (!process.env.ENCRYPTION_KEY) { console.error('no ENCRYPTION_KEY found in a bridge pid environ'); process.exit(1); }

const { TokenStore } = require(`${BRIDGE}/lib/token-store.js`);
const { createSlackClient } = require(`${BRIDGE}/lib/slack-client.js`);
const store = new TokenStore(`${BRIDGE}/config.json`);
const ws = Object.values(store.workspaces).find(w => w.name === wsName);
if (!ws) { console.error('workspace not found; names:', Object.values(store.workspaces).map(w => w.name).join(', ')); process.exit(1); }
const client = createSlackClient(ws.token, ws.cookie);
const names = {};
async function who(uid) {
  if (!uid) return '?';
  if (!names[uid]) {
    try { const r = await client.users.info({ user: uid }); names[uid] = r.user.real_name || r.user.name || uid; }
    catch (_) { names[uid] = uid; }
  }
  return names[uid];
}
// Zone comes from TZ, which slack_bridge.py sets to the Mac's current zone (the owner travels).
const et = ts => new Date(parseFloat(ts) * 1000).toLocaleString('en-US', { hour12: true, timeZoneName: 'short' });
// --- P0 (2026-10-30): lossless modes. Nothing above or in the default/json/feed modes changed.
const REPLY_CAP = 2000;
function blocksText(blocks) {
  const out = [];
  const walk = (node) => {
    if (!node || typeof node !== 'object') return;
    if (Array.isArray(node)) { node.forEach(walk); return; }
    if (node.type === 'text' && typeof node.text === 'string') out.push(node.text);
    else if (node.type === 'link') out.push(node.text || node.url || '');
    else if (node.type === 'user') out.push(`<@${node.user_id}>`);
    else if (node.type === 'emoji') out.push(`:${node.name}:`);
    else if (node.type === 'section' && node.text && typeof node.text.text === 'string') out.push(node.text.text);
    ['elements', 'fields'].forEach(k => walk(node[k]));
  };
  walk(blocks || []);
  return out.join('');
}
async function allReplies(ts) {
  const out = []; let cursor; let capped = false;
  for (;;) {
    const r = await client.conversations.replies({ channel, ts, limit: 200, cursor });
    for (const m of (r.messages || [])) if (m.ts !== ts) out.push(m);
    cursor = r.response_metadata && r.response_metadata.next_cursor;
    if (out.length >= REPLY_CAP) { capped = !!(r.has_more || cursor); break; }
    if (!r.has_more || !cursor) break;
  }
  return { replies: out.slice(0, REPLY_CAP), capped };
}
async function rowFull(m, parent) {
  const reactions = [];
  for (const r of (m.reactions || [])) {
    for (const u of (r.users || [])) reactions.push({ key: `:${r.name}:`, sender: await who(u), sender_id: u });
  }
  const bt = blocksText(m.blocks);
  return {
    ts: m.ts, thread_ts: parent || null, user: m.user || null, bot_id: m.bot_id || null,
    name: m.user ? await who(m.user) : ((m.bot_profile && m.bot_profile.name) || m.username || (m.bot_id ? await who(m.bot_id) : '?')),
    subtype: m.subtype || null, text: m.text || '', edited: m.edited || null,
    files: (m.files || []).map(f => ({ id: f.id, name: f.name || f.title || null, mimetype: f.mimetype || null, size: f.size || null })),
    reactions,
    attachments: (m.attachments || []).map(a => ({ fallback: a.fallback || null, title: a.title || null, pretext: a.pretext || null, text: a.text || null })),
    blocks_text: bt && bt !== (m.text || '') ? bt : null,
    reply_count: m.reply_count || 0, latest_reply: m.latest_reply || null,
  };
}
async function fullMode() {
  const me = await client.auth.test().then(r => r.user_id).catch(() => null);
  const top = []; let cursor; let endReached = false;
  for (;;) {
    const args = { channel, limit: Math.min(200, limit - top.length), cursor };
    if (extraArg) { args.latest = extraArg; args.inclusive = false; }
    const r = await client.conversations.history(args);
    top.push(...(r.messages || []));
    cursor = r.response_metadata && r.response_metadata.next_cursor;
    if (!r.has_more) { endReached = true; break; }
    if (top.length >= limit || !cursor) break;
  }
  const msgs = top.slice(0, limit).reverse();
  if (top.length > limit) endReached = false;
  const out = [];
  for (const m of msgs) {
    const row = await rowFull(m, null);
    out.push(row);
    if (m.reply_count) {
      const { replies, capped } = await allReplies(m.ts);
      row.replies_capped = capped;
      for (const r of replies) out.push(await rowFull(r, m.ts));
    }
  }
  console.log(JSON.stringify({ channel, workspace: wsName, me, end_reached: endReached, messages: out }));
}
async function threadMode() {
  const first = await client.conversations.replies({ channel, ts: extraArg, limit: 1 });
  const root = (first.messages || [])[0];
  const found = !!(root && root.ts === extraArg);
  const { replies, capped } = found ? await allReplies(extraArg) : { replies: [], capped: false };
  const out = [];
  for (const r of replies) out.push({ ts: r.ts, name: await who(r.user || r.bot_id), text: r.text || '' });
  console.log(JSON.stringify({ channel, workspace: wsName, thread_ts: extraArg, found_root: found, capped, messages: out }));
}

(async () => {
  if (mode === 'full') return fullMode();
  if (mode === 'thread') return threadMode();
  const hist = await client.conversations.history({ channel, limit });
  const msgs = (hist.messages || []).slice().reverse();
  if (mode === 'json') {
    const out = [];
    for (const m of msgs) {
      out.push({ ts: m.ts, latest_reply: m.latest_reply || null, reply_count: m.reply_count || 0,
                 name: await who(m.user || m.bot_id), text: (m.text || '').slice(0, 300) });
    }
    console.log(JSON.stringify({ channel, workspace: wsName, messages: out }));
    return;
  }
  // `feed` (used by `comms unread`): one JSON object, every top-level message AND every
  // thread reply (with its parent's `thread_ts`), each with sender name, text and file count.
  if (mode === 'feed') {
    const out = [];
    const row = async (m, parent) => ({ ts: m.ts, thread_ts: parent || null,
      name: await who(m.user || m.bot_id), text: (m.text || '').slice(0, 900),
      files: (m.files || []).length });
    for (const m of msgs) {
      out.push(await row(m, null));
      if (m.reply_count) {
        const rep = await client.conversations.replies({ channel, ts: m.ts, limit: 100 });
        for (const r of (rep.messages || []).slice(1)) out.push(await row(r, m.ts));
      }
    }
    console.log(JSON.stringify({ channel, workspace: wsName, messages: out }));
    return;
  }
  console.log(`channel ${channel} workspace ${wsName}: ${msgs.length} top-level message(s), newest last`);
  for (const m of msgs) {
    const n = await who(m.user);
    console.log(`\n[${et(m.ts)}] ${n}${m.reply_count ? ` (replies: ${m.reply_count})` : ''}\n  ${(m.text || '').replace(/\n/g, '\n  ').slice(0, 900)}`);
    if (m.reply_count) {
      const rep = await client.conversations.replies({ channel, ts: m.ts, limit: 100 });
      for (const r of (rep.messages || []).slice(1)) {
        const rn = await who(r.user);
        console.log(`    -> [${et(r.ts)}] ${rn}\n       ${(r.text || '').replace(/\n/g, '\n       ').slice(0, 900)}`);
      }
    }
  }
})().catch(e => { console.error('ERR', e.data ? JSON.stringify(e.data).slice(0, 300) : e.message); process.exit(1); });
