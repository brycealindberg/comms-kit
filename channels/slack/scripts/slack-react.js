#!/usr/bin/env node
// Emoji reactions for the Slack bridge. Run ON the VPS (slack_bridge.py pipes it over ssh stdin):
//   node - CHANNEL_ID WORKSPACE_NAME MESSAGE_TS REACTION_NAME check|add|remove
// check  = READ ONLY: reactions.get (works on a thread reply too) + auth.test, nothing written.
// add    = reactions.add; `already_reacted` is a no-op, not an error.
// remove = reactions.remove; `no_reaction` is a no-op, not an error.
// The ONLY write calls in this file are reactions.add and reactions.remove; no .mark calls.
// Prints ONE JSON object. Loads ENCRYPTION_KEY from the running bridge's /proc/<pid>/environ
// (never printed) and decrypts config.json through the bridge's own TokenStore, like
// slack-thread-read.js does. Kept separate from that file on purpose: its header promises no writes.
const fs = require('fs');
const { execSync } = require('child_process');
// The bridge folder on the server: slack_bridge.py sets SLACK_BRIDGE_DIR from slack.vps_bridge_dir.
const BRIDGE = process.env.SLACK_BRIDGE_DIR;
const [channel, wsName, ts, name, mode] = process.argv.slice(2);
const out = o => console.log(JSON.stringify(o));
if (!channel || !wsName || !ts || !name || !['check', 'add', 'remove'].includes(mode)) {
  console.error('usage: CHANNEL_ID WORKSPACE_NAME MESSAGE_TS REACTION_NAME check|add|remove');
  process.exit(2);
}

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
if (!process.env.ENCRYPTION_KEY) { out({ ok: false, error: 'no ENCRYPTION_KEY found in a bridge pid environ' }); process.exit(1); }

if (!BRIDGE) { out({ ok: false, error: 'SLACK_BRIDGE_DIR is not set (slack_bridge.py sets it from slack.vps_bridge_dir)' }); process.exit(1); }
const { TokenStore } = require(`${BRIDGE}/lib/token-store.js`);
const { createSlackClient } = require(`${BRIDGE}/lib/slack-client.js`);
const store = new TokenStore(`${BRIDGE}/config.json`);
const ws = Object.values(store.workspaces).find(w => w.name === wsName);
if (!ws) { out({ ok: false, error: `workspace not found: ${wsName}` }); process.exit(1); }
const client = createSlackClient(ws.token, ws.cookie);

(async () => {
  if (mode === 'check') {
    const me = await client.auth.test();
    const got = await client.reactions.get({ channel, timestamp: ts, full: true });
    const m = got.message || {};
    let author = m.user || m.bot_id || '?';
    if (m.user) {
      try { const u = await client.users.info({ user: m.user }); author = u.user.real_name || u.user.name || m.user; }
      catch (_) {}
    }
    out({ ok: true, mode, self: me.user_id, channel,
          message: { ts: m.ts, thread_ts: m.thread_ts || null, user: m.user || null, name: author,
                     text: (m.text || '').slice(0, 2000) },
          reactions: (m.reactions || []).map(r => ({ name: r.name, count: r.count, users: r.users || [] })) });
    return;
  }
  try {
    if (mode === 'add') await client.reactions.add({ channel, timestamp: ts, name });
    else await client.reactions.remove({ channel, timestamp: ts, name });
    out({ ok: true, mode, channel, ts, name });
  } catch (e) {
    const code = e.data && e.data.error;
    if ((mode === 'add' && code === 'already_reacted') || (mode === 'remove' && code === 'no_reaction')) {
      out({ ok: true, mode, channel, ts, name, noop: code });
      return;
    }
    out({ ok: false, mode, channel, ts, name, error: code || e.message });
    process.exit(1);
  }
})().catch(e => { out({ ok: false, mode, error: (e.data && e.data.error) || e.message }); process.exit(1); });
