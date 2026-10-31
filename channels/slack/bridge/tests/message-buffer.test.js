// tests/message-buffer.test.js
const { describe, it, beforeEach } = require('node:test');
const assert = require('node:assert');

describe('MessageBuffer', () => {
  let buffer;

  beforeEach(() => {
    delete require.cache[require.resolve('../lib/message-buffer')];
    const { MessageBuffer } = require('../lib/message-buffer');
    buffer = new MessageBuffer({ maxPerChannel: 5, maxTotal: 10 });
  });

  it('should add and retrieve messages', () => {
    buffer.add('C01', { ts: '1000.0', text: 'hello', user: 'U01' });
    buffer.add('C01', { ts: '1001.0', text: 'world', user: 'U02' });
    const msgs = buffer.getMessages('C01');
    assert.strictEqual(msgs.length, 2);
  });

  it('should filter by since timestamp', () => {
    buffer.add('C01', { ts: '1000.0', text: 'old', user: 'U01' });
    buffer.add('C01', { ts: '2000.0', text: 'new', user: 'U02' });
    const msgs = buffer.getMessages('C01', { since: '1500.0' });
    assert.strictEqual(msgs.length, 1);
    assert.strictEqual(msgs[0].text, 'new');
  });

  it('should cap per-channel messages', () => {
    for (let i = 0; i < 8; i++) {
      buffer.add('C01', { ts: `${i}.0`, text: `msg${i}`, user: 'U01' });
    }
    const msgs = buffer.getMessages('C01');
    assert.strictEqual(msgs.length, 5); // maxPerChannel
    // Should keep the newest messages
    assert.strictEqual(msgs[0].text, 'msg3');
  });

  it('should list channels with stats', () => {
    buffer.add('C01', { ts: '1.0', text: 'a', user: 'U01' });
    buffer.add('C02', { ts: '2.0', text: 'b', user: 'U02' });
    const channels = buffer.getChannelStats();
    assert.strictEqual(channels.length, 2);
  });

  it('should return total message count', () => {
    buffer.add('C01', { ts: '1.0', text: 'a', user: 'U01' });
    buffer.add('C02', { ts: '2.0', text: 'b', user: 'U02' });
    assert.strictEqual(buffer.totalMessages(), 2);
  });
});
