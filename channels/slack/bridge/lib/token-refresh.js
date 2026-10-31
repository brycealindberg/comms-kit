// lib/token-refresh.js
const { assertTeamMatches } = require('./team-guard');

// A token check can run for 15 minutes (3 refresh tries, 5 and 10 minutes apart), and `pair`
// can push a new token and cookie in that time. On 2026-10-13 a check that began at 18:38Z
// marked three freshly paired workspaces needs_reauth with their OLD tokens and stopped the
// new pollers. So every step compares the stored session with the one the check began with,
// and a replaced session ends the check as superseded.
function sameSession(store, teamId, started) {
  const current = store.getWorkspace(teamId);
  return !!current && current.token === started.token && current.cookie === started.cookie;
}

/**
 * Attempt to refresh a workspace's token using client.boot.
 * Retries up to maxRetries times with exponential backoff. Refuses to store
 * a refreshed token whose team doesn't match the workspace it was refreshed
 * for — a client.boot response for the wrong team must never get written
 * under this teamId (see lib/team-guard.js). Stops, and stores nothing, once
 * a re-pair has replaced the session it began with.
 *
 * Returns { success, token, cookie, superseded?, error }
 */
async function attemptRefresh(teamId, {
  maxRetries = 3,
  store,
  refreshFn,
  delayFn = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  log = console.log,
  logError = console.error,
} = {}) {
  const ws = store.getWorkspace(teamId);
  if (!ws) return { success: false, error: 'workspace not found' };
  const started = { token: ws.token, cookie: ws.cookie };
  const superseded = { success: false, superseded: true, error: 're-paired during refresh' };

  for (let attempt = 1; attempt <= maxRetries; attempt++) {
    if (!sameSession(store, teamId, started)) return superseded;
    log(`[${ws.name}] Token refresh attempt ${attempt}/${maxRetries}...`);
    const result = await refreshFn(ws.name, started.cookie);
    if (!sameSession(store, teamId, started)) return superseded;

    if (result.success) {
      const teamCheck = assertTeamMatches(teamId, result.team_id);
      if (!teamCheck.ok) {
        logError(`[${ws.name}] ${teamCheck.error}`);
        return { success: false, error: teamCheck.error };
      }

      store.updateToken(teamId, result.token);
      log(`[${ws.name}] Token refreshed successfully`);
      return { success: true, token: result.token, cookie: started.cookie };
    }

    logError(`[${ws.name}] Refresh attempt ${attempt} failed: ${result.error}`);
    if (attempt < maxRetries) {
      const delay = attempt * 5 * 60 * 1000; // 5min, 10min backoff
      log(`[${ws.name}] Retrying in ${delay / 60000} minutes...`);
      await delayFn(delay);
    }
  }

  return { success: false, error: 'all refresh attempts failed' };
}

/**
 * Check one workspace with the session stored NOW, never a copy from when a sweep began.
 * Takes attemptRefresh's options plus validateFn(token, cookie, teamId).
 * Returns 'missing' | 'valid' | 'refreshed' | 'superseded' | 'needs_reauth'.
 * It writes the 'connected' and 'needs_reauth' statuses; the caller starts or stops pollers.
 */
async function checkWorkspace(teamId, options) {
  const { store, validateFn, log = console.log } = options;
  const ws = store.getWorkspace(teamId);
  if (!ws) return 'missing';
  const started = { token: ws.token, cookie: ws.cookie };

  const validation = await validateFn(started.token, started.cookie, teamId);
  if (!sameSession(store, teamId, started)) return 'superseded';
  if (validation.valid) {
    store.updateStatus(teamId, 'connected');
    return 'valid';
  }

  log(`[${ws.name}] Token expired, attempting auto-refresh...`);
  const result = await attemptRefresh(teamId, options);
  if (result.success) return 'refreshed';
  if (result.superseded || !sameSession(store, teamId, started)) {
    log(`[${ws.name}] Re-paired during the check; left the new session alone`);
    return 'superseded';
  }
  store.updateStatus(teamId, 'needs_reauth');
  return 'needs_reauth';
}

module.exports = { attemptRefresh, checkWorkspace, sameSession };
