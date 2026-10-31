// lib/team-guard.js
//
// Guards every place that stores or refreshes a Slack token for a specific
// team: the team a pasted (or refreshed) token actually belongs to — per
// Slack's own auth.test / client.boot response — must match the team_id it
// is about to be saved or refreshed under. Without this, pairing several
// workspaces back to back can silently save one workspace's token under a
// different workspace's team_id, leaving that other team's Slack unread.

/**
 * Compare the team a token is being saved/refreshed under against the team
 * Slack itself says the token belongs to. Never includes a token value.
 */
function assertTeamMatches(expectedTeamId, actualTeamId) {
  if (expectedTeamId && actualTeamId && expectedTeamId !== actualTeamId) {
    return {
      ok: false,
      error: `Token belongs to team ${actualTeamId}, not the requested team ${expectedTeamId}`,
    };
  }
  return { ok: true };
}

/**
 * Decide whether a POST /admin/tokens request should be accepted.
 * Pure aside from the injected validateFn — never touches the token store,
 * so "ok: false" alone proves nothing gets persisted.
 *
 * Returns either:
 *   { ok: false, status, error }
 *   { ok: true, workspace: { name, token, cookie }, response: {...} }
 */
async function evaluateTokenUpload({ workspace, team_id, token, cookie, validateFn }) {
  if (!token || !cookie || !team_id) {
    return { ok: false, status: 400, error: 'team_id, token, and cookie required' };
  }

  const validation = await validateFn(token, cookie);
  if (!validation.valid) {
    return { ok: false, status: 400, error: `Token validation failed: ${validation.error}` };
  }

  const teamCheck = assertTeamMatches(team_id, validation.team_id);
  if (!teamCheck.ok) {
    return { ok: false, status: 400, error: teamCheck.error };
  }

  return {
    ok: true,
    workspace: {
      name: workspace || validation.team || team_id,
      token,
      cookie,
    },
    response: {
      ok: true,
      team_id,
      workspace: workspace || validation.team,
      user: validation.user,
    },
  };
}

/**
 * Validate a stored token and confirm it still belongs to the team it is
 * stored under. A token that passes auth.test but for the wrong team must
 * be treated as invalid for this workspace — not "still connected" — or a
 * mis-saved token (see evaluateTokenUpload) keeps reporting healthy forever
 * and never gets refreshed or flagged for re-pairing.
 *
 * Returns { valid: false, error } or { valid: true, team_id }
 */
async function validateTokenForTeam(teamId, token, cookie, validateFn) {
  const validation = await validateFn(token, cookie);
  if (!validation.valid) {
    return { valid: false, error: validation.error };
  }

  const teamCheck = assertTeamMatches(teamId, validation.team_id);
  if (!teamCheck.ok) {
    return { valid: false, error: teamCheck.error };
  }

  return { valid: true, team_id: validation.team_id };
}

module.exports = { assertTeamMatches, evaluateTokenUpload, validateTokenForTeam };
