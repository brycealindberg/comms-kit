// lib/poller.js
const { createSlackClient, validateToken } = require('./slack-client');
const { isTranscribableFile } = require('./transcription');

// Message subtypes to skip (same as bi_sync_slack.py)
const SKIP_SUBTYPES = new Set([
  'channel_join', 'channel_leave', 'channel_topic', 'channel_purpose',
  'channel_name', 'channel_archive', 'channel_unarchive',
  'group_join', 'group_leave', 'group_topic', 'group_purpose',
  'group_name', 'group_archive', 'group_unarchive',
  'bot_message', 'bot_add', 'bot_remove',
  'pinned_item', 'unpinned_item',
  'file_comment', 'file_mention',
]);

class WorkspacePoller {
  /**
   * @param {Object} opts
   * @param {string} opts.teamId
   * @param {string} opts.name
   * @param {string} opts.token - xoxc- token
   * @param {string} opts.cookie - xoxd- cookie
   * @param {import('./message-buffer').MessageBuffer} opts.buffer
   * @param {number} opts.intervalMs - Poll interval (default 30000)
   * @param {number} opts.backfillHours - Initial backfill window (default 24)
   * @param {Function} opts.onStatusChange - Callback(teamId, status)
   * @param {Function} opts.refreshFn - Async callback(teamId) that attempts token refresh. Returns { success, token? }
   */
  constructor({
    teamId, name, token, cookie, buffer, intervalMs = 30000, backfillHours = 24,
    channelRefreshMs = 15 * 60 * 1000, maxChannelRefreshBackoffMs = 2 * 60 * 60 * 1000,
    threadRecheckMs = 2 * 60 * 1000, maxThreadRechecksPerCycle = 10,
    threadTrackingWindowMs = 7 * 24 * 60 * 60 * 1000,
    onStatusChange, refreshFn,
  }) {
    this.teamId = teamId;
    this.name = name;
    this.client = createSlackClient(token, cookie);
    this.buffer = buffer;
    this.intervalMs = intervalMs;
    this.backfillHours = backfillHours;
    // How often to re-list channels via conversations.list. Slack rate-limits
    // that endpoint aggressively, so keep this well above the poll interval.
    this.channelRefreshMs = channelRefreshMs;
    // Ceiling for the extra delay added after repeated channel-refresh failures.
    this.maxChannelRefreshBackoffMs = maxChannelRefreshBackoffMs;
    this.onStatusChange = onStatusChange || (() => {});
    this.refreshFn = refreshFn || null;

    // State
    this.channels = [];           // cached channel list
    this.channelMap = new Map();   // channelId -> channel info
    this.userCache = new Map();    // userId -> user info
    this.ownerUserId = null;      // detected from auth.test
    this.lastPoll = null;
    this.lastChannelRefresh = 0;
    // Extra delay stacked on top of channelRefreshMs while conversations.list keeps
    // failing. Reset to 0 the moment a refresh succeeds.
    this.channelRefreshBackoffMs = 0;
    // Cleared ONLY by a successful refresh — deliberately not by a successful poll.
    // Once the latch fix lets history polls succeed through a failing refresh, these
    // are the only signal that the channel map is going stale.
    this.channelRefreshFailures = 0;
    this.lastChannelRefreshFailure = null;
    this.pollCount = 0;
    this.errorCount = 0;
    this.running = false;
    this._timer = null;

    // Only one poll cycle may be in flight at a time. Without this, a slow cycle
    // (Slack 429 retries sleep 30s+ each) lets setInterval stack overlapping polls
    // that all hammer the same rate-limited endpoint and keep it saturated.
    this._pollInFlight = false;
    this.skippedPolls = 0;

    // Rate-limit visibility. 'connected' only means start() ran, so without these a
    // workspace can be starved for hours while reporting connected / error_count 0.
    this.rateLimitedSince = null;
    this.consecutiveRateLimits = 0;

    // Thread replies (non-broadcast ones never otherwise reach the buffer --
    // conversations.history only ever shows a thread PARENT, never re-surfaces
    // it when the thread gains a new reply). `threadCursors` is keyed
    // `${channelId}:${parentTs}` -> { parentTs, channelId, latestReplyMeta,
    // importedThrough, lastChecked }. Trigger A (free): a newly-discovered
    // parent with reply_count > 0 gets its replies fetched once, right in the
    // normal history loop, bounded by new-message volume alone. Trigger B
    // (bounded, its own cadence): conversations.history can never tell us
    // whether an ALREADY-SEEN thread later gained a reply -- the replies call
    // IS the only check there is -- so a capped, least-recently-checked-first
    // sweep re-polls a small slice of tracked threads every threadRecheckMs.
    this.threadCursors = new Map();
    this.threadRecheckMs = threadRecheckMs;
    this.maxThreadRechecksPerCycle = maxThreadRechecksPerCycle;
    this.threadTrackingWindowMs = threadTrackingWindowMs;
    this.lastThreadRecheck = 0;

    // Track last-fetched timestamp per channel to only get new messages
    this.channelCursors = new Map(); // channelId -> lastSeenTs
  }

