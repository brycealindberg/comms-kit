// tests/token-refresh.test.js
const { describe, it, beforeEach, afterEach, mock } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

// node --test runs test files at once, so each file needs its own config file.
const CONFIG_PATH = path.join(os.tmpdir(), `token-refresh-test-${process.pid}.json`);
process.env.ENCRYPTION_KEY = 'a]4f9b2c1d8e7f6a5b4c3d2e1f0a9b8c7d6e5f4a3b2c1d0e';

// --- refreshToken tests ---

describe('refreshToken', () => {
  let originalFetch;

  beforeEach(() => {
    originalFetch = globalThis.fetch;
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  it('should return a fresh token on success', async () => {
    globalThis.fetch = mock.fn(async () => ({
      ok: true,
      json: async () => ({
        ok: true,
        api_token: 'xoxc-f0017',
        team: { id: 'T0123' },
      }),
    }));

    const { refreshToken } = require('../lib/slack-client');
    const result = await refreshToken('testworkspace', 'xoxd-f0018');

    assert.strictEqual(result.success, true);
    assert.strictEqual(result.token, 'xoxc-f0017');
    assert.strictEqual(result.team_id, 'T0123');

    // Verify fetch was called with correct URL and cookie
    const call = globalThis.fetch.mock.calls[0];
    assert.strictEqual(call.arguments[0], 'https://testworkspace.slack.com/api/client.boot');
    assert.ok(call.arguments[1].headers.Cookie.includes('xoxd-f0018'));
  });

  it('should return error when Slack API returns ok=false', async () => {
    globalThis.fetch = mock.fn(async () => ({
      ok: true,
      json: async () => ({
        ok: false,
        error: 'invalid_cookie',
      }),
    }));

    const { refreshToken } = require('../lib/slack-client');
    const result = await refreshToken('testworkspace', 'xoxd-f0019');

    assert.strictEqual(result.success, false);
    assert.strictEqual(result.error, 'invalid_cookie');
  });

  it('should return error on HTTP failure', async () => {
    globalThis.fetch = mock.fn(async () => ({
      ok: false,
      status: 500,
      statusText: 'Internal Server Error',
    }));

    const { refreshToken } = require('../lib/slack-client');
    const result = await refreshToken('testworkspace', 'xoxd-test');

    assert.strictEqual(result.success, false);
    assert.ok(result.error.includes('500'));
  });

  it('should return error when no api_token in response', async () => {
    globalThis.fetch = mock.fn(async () => ({
      ok: true,
      json: async () => ({ ok: true }),
    }));

    const { refreshToken } = require('../lib/slack-client');
    const result = await refreshToken('testworkspace', 'xoxd-test');

    assert.strictEqual(result.success, false);
    assert.ok(result.error.includes('No api_token'));
  });

  it('should handle network errors gracefully', async () => {
    globalThis.fetch = mock.fn(async () => {
      throw new Error('ECONNREFUSED');
    });

    const { refreshToken } = require('../lib/slack-client');
    const result = await refreshToken('testworkspace', 'xoxd-test');

    assert.strictEqual(result.success, false);
    assert.strictEqual(result.error, 'ECONNREFUSED');
  });
});

// --- updateToken tests ---

describe('updateToken', () => {
  let store;

  beforeEach(() => {
    delete require.cache[require.resolve('../lib/token-store')];
    const { TokenStore } = require('../lib/token-store');
    store = new TokenStore(CONFIG_PATH);
  });

  afterEach(() => {
    if (fs.existsSync(CONFIG_PATH)) fs.unlinkSync(CONFIG_PATH);
  });

  it('should update token while preserving other fields', () => {
    store.setWorkspace('T0123', {
      name: 'testworkspace',
      token: 'xoxc-old-token',
      cookie: 'xoxd-f0020',
    });

    const before = store.getWorkspace('T0123');
    const originalAddedAt = before.added_at;
    const originalCookie = before.cookie;

    store.updateToken('T0123', 'xoxc-new-token');

    const after = store.getWorkspace('T0123');
    assert.strictEqual(after.token, 'xoxc-new-token');
    assert.strictEqual(after.cookie, originalCookie);
    assert.strictEqual(after.name, 'testworkspace');
    assert.strictEqual(after.added_at, originalAddedAt);
    assert.strictEqual(after.status, 'connected');
  });

  it('should persist updated token to disk', () => {
    store.setWorkspace('T0123', {
      name: 'ws',
      token: 'xoxc-old',
      cookie: 'xoxd-cookie',
    });

    store.updateToken('T0123', 'xoxc-refreshed');

    // Reload from disk
    delete require.cache[require.resolve('../lib/token-store')];
    const { TokenStore } = require('../lib/token-store');
    const store2 = new TokenStore(CONFIG_PATH);
    const ws = store2.getWorkspace('T0123');

    assert.strictEqual(ws.token, 'xoxc-refreshed');
    assert.strictEqual(ws.status, 'connected');
  });

  it('should set status to connected and update last_valid', () => {
    store.setWorkspace('T0123', {
      name: 'ws',
      token: 'xoxc-old',
      cookie: 'xoxd-cookie',
    });

    // Simulate needs_reauth
    store.updateStatus('T0123', 'needs_reauth');
    assert.strictEqual(store.getWorkspace('T0123').status, 'needs_reauth');

    store.updateToken('T0123', 'xoxc-new');

    const ws = store.getWorkspace('T0123');
    assert.strictEqual(ws.status, 'connected');
    assert.ok(ws.last_valid);
  });

  it('should return false for unknown workspace', () => {
    const result = store.updateToken('TNOTEXIST', 'xoxc-new');
    assert.strictEqual(result, false);
  });
});
