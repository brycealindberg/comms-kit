#!/usr/bin/env node
// Admin verbs for the Slack bridge. Run ON the VPS (slack_bridge.py pipes it over ssh stdin):
//   node - WORKSPACE_NAME METHOD PARAMS_BASE64
// PARAMS_BASE64 = base64 of one JSON object. Prints ONE JSON object: {ok, method, result} or
// {ok:false, method, error}. Exit 0 = Slack said ok, 1 = Slack or the box said no, 2 = refused here.
//
// STRICT ALLOW-LIST. Only the twelve methods in METHODS can run, each with its own parameter
// list (an unknown or missing key is refused before any network call). Nothing else is callable:
// no chat.postMessage, no *.mark, no reactions, no files, no admin.*, no generic apiCall. The method
// is looked up with hasOwnProperty, so `constructor` or `__proto__` cannot slip through.
// The bridge itself is not touched: no file on the VPS changes, nothing is restarted.
// Loads ENCRYPTION_KEY from the running bridge's /proc/<pid>/environ (never printed) and decrypts
// config.json through the bridge's own TokenStore, like slack-react.js does. Tokens never print.
const fs = require('fs');
const { execSync } = require('child_process');
// The bridge folder on the server: slack_bridge.py sets SLACK_BRIDGE_DIR from slack.vps_bridge_dir.
const BRIDGE = process.env.SLACK_BRIDGE_DIR;
const out = o => console.log(JSON.stringify(o));

const ID = /^[A-Z0-9]{8,15}$/;
const TS = /^\d{9,11}\.\d{1,6}$/;
const USER = /^[UW][A-Z0-9]{8,15}$/;
const CHAN_NAME = /^[a-z0-9_-]{1,80}$/;
const str = (re, max = 100) => v => typeof v === 'string' && v.length <= max && re.test(v);
const anyText = max => v => typeof v === 'string' && v.length > 0 && v.length <= max;

// method -> { required: {key: check}, optional: {key: check}, call(client, params) }
const METHODS = {
  'auth.test': { required: {}, optional: {}, call: (c) => c.auth.test() },
  'conversations.open': {
    required: { users: str(USER, 16) }, optional: {},          // ONE user id: a 1:1 DM, never a group
    call: (c, p) => c.conversations.open({ users: p.users, return_im: true }) },
  'chat.update': {
    required: { channel: str(ID, 15), ts: str(TS, 20), text: anyText(40000) }, optional: {},
    call: (c, p) => c.chat.update({ channel: p.channel, ts: p.ts, text: p.text }) },
  'chat.delete': {
    required: { channel: str(ID, 15), ts: str(TS, 20) }, optional: {},
    call: (c, p) => c.chat.delete({ channel: p.channel, ts: p.ts }) },
  'search.messages': {
    required: { query: anyText(500) },
    optional: { count: v => Number.isInteger(v) && v >= 1 && v <= 100 },
    call: async (c, p) => {
      let query = p.query;
      if (query.includes('{ME}')) {                              // "mentions" asks for the caller's own id
        const me = await c.auth.test();
        query = query.split('{ME}').join(me.user_id);
      }
      return c.search.messages({ query, count: p.count || 20, sort: 'timestamp', sort_dir: 'desc' });
    } },
  'conversations.create': {
    required: { name: str(CHAN_NAME, 80) }, optional: { is_private: v => typeof v === 'boolean' },
    call: (c, p) => c.conversations.create({ name: p.name, is_private: !!p.is_private }) },
  'conversations.invite': {
    required: { channel: str(ID, 15), users: str(USER, 16) }, optional: {},
    call: (c, p) => c.conversations.invite({ channel: p.channel, users: p.users }) },
  'conversations.kick': {
    required: { channel: str(ID, 15), user: str(USER, 16) }, optional: {},
    call: (c, p) => c.conversations.kick({ channel: p.channel, user: p.user }) },
  'conversations.rename': {
    required: { channel: str(ID, 15), name: str(CHAN_NAME, 80) }, optional: {},
    call: (c, p) => c.conversations.rename({ channel: p.channel, name: p.name }) },
  'conversations.leave': {
    required: { channel: str(ID, 15) }, optional: {},
    call: (c, p) => c.conversations.leave({ channel: p.channel }) },
  'conversations.archive': {
    required: { channel: str(ID, 15) }, optional: {},
    call: (c, p) => c.conversations.archive({ channel: p.channel }) },
  'conversations.members': {
    required: { channel: str(ID, 15) },
    optional: { limit: v => Number.isInteger(v) && v >= 1 && v <= 1000, cursor: v => typeof v === 'string' && v.length <= 200 },
    call: (c, p) => c.conversations.members({ channel: p.channel, limit: p.limit || 200, ...(p.cursor ? { cursor: p.cursor } : {}) }) },
  'conversations.info': {
    required: { channel: str(ID, 15) }, optional: {},
    call: (c, p) => c.conversations.info({ channel: p.channel }) },
};