  async start() {
    if (this.running) return;
    this.running = true;

    // Detect the owner's user ID
    try {
      const authResult = await this.client.auth.test({ team_id: this.teamId });
      this.ownerUserId = authResult.user_id;
      console.log(`[${this.name}] Authenticated as ${authResult.user} (${this.ownerUserId})`);
    } catch (e) {
      console.error(`[${this.name}] Auth failed: ${e.message}`);
      this.onStatusChange(this.teamId, 'needs_reauth');
      this.running = false;
      return;
    }

    // Initial backfill
    const backfillTs = String(Date.now() / 1000 - this.backfillHours * 3600);
    // Set all channel cursors to backfill time initially
    this._defaultCursor = backfillTs;

    // First poll immediately
    await this._poll();

    // Then schedule recurring polls
    this._timer = setInterval(() => {
      this._poll().catch(e => console.error(`[${this.name}] Poll cycle rejected: ${e.message}`));
    }, this.intervalMs);
    console.log(`[${this.name}] Polling every ${this.intervalMs / 1000}s`);
  }

  stop() {
    this.running = false;
    if (this._timer) {
      clearInterval(this._timer);
      this._timer = null;
    }
    console.log(`[${this.name}] Stopped`);
  }

  /**
   * Serialised entry point. Guarantees at most one _pollOnce() in flight; the
   * finally clause releases the latch on every path, including a throw.
   */
  async _poll() {
    if (this._pollInFlight) {
      this.skippedPolls += 1;
      return;
    }
    this._pollInFlight = true;
    try {
      return await this._pollOnce();
    } finally {
      this._pollInFlight = false;
    }
  }

