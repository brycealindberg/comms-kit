// tests/security-limits.test.js
const { describe, it } = require('node:test');
const assert = require('node:assert');

const {
  buildHealthPayload,
  isDetailedHealthAuthorized,
  createBoundedMap,
  createConcurrencyGate,
  createSlidingWindowLimiter,
  runLimitedWork,
  splitTranscriptionWork,
  admitSend,
} = require('../lib/security-limits');

describe('admitSend', () => {
  const pollerWith = (name, ...channels) => ({ name, channelMap: new Map(channels.map(c => [c, {}])) });
  const makeLimiter = (max = 2, clock = { t: 0 }) =>
    createSlidingWindowLimiter({ max, windowMs: 60_000, now: () => clock.t });

  it('does not spend the send limit on a channel no workspace knows', () => {
    const pollers = new Map([['T1', pollerWith('main', 'D1')]]);
    const limiter = makeLimiter(2);

    for (let i = 0; i < 25; i++) {
      assert.strictEqual(admitSend({ pollers, channel: 'NOPE', limiter }).status, 404);
    }
    const real = admitSend({ pollers, channel: 'D1', limiter });
    assert.strictEqual(real.status, 200);
    assert.strictEqual(real.poller.name, 'main');
  });

  it('still answers 429 once real sends pass the limit', () => {
    const pollers = new Map([['T1', pollerWith('main', 'D1')]]);
    const limiter = makeLimiter(2);

    assert.strictEqual(admitSend({ pollers, channel: 'D1', limiter }).status, 200);
    assert.strictEqual(admitSend({ pollers, channel: 'D1', limiter }).status, 200);
    assert.strictEqual(admitSend({ pollers, channel: 'D1', limiter }).status, 429);
  });

  it('frees the slots when the window passes', () => {
    const clock = { t: 0 };
    const pollers = new Map([['T1', pollerWith('main', 'D1')]]);
    const limiter = makeLimiter(1, clock);

    assert.strictEqual(admitSend({ pollers, channel: 'D1', limiter }).status, 200);
    assert.strictEqual(admitSend({ pollers, channel: 'D1', limiter }).status, 429);
    clock.t = 60_001;
    assert.strictEqual(admitSend({ pollers, channel: 'D1', limiter }).status, 200);
  });

  it('answers 404 without a slot when the channel is only in another workspace', () => {
    const pollers = new Map([['T1', pollerWith('main', 'D1')], ['T2', pollerWith('other', 'C2')]]);
    const limiter = makeLimiter(1);

    assert.strictEqual(admitSend({ pollers, channel: 'C2', workspace: 'main', limiter }).status, 404);
    assert.strictEqual(admitSend({ pollers, channel: 'D1', workspace: 'main', limiter }).status, 200);
  });

  it('finds the channel by team id or by workspace name', () => {
    const pollers = new Map([['T1', pollerWith('main', 'D1')], ['T2', pollerWith('other', 'C2')]]);
    const limiter = makeLimiter(5);

    assert.strictEqual(admitSend({ pollers, channel: 'C2', workspace: 'T2', limiter }).poller.name, 'other');
    assert.strictEqual(admitSend({ pollers, channel: 'C2', workspace: 'other', limiter }).poller.name, 'other');
    assert.strictEqual(admitSend({ pollers, channel: 'C2', limiter }).poller.name, 'other');
  });
});

describe('security limit helpers', () => {
  it('hides detailed workspace data from public health payloads', () => {
    const base = {
      uptime_seconds: 123,
      total_messages: 7,
      workspaces: [{ name: 'client-workspace', team_id: 'T1', channels: 10 }],
    };

    assert.deepStrictEqual(buildHealthPayload(base, { detailed: false }), {
      status: 'ok',
      uptime_seconds: 123,
    });
    assert.deepStrictEqual(buildHealthPayload(base, { detailed: true }), {
      status: 'ok',
      uptime_seconds: 123,
      total_messages: 7,
      workspaces: base.workspaces,
    });
  });

  it('authorizes detailed health only with a non-empty matching API key', () => {
    const compare = (provided, configured) => provided === configured;

    assert.strictEqual(isDetailedHealthAuthorized(undefined, '', compare), false);
    assert.strictEqual(isDetailedHealthAuthorized('', '', compare), false);
    assert.strictEqual(isDetailedHealthAuthorized('expected-key', 'expected-key', compare), true);
    assert.strictEqual(isDetailedHealthAuthorized('wrong-key', 'expected-key', compare), false);
  });

  it('limits uncached bulk transcription work while preserving cached results', () => {
    const items = [
      { file: { id: 'A' } },
      { file: { id: 'B' } },
      { file: { id: 'C' } },
      { file: { id: 'D' } },
    ];

    const split = splitTranscriptionWork(items, {
      maxNew: 2,
      isCached: item => item.file.id === 'B',
    });

    assert.deepStrictEqual(split.cached.map(item => item.file.id), ['B']);
    assert.deepStrictEqual(split.toProcess.map(item => item.file.id), ['A', 'C']);
    assert.deepStrictEqual(split.skipped.map(item => item.file.id), ['D']);
    assert.strictEqual(split.total, 4);
    assert.strictEqual(split.maxNew, 2);
  });

  it('enforces sliding-window admission before paid transcription work', () => {
    let now = 1000;
    const limiter = createSlidingWindowLimiter({ max: 2, windowMs: 60_000, now: () => now });

    assert.deepStrictEqual(limiter.tryRemoveToken(), { ok: true, remaining: 1 });
    assert.deepStrictEqual(limiter.tryRemoveToken(), { ok: true, remaining: 0 });
    assert.strictEqual(limiter.tryRemoveToken().ok, false);

    now += 60_001;
    assert.deepStrictEqual(limiter.tryRemoveToken(), { ok: true, remaining: 1 });
  });

  it('fails fast when the transcription concurrency gate is full', async () => {
    const gate = createConcurrencyGate(1);
    let releaseFirst;
    const first = gate.run(() => new Promise(resolve => { releaseFirst = resolve; }));

    await assert.rejects(
      gate.run(async () => 'second'),
      /too many transcription requests/
    );

    releaseFirst('first');
    assert.strictEqual(await first, 'first');
    assert.strictEqual(await gate.run(async () => 'after'), 'after');
  });

  it('does not burn rate tokens when concurrency is already full', async () => {
    let now = 1000;
    const limiter = createSlidingWindowLimiter({ max: 1, windowMs: 60_000, now: () => now });
    const gate = createConcurrencyGate(1);
    let releaseFirst;
    const first = gate.run(() => new Promise(resolve => { releaseFirst = resolve; }));

    await assert.rejects(
      runLimitedWork({ limiter, gate }, async () => 'blocked'),
      /too many transcription requests/
    );

    releaseFirst('first');
    assert.strictEqual(await first, 'first');
    assert.strictEqual(await runLimitedWork({ limiter, gate }, async () => 'allowed'), 'allowed');
    assert.strictEqual(limiter.tryRemoveToken().ok, false);

    now += 60_001;
    assert.strictEqual(await runLimitedWork({ limiter, gate }, async () => 'next-window'), 'next-window');
  });

  it('evicts old transcription cache entries instead of growing without bound', () => {
    const cache = createBoundedMap(2);
    cache.set('A', 'one');
    cache.set('B', 'two');
    cache.set('C', 'three');

    assert.strictEqual(cache.get('A'), undefined);
    assert.strictEqual(cache.get('B'), 'two');
    assert.strictEqual(cache.get('C'), 'three');
    assert.strictEqual(cache.size, 2);
  });
});