// What each method may print: a short whitelist, never the raw reply.
const SHAPE = {
  'auth.test': r => ({ user_id: r.user_id, user: r.user, team_id: r.team_id, team: r.team }),
  'conversations.open': r => ({ channel: r.channel && r.channel.id, no_op: !!r.already_open }),
  'chat.update': r => ({ channel: r.channel, ts: r.ts }),
  'chat.delete': r => ({ channel: r.channel, ts: r.ts }),
  'search.messages': r => {
    const m = (r.messages && r.messages.matches) || [];
    return { total: (r.messages && r.messages.total) || 0,
             matches: m.map(x => ({ ts: x.ts, channel_id: x.channel && x.channel.id, channel_name: x.channel && x.channel.name,
                                    user: x.user || null, username: x.username || null,
                                    text: String(x.text || '').slice(0, 2000), permalink: x.permalink || null })) };
  },
  'conversations.create': r => ({ channel: r.channel && r.channel.id, name: r.channel && r.channel.name }),
  'conversations.invite': r => ({ channel: r.channel && r.channel.id }),
  'conversations.kick': () => ({}),
  'conversations.rename': r => ({ channel: r.channel && r.channel.id, name: r.channel && r.channel.name }),
  'conversations.leave': r => ({ not_in_channel: !!r.not_in_channel }),
  'conversations.archive': () => ({}),
  'conversations.members': r => ({ members: r.members || [],
                                   next_cursor: (r.response_metadata && r.response_metadata.next_cursor) || '' }),
  'conversations.info': r => { const c = r.channel || {};
    return { id: c.id, name: c.name || null, is_private: !!c.is_private, is_im: !!c.is_im, is_mpim: !!c.is_mpim,
             is_member: c.is_member !== false, num_members: c.num_members == null ? null : c.num_members,
             user: c.user || null, creator: c.creator || null }; },
};

const [wsName, method, b64] = process.argv.slice(2);
const refuse = (error, detail) => { out({ ok: false, method: String(method).slice(0, 60), error, ...(detail ? { detail } : {}) }); process.exit(2); };

if (!wsName || !method || b64 === undefined) { console.error('usage: WORKSPACE_NAME METHOD PARAMS_BASE64'); process.exit(2); }
// The allow-list check comes FIRST: a refused method never reads a key or a token.
if (!Object.prototype.hasOwnProperty.call(METHODS, method)) refuse('method_not_allowed');
const spec = METHODS[method];

let params;
try { params = JSON.parse(Buffer.from(b64, 'base64').toString('utf8')); } catch (_) { refuse('bad_params', 'not base64 JSON'); }
if (params === null || typeof params !== 'object' || Array.isArray(params)) refuse('bad_params', 'params must be an object');
for (const k of Object.keys(params)) {
  const check = Object.prototype.hasOwnProperty.call(spec.required, k) ? spec.required[k]
    : Object.prototype.hasOwnProperty.call(spec.optional, k) ? spec.optional[k] : null;
  if (!check) refuse('bad_params', `unknown key: ${k.slice(0, 40)}`);
  if (!check(params[k])) refuse('bad_params', `bad value for ${k}`);
}
for (const k of Object.keys(spec.required)) if (!(k in params)) refuse('bad_params', `missing key: ${k}`);

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
if (!process.env.ENCRYPTION_KEY) { out({ ok: false, method, error: 'no ENCRYPTION_KEY found in a bridge pid environ' }); process.exit(1); }

if (!BRIDGE) { out({ ok: false, method, error: 'SLACK_BRIDGE_DIR is not set (slack_bridge.py sets it from slack.vps_bridge_dir)' }); process.exit(1); }
const { TokenStore } = require(`${BRIDGE}/lib/token-store.js`);
const { createSlackClient } = require(`${BRIDGE}/lib/slack-client.js`);
const store = new TokenStore(`${BRIDGE}/config.json`);
const ws = Object.values(store.workspaces).find(w => w.name === wsName);
if (!ws) { out({ ok: false, method, error: `workspace not found: ${wsName}` }); process.exit(1); }
const client = createSlackClient(ws.token, ws.cookie);

(async () => {
  try {
    const r = await spec.call(client, params);
    out({ ok: true, method, result: SHAPE[method](r || {}) });
  } catch (e) {
    out({ ok: false, method, error: (e.data && e.data.error) || e.message });
    process.exit(1);
  }
})().catch(e => { out({ ok: false, method, error: (e.data && e.data.error) || e.message }); process.exit(1); });