  async _pollOnce() {
    try {
      // Refresh channel list periodically (conversations.list is rate-limited).
      //
      // The window is claimed BEFORE the await, and a failure stacks a bounded
      // backoff instead of leaving the gate open. Previously lastChannelRefresh was
      // only advanced after a SUCCESSFUL refresh, so one sustained 429 meant every
      // subsequent 30s poll re-issued conversations.list; each of those retries 3x
      // with 30s sleeps, so the calls piled up and kept the endpoint saturated. That
      // latch held on one workspace for 41 days (~2,860 aborted polls/day).
      const now = Date.now();
      if (now - this.lastChannelRefresh > this.channelRefreshMs + this.channelRefreshBackoffMs) {
        this.lastChannelRefresh = now;
        try {
          await this._refreshChannels();
          this.channelRefreshBackoffMs = 0;
          this.channelRefreshFailures = 0;
          this.lastChannelRefreshFailure = null;
        } catch (e) {
          this.channelRefreshFailures += 1;
          this.lastChannelRefreshFailure = new Date().toISOString();
          // Bounded, and never permanent: the refresh is always retried, just less
          // often while Slack is refusing. 15m -> 30m -> 45m -> ... -> capped.
          this.channelRefreshBackoffMs = Math.min(
            this.channelRefreshBackoffMs === 0 ? this.channelRefreshMs : this.channelRefreshBackoffMs * 2,
            this.maxChannelRefreshBackoffMs,
          );
          throw e;
        }
      }

      // Fetch new messages per channel
      let totalNew = 0;
      let historySuccessCount = 0;
      let historyInvalidAuthError = null;
      for (const ch of this.channels) {
        const oldestTs = this.channelCursors.get(ch.id) || this._defaultCursor;
        let newMsgs;
        try {
          newMsgs = await this._fetchHistory(ch.id, oldestTs);
          historySuccessCount++;
        } catch (e) {
          if (e.data?.error === 'invalid_auth') {
            historyInvalidAuthError = e;
            continue;
          }
          throw e;
        }

        if (newMsgs.length > 0) {
          // Resolve users and enrich messages
          const enriched = await this._enrichMessages(newMsgs, ch);
          this.buffer.addBatch(ch.id, enriched);

          // Update cursor to newest message
          const newest = newMsgs.reduce((max, m) => parseFloat(m.ts) > parseFloat(max) ? m.ts : max, oldestTs);
          this.channelCursors.set(ch.id, newest);
          totalNew += enriched.length;

          // Every new top-level message is tracked -- zero-cost bookkeeping,
          // no call spent -- so a parent seen with reply_count=0 is still a
          // Trigger B candidate once it gains its first reply later:
          // conversations.history never re-surfaces a parent just because an
          // old thread of its gained a reply, so this is the only way that
          // common case is ever found. Trigger A (free) spends a call only
          // for a parent that already has replies by the time we see it;
          // bounded by new-message volume, never an extra call for a
          // message we were not already fetching.
          for (const m of newMsgs) {
            const key = `${ch.id}:${m.ts}`;
            if (!this.threadCursors.has(key)) {
              this.threadCursors.set(key, {
                parentTs: m.ts, channelId: ch.id,
                latestReplyMeta: null, importedThrough: m.ts, lastChecked: now,
              });
            }
            if (m.reply_count > 0) {
              totalNew += await this._importThreadReplies(ch, m.ts, m.latest_reply);
            }
          }
        }
      }

      // Trigger B (bounded, own cadence): conversations.history never
      // re-surfaces an already-seen parent just because its thread gained a
      // reply, so this is the only way to find a reply that arrives well
      // after its parent -- the common case. Capped and least-recently-
      // checked-first so the cost never grows past maxThreadRechecksPerCycle
      // calls per cycle, no matter how many threads accumulate.
      if (now - this.lastThreadRecheck > this.threadRecheckMs) {
        this.lastThreadRecheck = now;
        await this._recheckTrackedThreads(now);
      }

      if (historyInvalidAuthError && historySuccessCount === 0) {
        throw historyInvalidAuthError;
      }

      this.lastPoll = new Date().toISOString();
      this.pollCount++;
      this.errorCount = 0; // Reset on success
      this.rateLimitedSince = null;
      this.consecutiveRateLimits = 0;

      if (totalNew > 0) {
        console.log(`[${this.name}] Poll #${this.pollCount}: ${totalNew} new messages`);
      }
    } catch (e) {
      const isAuthError = e.data?.error === 'token_revoked' || e.data?.error === 'invalid_auth' || e.data?.error === 'not_authed';
      const isRateLimit = !isAuthError && this._isRateLimitError(e);

      // Rate limiting is transient, not a credential problem. Slack throttles
      // conversations.list/history hard, and treating a 429 like an auth failure
      // wrongly flips the workspace to needs_reauth and stops polling (which the
      // 6h auto-refresh loop then skips). Don't count it toward the safeguard —
      // skip this cycle and let the next scheduled poll retry once Slack lets up.
      if (isRateLimit) {
        // Record it. Swallowing this silently (connected, error_count 0, frozen
        // last_poll) is what let a 41-day starvation go unnoticed.
        this.consecutiveRateLimits += 1;
        if (!this.rateLimitedSince) this.rateLimitedSince = new Date().toISOString();
        const retry = typeof e.retryAfter === 'number' ? ` (retry-after: ${e.retryAfter}s)` : '';
        console.warn(`[${this.name}] Poll rate-limited${retry}, backing off until next cycle (consecutive: ${this.consecutiveRateLimits}, channel-refresh backoff: ${Math.round(this.channelRefreshBackoffMs / 60000)}m)`);
        return;
      }

      this.errorCount++;
      console.error(`[${this.name}] Poll error (${this.errorCount}): ${e.message}${isAuthError ? ' [auth]' : ''}`);

      // On auth error, attempt token refresh before giving up
      if (isAuthError && this.refreshFn) {
        console.log(`[${this.name}] Auth error detected, attempting token refresh...`);
        const result = await this.refreshFn(this.teamId);
        if (result && result.success && result.token) {
          console.log(`[${this.name}] Token refreshed successfully, updating client`);
          this.client = createSlackClient(result.token, result.cookie || this.client.headers?.cookie);
          this.errorCount = 0;
          return; // Continue polling with new token
        }
        console.error(`[${this.name}] Token refresh failed: ${result?.error || 'unknown'}`);
      }

      // After 5 consecutive errors, mark as needs_reauth
      if (this.errorCount >= 5) {
        this.onStatusChange(this.teamId, 'needs_reauth');
        this.stop();
      }
    }
  }

