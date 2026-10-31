// tests/sync.test.js
const { describe, it } = require('node:test');
const assert = require('node:assert');

const {
  collectWorkspaces,
  normalizeArgs,
  normalizeBridgeUrl,
  parseOptions,
  splitWorkspaces,
  syncArgs,
  urlWithPath,
  fetchHealth,
  missingDependencies,
  pairArgs,
} = require('../sync');

describe('Slack sync wrapper helpers', () => {
  it('normalizes equals-style arguments', () => {
    assert.deepStrictEqual(
      normalizeArgs(['--workspace=acmecoai,widgetco', '--days=7', '--bridge-url=https://example.test/slack']),
      ['--workspace', 'acmecoai,widgetco', '--days', '7', '--bridge-url', 'https://example.test/slack']
    );
  });

  it('collects unique workspaces from repeated and comma-separated flags', () => {
    const args = normalizeArgs([
      '--workspace', 'acmecoai,widgetco',
      '--pair=globex',
      '--workspaces', 'widgetco,samdoe',
    ]);

    assert.deepStrictEqual(
      collectWorkspaces(args),
      ['acmecoai', 'widgetco', 'globex', 'samdoe']
    );
  });

  it('builds URLs below the Slack nginx prefix', () => {
    assert.strictEqual(
      urlWithPath('https://bridge.example.com/slack', '/health'),
      'https://bridge.example.com/slack/health'
    );
    assert.strictEqual(
      urlWithPath('http://127.0.0.1:3101', '/health'),
      'http://127.0.0.1:3101/health'
    );
  });

  it('builds bi_sync_slack arguments', () => {
    assert.deepStrictEqual(syncArgs({ days: '7', dryRun: true }), [
      'execution/infrastructure/bi_sync_slack.py',
      '--days',
      '7',
      '--dry-run',
    ]);
  });

  it('defaults to deploy-config bridge URL instead of stale SLACK_BRIDGE_URL', () => {
    const options = parseOptions(['--dry-run'], {
      SLACK_BRIDGE_URL: 'https://old-host.example/slack',
    });

    assert.strictEqual(options.bridgeUrl, 'https://bridge.example.com/slack');
    assert.strictEqual(options.dryRun, true);
    assert.strictEqual(options.days, '3');
  });

  it('allows explicit bridge URL override and strips trailing slash for Python sync', () => {
    const options = parseOptions(['--bridge-url', 'https://example.test/slack/', '--days', '14'], {});

    assert.strictEqual(options.bridgeUrl, 'https://example.test/slack');
    assert.strictEqual(options.days, '14');
  });

  it('fails fast when value-required flags are missing values', () => {
    assert.throws(() => parseOptions(['--bridge-url'], {}), /--bridge-url requires a value/);
    assert.throws(() => parseOptions(['--days'], {}), /--days requires a value/);
  });

  it('normalizes bridge URLs consistently', () => {
    assert.strictEqual(normalizeBridgeUrl('https://example.test/slack///'), 'https://example.test/slack');
  });

  it('splits comma-separated workspace values safely', () => {
    assert.deepStrictEqual(splitWorkspaces(' a, ,b,c '), ['a', 'b', 'c']);
  });

  it('finds that puppeteer is missing before pairing ("Cannot find module puppeteer", 2026-10-13)', () => {
    const present = new Set(['/b/node_modules/puppeteer/package.json']);
    assert.strictEqual(missingDependencies('/b', p => present.has(p)), false);
    assert.strictEqual(missingDependencies('/b', () => false), true);
  });

  it('sends the API key to /health, so the summary lists the paired workspaces', async () => {
    let seen = null;
    const fakeFetch = async (url, init) => {
      seen = { url, key: init && init.headers && init.headers['X-Api-Key'] };
      return { ok: true, json: async () => ({ status: 'ok', workspaces: [] }) };
    };
    await fetchHealth('https://example.test/slack', fakeFetch, 'k123');
    assert.deepStrictEqual(seen, { url: 'https://example.test/slack/health', key: 'k123' });
  });

  it('passes --sign-in through to pair.js only when asked', () => {
    assert.deepStrictEqual(
      pairArgs('/b', 'acme', 'https://example.test/slack', { signIn: false }),
      ['/b/pair.js', '--workspace', 'acme', '--push', 'https://example.test/slack']
    );
    assert.deepStrictEqual(
      pairArgs('/b', 'acme', 'https://example.test/slack', { signIn: true }).slice(-1),
      ['--sign-in']
    );
    assert.strictEqual(parseOptions(['--workspace', 'acme', '--sign-in'], { REMOTE_BRIDGE_URL: 'https://x.test' }).signIn, true);
  });
});
