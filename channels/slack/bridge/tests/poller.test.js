// tests/poller.test.js
const { describe, it } = require('node:test');
const assert = require('node:assert');

const { WorkspacePoller } = require('../lib/poller');

function makePoller(historyImpl, extra = {}) {
  const addedBatches = [];
  const poller = new WorkspacePoller({
    teamId: 'T1',
    name: 'test',
    token: 'xoxc-test',
    cookie: 'cookie-test',
    buffer: {
      addBatch(channelId, messages) {
        addedBatches.push({ channelId, messages });
      },
    },
    ...extra,
  });
  poller.client = {
    conversations: {
      history: historyImpl,
      list: async () => ({ channels: [] }),
    },
  };
  poller._testAddedBatches = addedBatches;
  poller.channels = [{ id: 'C1', name: 'one' }, { id: 'C2', name: 'two' }];
  poller.lastChannelRefresh = Date.now();
  poller._defaultCursor = '0';
  return poller;
}

function invalidAuthError() {
  const err = new Error('An API error occurred: invalid_auth');
  err.data = { error: 'invalid_auth' };
  return err;
}

function rateLimitedError() {
  // Shape thrown by @slack/web-api once retryConfig retries are exhausted.
  const err = new Error('A rate limit was exceeded');
  err.code = 'slack_webapi_rate_limited_error';
  err.retryAfter = 30;
  return err;
}