  async _refreshChannels() {
    const channels = [];
    let cursor;

    do {
      const opts = {
        types: 'public_channel,private_channel,mpim,im',
        limit: 200,
        exclude_archived: true,
        team_id: this.teamId,
      };
      if (cursor) opts.cursor = cursor;

      const result = await this.client.conversations.list(opts);
      for (const ch of result.channels || []) {
        if (ch.is_member !== false) {
          channels.push(ch);
          this.channelMap.set(ch.id, {
            id: ch.id,
            name: ch.name || ch.id,
            type: ch.is_im ? 'im' : ch.is_mpim ? 'mpim' : ch.is_private ? 'private' : 'channel',
            member_count: ch.num_members || 0,
            unread_count: ch.unread_count ?? null,
            unread_count_display: ch.unread_count_display ?? null,
            last_read: ch.last_read || null,
            latest: ch.latest || null,
            user: ch.user || null,
          });
        }
      }

      cursor = result.response_metadata?.next_cursor;
    } while (cursor);

    this.channels = channels;
    console.log(`[${this.name}] ${channels.length} channels`);
  }

  async _fetchHistory(channelId, oldestTs) {
    try {
      const result = await this.client.conversations.history({
        channel: channelId,
        oldest: oldestTs,
        limit: 200,
        inclusive: false,
        team_id: this.teamId,
      });
      return (result.messages || []).filter(m => !this._shouldSkip(m));
    } catch (e) {
      // Channel not accessible — skip it instead of poisoning the whole workspace poll.
      if (
        e.data?.error === 'channel_not_found'
        || e.data?.error === 'not_in_channel'
      ) {
        return [];
      }
      throw e;
    }
  }

  _shouldSkip(msg) {
    if (SKIP_SUBTYPES.has(msg.subtype)) return true;
    if (msg.bot_id) return true;

    const hasTranscribableFile = Array.isArray(msg.files) && msg.files.some(isTranscribableFile);
    if (!hasTranscribableFile && (!msg.text || msg.text.trim().length < 2)) return true;

    return false;
  }

  _isRateLimitError(e) {
    return e.code === 'slack_webapi_rate_limited_error'
      || typeof e.retryAfter === 'number'
      || e.data?.error === 'ratelimited'
      || /rate limit/i.test(e.message || '');
  }

  _isSoftThreadError(e) {
    return e.data?.error === 'thread_not_found'
      || e.data?.error === 'channel_not_found'
      || e.data?.error === 'not_in_channel';
  }

