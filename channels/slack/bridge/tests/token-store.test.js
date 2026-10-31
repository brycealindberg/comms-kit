// tests/token-store.test.js
const { describe, it, beforeEach, afterEach } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

// node --test runs test files at once, so each file needs its own config file.
const CONFIG_PATH = path.join(os.tmpdir(), `token-store-test-${process.pid}.json`);

// Set env before requiring module
process.env.ENCRYPTION_KEY = 'a]4f9b2c1d8e7f6a5b4c3d2e1f0a9b8c7d6e5f4a3b2c1d0e';

describe('TokenStore', () => {
  let store;

  beforeEach(() => {
    delete require.cache[require.resolve('../lib/token-store')];
    const { TokenStore } = require('../lib/token-store');
    store = new TokenStore(CONFIG_PATH);
  });

  afterEach(() => {
    if (fs.existsSync(CONFIG_PATH)) fs.unlinkSync(CONFIG_PATH);
  });

  it('keeps the token file private, also when an older file was readable by others', () => {
    const entry = { name: 'testworkspace', token: 'xoxc-f0023', cookie: 'xoxd-f0024' };
    store.setWorkspace('T0123', entry);
    assert.strictEqual(fs.statSync(CONFIG_PATH).mode & 0o777, 0o600);
    fs.chmodSync(CONFIG_PATH, 0o644);
    store.setWorkspace('T0456', entry);
    assert.strictEqual(fs.statSync(CONFIG_PATH).mode & 0o777, 0o600);
  });

  it('should save and load workspace tokens', () => {
    store.setWorkspace('T0123', {
      name: 'testworkspace',
      token: 'xoxc-f0023',
      cookie: 'xoxd-f0024',
    });

    delete require.cache[require.resolve('../lib/token-store')];
    const { TokenStore } = require('../lib/token-store');
    const store2 = new TokenStore(CONFIG_PATH);
    const ws = store2.getWorkspace('T0123');

    assert.strictEqual(ws.name, 'testworkspace');
    assert.strictEqual(ws.token, 'xoxc-f0023');
    assert.strictEqual(ws.cookie, 'xoxd-f0024');
    assert.strictEqual(ws.status, 'connected');
  });

  it('should list all workspaces', () => {
    store.setWorkspace('T001', { name: 'ws1', token: 't1', cookie: 'c1' });
    store.setWorkspace('T002', { name: 'ws2', token: 't2', cookie: 'c2' });

    const all = store.listWorkspaces();
    assert.strictEqual(all.length, 2);
    assert.deepStrictEqual(all.map(w => w.name).sort(), ['ws1', 'ws2']);
  });

  it('should encrypt tokens on disk', () => {
    store.setWorkspace('T0123', {
      name: 'encrypted',
      token: 'xoxc-secret',
      cookie: 'xoxd-secret',
    });

    const raw = fs.readFileSync(CONFIG_PATH, 'utf-8');
    assert.ok(!raw.includes('xoxc-secret'));
    assert.ok(!raw.includes('xoxd-secret'));
  });

  it('should update workspace status', () => {
    store.setWorkspace('T0123', { name: 'ws', token: 't', cookie: 'c' });
    store.updateStatus('T0123', 'needs_reauth');

    const ws = store.getWorkspace('T0123');
    assert.strictEqual(ws.status, 'needs_reauth');
  });

  it('should return null for unknown workspace', () => {
    assert.strictEqual(store.getWorkspace('TNOTEXIST'), null);
  });
});