describe('WorkspacePoller history fetch', () => {
  it('surfaces invalid_auth to poll-level auth handling', async () => {
    const poller = makePoller(async () => { throw invalidAuthError(); });

    await assert.rejects(() => poller._fetchHistory('C1', '0'), /invalid_auth/);
  });

  it('still throws unexpected history errors', async () => {
    const err = new Error('Slack exploded');
    err.data = { error: 'server_error' };
    const poller = makePoller(async () => { throw err; });

    await assert.rejects(() => poller._fetchHistory('C1', '0'), /Slack exploded/);
  });

  it('skips isolated invalid_auth channel failures when another channel succeeds', async () => {
    const poller = makePoller(async ({ channel }) => {
      if (channel === 'C2') throw invalidAuthError();
      return { messages: [{ ts: '1.0', text: 'hello' }] };
    });

    await poller._poll();

    assert.strictEqual(poller.errorCount, 0);
    assert.strictEqual(poller.pollCount, 1);
    assert.deepStrictEqual(poller._testAddedBatches.map(batch => batch.channelId), ['C1']);
  });

  it('treats all-channel invalid_auth as workspace auth failure', async () => {
    let refreshCalls = 0;
    const poller = makePoller(
      async () => { throw invalidAuthError(); },
      {
        refreshFn: async () => {
          refreshCalls += 1;
          return { success: false, error: 'still invalid' };
        },
      }
    );

    await poller._poll();

    assert.strictEqual(refreshCalls, 1);
    assert.strictEqual(poller.errorCount, 1);
    assert.strictEqual(poller.pollCount, 0);
  });

  it('does not count Slack rate-limit errors toward the reauth threshold', async () => {
    const statusChanges = [];
    let stopped = false;
    const poller = makePoller(async () => { throw rateLimitedError(); }, {
      onStatusChange: (tid, status) => statusChanges.push(status),
    });
    poller.stop = () => { stopped = true; };

    // Poll well past the 5-consecutive-error safeguard.
    for (let i = 0; i < 8; i += 1) await poller._poll();

    assert.strictEqual(poller.errorCount, 0, 'rate-limits must not increment errorCount');
    assert.ok(!statusChanges.includes('needs_reauth'), 'rate-limits must not flip a workspace to needs_reauth');
    assert.strictEqual(stopped, false, 'rate-limits must not stop the poller');
  });

  it('still trips the reauth safeguard after 5 genuine (non-rate-limit) errors', async () => {
    const statusChanges = [];
    let stopped = false;
    const err = new Error('Slack exploded');
    err.data = { error: 'server_error' };
    const poller = makePoller(async () => { throw err; }, {
      onStatusChange: (tid, status) => statusChanges.push(status),
    });
    poller.stop = () => { stopped = true; };

    for (let i = 0; i < 5; i += 1) await poller._poll();

    assert.strictEqual(poller.errorCount, 5);
    assert.ok(statusChanges.includes('needs_reauth'));
    assert.strictEqual(stopped, true);
  });

  it('does not re-issue conversations.list on every poll while it is rate-limited', async () => {
    // Regression: lastChannelRefresh used to advance only after a SUCCESSFUL
    // refresh, so a sustained 429 made every poll call conversations.list again.
    let listCalls = 0;
    const poller = makePoller(async () => ({ messages: [] }));
    poller.client.conversations.list = async () => {
      listCalls += 1;
      throw rateLimitedError();
    };
    poller.lastChannelRefresh = 0; // force a refresh on the first poll

    for (let i = 0; i < 20; i += 1) await poller._poll();

    assert.strictEqual(listCalls, 1, 'a failed refresh must not be retried on the very next poll');
    assert.strictEqual(poller.channelRefreshBackoffMs, 15 * 60 * 1000, 'first failure stacks one backoff step');
    // The whole point of the fix: history polls keep succeeding through a failing
    // channel refresh, instead of the workspace being starved entirely.
    assert.strictEqual(poller.pollCount, 19, 'polls after the failed refresh must still complete');
    // ...and the stale channel map stays visible even though polls are succeeding.
    assert.strictEqual(poller.channelRefreshFailures, 1);
    assert.ok(poller.lastChannelRefreshFailure, 'last_channel_refresh_failure must be set');
  });

  it('retries the channel refresh once the bounded backoff elapses, and caps it', async () => {
    let listCalls = 0;
    const poller = makePoller(async () => ({ messages: [] }));
    poller.client.conversations.list = async () => {
      listCalls += 1;
      throw rateLimitedError();
    };
    poller.lastChannelRefresh = 0;

    // Each iteration pretends the full window has elapsed.
    for (let i = 0; i < 12; i += 1) {
      poller.lastChannelRefresh = Date.now() - (poller.channelRefreshMs + poller.channelRefreshBackoffMs + 1);
      await poller._poll();
    }

    assert.strictEqual(listCalls, 12, 'refresh must still be retried every window — never stale forever');
    assert.ok(
      poller.channelRefreshBackoffMs <= poller.maxChannelRefreshBackoffMs,
      'backoff must stay capped',
    );
  });

  it('clears the rate-limit state and backoff once a poll succeeds', async () => {
    let failList = true;
    const poller = makePoller(async () => ({ messages: [] }));
    poller.client.conversations.list = async () => {
      if (failList) throw rateLimitedError();
      return { channels: [] };
    };
    poller.lastChannelRefresh = 0;

    await poller._poll();
    assert.ok(poller.rateLimitedSince, 'precondition: rate-limited');

    failList = false;
    poller.lastChannelRefresh = 0;
    await poller._poll();

    assert.strictEqual(poller.rateLimitedSince, null);
    assert.strictEqual(poller.consecutiveRateLimits, 0);
    assert.strictEqual(poller.channelRefreshBackoffMs, 0);
    assert.strictEqual(poller.channelRefreshFailures, 0);
    assert.strictEqual(poller.lastChannelRefreshFailure, null);
  });

  it('serialises overlapping polls and always releases the in-flight latch', async () => {
    let release;
    const gate = new Promise((r) => { release = r; });
    let historyCalls = 0;
    const poller = makePoller(async () => {
      historyCalls += 1;
      await gate;
      return { messages: [] };
    });

    const first = poller._poll();
    // These fire while the first cycle is still awaiting Slack.
    await poller._poll();
    await poller._poll();
    assert.strictEqual(poller.skippedPolls, 2, 'overlapping polls must be skipped, not stacked');

    release();
    await first;
    assert.strictEqual(poller._pollInFlight, false, 'latch must clear after a normal cycle');
    assert.strictEqual(historyCalls, 2, 'only the first cycle reached Slack (2 channels)');
  });

  it('releases the in-flight latch even when a poll cycle throws', async () => {
    const poller = makePoller(async () => ({ messages: [] }));
    poller._pollOnce = async () => { throw new Error('boom'); };

    await assert.rejects(() => poller._poll(), /boom/);

    assert.strictEqual(poller._pollInFlight, false, 'a throw must not deadlock the poller');

    // And the poller is still usable afterwards.
    poller._pollOnce = async () => 'ok';
    assert.strictEqual(await poller._poll(), 'ok');
  });

  it('scopes Slack API reads to the workspace team ID', async () => {
    const calls = { list: null, history: null, user: null };
    const poller = makePoller(async (opts) => {
      calls.history = opts;
      return { messages: [] };
    });
    poller.client = {
      conversations: {
        list: async (opts) => {
          calls.list = opts;
          return { channels: [{ id: 'C1', name: 'one', is_member: true }] };
        },
        history: poller.client.conversations.history,
      },
      users: {
        info: async (opts) => {
          calls.user = opts;
          return { user: { id: opts.user, name: 'user' } };
        },
      },
    };

    await poller._refreshChannels();
    await poller._fetchHistory('C1', '0');
    await poller._resolveUser('U1');

    assert.strictEqual(calls.list.team_id, 'T1');
    assert.strictEqual(calls.history.team_id, 'T1');
    assert.strictEqual(calls.user.team_id, 'T1');
  });
});