  /**
   * Fetches replies newer than `since` for one thread and adds them to the
   * buffer, with thread_ts forced to the parent's own ts. Returns
   * { count, maxTs } -- maxTs is null when nothing new came back, so a
   * caller can tell "checked, nothing new" from "checked, got some".
   * Slack always returns the parent as replies()'s first message regardless
   * of `oldest`; it is filtered out here so it is never double-counted (the
   * plain history fetch already delivered it).
   */
  async _fetchAndStoreReplies(ch, parentTs, since) {
    const result = await this.client.conversations.replies({
      channel: ch.id,
      ts: parentTs,
      oldest: since,
      inclusive: false,
      limit: 200,
      team_id: this.teamId,
    });
    const nonParent = (result.messages || []).filter(m => m.ts !== parentTs);
    const replies = nonParent.filter(m => !this._shouldSkip(m));
    // Advance past every message actually fetched, even ones filtered out
    // just above -- a page that is all bot replies (or all skip-worthy)
    // would otherwise re-fetch the SAME page forever (same `oldest`),
    // hiding a later human reply beyond this call's limit:200.
    const maxFetchedTs = nonParent.reduce(
      (max, m) => (parseFloat(m.ts) > parseFloat(max) ? m.ts : max), since);
    if (replies.length === 0) {
      return { count: 0, maxTs: nonParent.length > 0 ? maxFetchedTs : null };
    }

    const enriched = await this._enrichMessages(replies, ch);
    for (const m of enriched) m.thread_ts = parentTs;
    this.buffer.addBatch(ch.id, enriched);
    return { count: enriched.length, maxTs: maxFetchedTs };
  }

  /**
   * Trigger A: a parent just discovered in THIS poll's new-message batch
   * that already has replies. `latestReplyMeta` is Slack's own reply-count
   * summary field on the parent (no extra call to read it) -- re-observing
   * the SAME value (e.g. after a bridge restart re-returns an already-known
   * parent during the 24h backfill) means nothing changed, so no call is
   * spent. A changed value, or never having checked this parent before,
   * means the replies call is the only way to know what is new.
   */
  async _importThreadReplies(ch, parentTs, latestReplyMeta) {
    const key = `${ch.id}:${parentTs}`;
    const now = Date.now();
    const existing = this.threadCursors.get(key);
    if (existing && existing.latestReplyMeta === latestReplyMeta) {
      existing.lastChecked = now;
      return 0;
    }
    const since = existing ? existing.importedThrough : parentTs;
    let result;
    try {
      result = await this._fetchAndStoreReplies(ch, parentTs, since);
    } catch (e) {
      if (this._isRateLimitError(e)) {
        console.warn(`[${this.name}] Thread replies rate-limited for ${key}, will retry next cycle`);
        return 0;    // contained: the history ingest that already landed this cycle stands
      }
      if (this._isSoftThreadError(e)) { this.threadCursors.delete(key); return 0; }
      console.error(`[${this.name}] Thread replies error for ${key}: ${e.message}`);
      return 0;      // never let a single thread's failure fail the whole poll
    }
    this.threadCursors.set(key, {
      parentTs, channelId: ch.id, latestReplyMeta,
      importedThrough: result.maxTs || since, lastChecked: now,
    });
    return result.count;
  }

