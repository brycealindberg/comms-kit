// lib/slack-client.js
const { WebClient } = require('@slack/web-api');

/**
 * Create a Slack WebClient authenticated with a browser session token (xoxc-)
 * and the associated d cookie. This mimics what the Slack web app does.
 */
function createSlackClient(token, cookie) {
  return new WebClient(token, {
    headers: {
      cookie: `d=${cookie}`,
    },
    retryConfig: {
      retries: 3,
      factor: 2,
      randomize: true,
    },
  });
}

/**
 * Validate that a token + cookie pair is still valid.
 * Uses auth.test which is lightweight and doesn't fetch messages.
 */
async function validateToken(token, cookie) {
  try {
    const client = createSlackClient(token, cookie);
    const result = await client.auth.test();
    return {
      valid: true,
      user_id: result.user_id,
      team_id: result.team_id,
      team: result.team,
      user: result.user,
    };
  } catch (e) {
    return {
      valid: false,
      error: e.message || String(e),
    };
  }
}

/**
 * Refresh an expired xoxc- token using the long-lived d cookie.
 * Calls Slack's client.boot API which returns a fresh session token.
 *
 * @param {string} workspaceName - Slack workspace subdomain (e.g. 'acmecoai')
 * @param {string} cookie - The xoxd- d cookie value
 * @returns {{ success: boolean, token?: string, team_id?: string, error?: string }}
 */
async function refreshToken(workspaceName, cookie) {
  try {
    const res = await fetch(`https://${workspaceName}.slack.com/api/client.boot`, {
      method: 'POST',
      headers: {
        'Cookie': `d=${cookie}`,
        'Content-Type': 'application/x-www-form-urlencoded',
      },
      body: 'token=',
    });

    if (!res.ok) {
      return { success: false, error: `HTTP ${res.status}: ${res.statusText}` };
    }

    const data = await res.json();

    if (!data.ok) {
      return { success: false, error: data.error || 'client.boot returned ok=false' };
    }

    const token = data.api_token;
    const teamId = data.team?.id;

    if (!token) {
      return { success: false, error: 'No api_token in client.boot response' };
    }

    return { success: true, token, team_id: teamId };
  } catch (e) {
    return { success: false, error: e.message || String(e) };
  }
}

module.exports = { createSlackClient, validateToken, refreshToken };
