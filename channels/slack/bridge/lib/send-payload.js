// lib/send-payload.js
//
// Thread replies out (built, off by default -- UNIBOX_SLACK_SENDS gates the
// relay side, not this). server.js's /send route calls chat.postMessage
// directly and cannot be required() in a test (app.listen() at module scope,
// a fail-closed process.exit(1) if BRIDGE_API_KEY is unset). Same pattern as
// lib/security-limits.js's buildHealthPayload: pull the one piece of logic
// worth testing out into a pure function the route calls.

/**
 * Builds the object passed to chat.postMessage for POST /send.
 * thread_ts is optional and additive: omitted entirely (not sent as
 * undefined/null) when there is none, so a plain top-level send is byte-for-
 * byte what it was before this change.
 */
function buildPostMessagePayload({ channel, text, teamId, threadTs }) {
  const payload = { channel, text, team_id: teamId };
  if (threadTs) payload.thread_ts = threadTs;
  return payload;
}

module.exports = { buildPostMessagePayload };