  /**
   * Trigger B: conversations.history never re-surfaces a parent just
   * because its thread gained a NEW reply, so for an already-tracked thread
   * the replies call IS the check -- there is no cheaper signal to poll
   * first. Capped at maxThreadRechecksPerCycle, least-recently-checked
   * first, so the per-cycle cost never grows past that no matter how many
   * threads accumulate. Old parents are pruned so the tracked set itself
   * stays bounded too.
   */
  async _recheckTrackedThreads(now) {
    for (const [key, entry] of this.threadCursors) {
      if (now - parseFloat(entry.parentTs) * 1000 > this.threadTrackingWindowMs) {
        this.threadCursors.delete(key);
      }
    }

    const candidates = [...this.threadCursors.entries()]
      .sort((a, b) => a[1].lastChecked - b[1].lastChecked)
      .slice(0, this.maxThreadRechecksPerCycle);

    for (const [key, entry] of candidates) {
      const chInfo = this.channelMap.get(entry.channelId);
      if (!chInfo) { this.threadCursors.delete(key); continue; }
      try {
        const result = await this._fetchAndStoreReplies(
          { id: entry.channelId, name: chInfo.name }, entry.parentTs, entry.importedThrough);
        if (result.maxTs) entry.importedThrough = result.maxTs;
        entry.lastChecked = now;
      } catch (e) {
        if (this._isRateLimitError(e)) {
          console.warn(`[${this.name}] Thread recheck rate-limited at ${key}, stopping this cycle's sweep`);
          break;     // keep the budget bounded; this entry stays due for next cycle
        }
        if (this._isSoftThreadError(e)) { this.threadCursors.delete(key); continue; }
        console.error(`[${this.name}] Thread recheck error for ${key}: ${e.message}`);
        entry.lastChecked = now;    // don't let a persistently failing thread hog the queue
      }
    }
  }

  async _enrichMessages(messages, channel) {
    const enriched = [];
    for (const msg of messages) {
      const user = msg.user ? await this._resolveUser(msg.user) : null;
      enriched.push({
        ts: msg.ts,
        text: msg.text,
        user: msg.user,
        sender_name: user?.real_name || user?.name || msg.user || 'unknown',
        sender_email: user?.email || '',
        is_from_me: msg.user === this.ownerUserId,
        channel_id: channel.id,
        channel_name: channel.name || channel.id,
        workspace: this.name,
        team_id: this.teamId,
        thread_ts: msg.thread_ts || null,
        reactions: msg.reactions || [],
        attachments: (msg.files || []).map(f => ({
          id: f.id,
          name: f.name,
          title: f.title || null,
          type: f.mimetype || f.filetype || null,
          mimetype: f.mimetype || null,
          filetype: f.filetype || null,
          size: f.size,
          url_private: f.url_private || null,
          url_private_download: f.url_private_download || null,
        })),
      });
    }
    return enriched;
  }

  async _resolveUser(userId) {
    if (this.userCache.has(userId)) return this.userCache.get(userId);

    try {
      const result = await this.client.users.info({ user: userId, team_id: this.teamId });
      const u = result.user || {};
      const profile = u.profile || {};
      const info = {
        id: userId,
        name: u.name || userId,
        real_name: profile.real_name_normalized || profile.real_name || u.real_name || '',
        email: profile.email || '',
        avatar: profile.image_72 || '',
        is_bot: u.is_bot || false,
      };
      this.userCache.set(userId, info);
      return info;
    } catch (e) {
      const info = { id: userId, name: userId, real_name: '', email: '', avatar: '', is_bot: false };
      this.userCache.set(userId, info);
      return info;
    }
  }

  getStatus() {
    return {
      team_id: this.teamId,
      name: this.name,
      status: this.running ? 'connected' : 'stopped',
      channels: this.channels.length,
      buffered_messages: this.channels.reduce(
        (sum, ch) => sum + (this.buffer.getMessages(ch.id)?.length || 0), 0
      ),
      users_cached: this.userCache.size,
      last_poll: this.lastPoll,
      poll_count: this.pollCount,
      error_count: this.errorCount,
      // 'status' above only reflects whether start() ran. These are the fields that
      // actually reveal a starved poller.
      rate_limited_since: this.rateLimitedSince,
      consecutive_rate_limits: this.consecutiveRateLimits,
      channel_refresh_backoff_ms: this.channelRefreshBackoffMs,
      channel_refresh_failures: this.channelRefreshFailures,
      last_channel_refresh_failure: this.lastChannelRefreshFailure,
      skipped_polls: this.skippedPolls,
      tracked_threads: this.threadCursors.size,
    };
  }
}

module.exports = { WorkspacePoller };
