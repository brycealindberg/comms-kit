// tests/slack-client.test.js
const { describe, it } = require('node:test');
const assert = require('node:assert');

describe('createSlackClient', () => {
  it('should create a WebClient with xoxc token', () => {
    const { createSlackClient } = require('../lib/slack-client');
    const client = createSlackClient('xoxc-test', 'xoxd-test');
    assert.ok(client);
    assert.strictEqual(typeof client.conversations, 'object');
  });

  it('should expose a validateToken function', () => {
    const { validateToken } = require('../lib/slack-client');
    assert.strictEqual(typeof validateToken, 'function');
  });
});
