// tests/pairing.test.js
const { describe, it } = require('node:test');
const assert = require('node:assert');

const { adminTokensUrl, buildChromeLaunchOptions, captureViaClientBoot, extractRequestTokenPayload, extractResponseTokenPayload, extractTokenPayload, findXoxcToken, getSlackCookie, navigateToWorkspace, resolveChromeExecutable, teamIdFromRequestUrl, teamIdFromUrl, validateCapturedCredentials } = require('../auth');
const { defaultRemoteBridgeUrl, normalizeKnownArgs } = require('../pair');

describe('Slack pairing helpers', () => {
  it('preserves nginx path prefix when building admin token URL', () => {
    assert.strictEqual(
      adminTokensUrl('https://bridge.example.com/slack').toString(),
      'https://bridge.example.com/slack/admin/tokens'
    );
    assert.strictEqual(
      adminTokensUrl('http://127.0.0.1:3101').toString(),
      'http://127.0.0.1:3101/admin/tokens'
    );
  });

  it('normalizes equals-style pair arguments', () => {
    assert.deepStrictEqual(
      normalizeKnownArgs(['--workspace=acmecoai', '--push=https://example.test/slack']),
      ['--workspace', 'acmecoai', '--push', 'https://example.test/slack']
    );
  });

  it('defaults pair push URL from deploy config Slack prefix', () => {
    assert.strictEqual(defaultRemoteBridgeUrl(), 'https://bridge.example.com/slack');
  });

  it('uses installed Google Chrome when available', () => {
    const chromePath = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
    assert.strictEqual(
      resolveChromeExecutable({
        env: {},
        existsSync: candidate => candidate === chromePath,
      }),
      chromePath
    );
  });

  it('builds a normal Chrome launch config with persistent pairing profile', () => {
    const launchOptions = buildChromeLaunchOptions({
      env: {
        CHROME_EXECUTABLE_PATH: '/custom/chrome',
        SLACK_PAIRING_CHROME_USER_DATA_DIR: '/tmp/slack-profile',
      },
      existsSync: () => false,
      homeDir: '/Users/you',
    });

    assert.strictEqual(launchOptions.executablePath, '/custom/chrome');
    assert.strictEqual(launchOptions.userDataDir, '/tmp/slack-profile');
    assert.strictEqual(launchOptions.headless, false);
    assert.ok(launchOptions.args.includes('--disable-blink-features=AutomationControlled'));
    assert.deepStrictEqual(launchOptions.ignoreDefaultArgs, ['--enable-automation']);
  });

  it('recovers when Slack detaches the initial navigation frame after login', async () => {
    const visited = [];
    const initialPage = {
      goto: async (url, options) => {
        visited.push({ url, options });
        throw new Error('Navigating frame was detached');
      },
      isClosed: () => false,
      url: () => 'about:blank',
    };
    const slackPage = {
      isClosed: () => false,
      url: () => 'https://app.slack.com/client/T0FAKEIDENT003/C0FAKECH002',
    };
    let pageCalls = 0;
    const browser = {
      pages: async () => {
        pageCalls += 1;
        return pageCalls === 1 ? [initialPage] : [initialPage, slackPage];
      },
    };

    const page = await navigateToWorkspace(browser, initialPage, 'acmecoai', { settleMs: 0 });

    assert.strictEqual(page, slackPage);
    assert.deepStrictEqual(visited, [{
      url: 'https://acmecoai.slack.com/',
      options: { waitUntil: 'networkidle2' },
    }]);
  });

  it('continues when Slack never reaches network idle but the workspace page loaded', async () => {
    const initialPage = {
      goto: async () => {
        throw new Error('Navigation timeout of 30000 ms exceeded');
      },
      isClosed: () => false,
      url: () => 'https://acmecoai.slack.com/',
    };
    const browser = {
      pages: async () => [initialPage],
    };

    const page = await navigateToWorkspace(browser, initialPage, 'acmecoai', { settleMs: 0 });

    assert.strictEqual(page, initialPage);
  });

  it('does not recover through a stale Slack tab for a different workspace', async () => {
    const visited = [];
    const initialPage = {
      goto: async (url, options) => {
        visited.push({ url, options });
        if (visited.length === 1) throw new Error('Navigating frame was detached');
      },
      isClosed: () => false,
      url: () => 'about:blank',
    };
    const staleSlackPage = {
      isClosed: () => false,
      url: () => 'https://app.slack.com/client/T_WRONG/C_WRONG',
    };
    const browser = {
      pages: async () => [initialPage, staleSlackPage],
    };

    const page = await navigateToWorkspace(browser, initialPage, 'acmecoai', { settleMs: 0 });

    assert.strictEqual(page, initialPage);
    assert.deepStrictEqual(visited, [
      {
        url: 'https://acmecoai.slack.com/',
        options: { waitUntil: 'networkidle2' },
      },
      {
        url: 'https://acmecoai.slack.com/',
        options: { waitUntil: 'domcontentloaded' },
      },
    ]);
  });

  it('extracts xoxc token from Slack client.boot api_token responses', () => {
    assert.deepStrictEqual(
      extractTokenPayload({ api_token: 'xoxc-f0002', team: { id: 'T123' } }),
      { token: 'xoxc-f0002', teamId: 'T123' }
    );
    assert.deepStrictEqual(
      extractTokenPayload({ token: 'xoxc-f0004', team_id: 'T456' }),
      { token: 'xoxc-f0004', teamId: 'T456' }
    );
  });

  it('ignores response tokens whose explicit team differs from the requested workspace', () => {
    assert.deepStrictEqual(
      extractResponseTokenPayload(
        { api_token: 'xoxc-f0005', team: { id: 'T0OTHER' } },
        'T0FAKEIDENT012'
      ),
      { token: null, teamId: 'T0OTHER' }
    );
  });

  it('finds nested xoxc tokens in Slack page state blobs', () => {
    assert.strictEqual(
      findXoxcToken({ nested: { localConfig: '{\"token\":\"xoxc-f0007\"}' } }),
      'xoxc-f0007'
    );
    assert.deepStrictEqual(
      extractTokenPayload({ cache: { boot: { api: 'xoxc-f0009' } }, team_id: 'T789' }),
      { token: 'xoxc-f0009', teamId: 'T789' }
    );
  });

  it('accepts any non-empty Slack d cookie value', async () => {
    const cookie = await getSlackCookie({
      cookies: async url => (url === 'https://app.slack.com' ? [{ name: 'd', value: 'nonstandard-cookie-value' }] : []),
    }, 'acmecoai');

    assert.deepStrictEqual(cookie, { name: 'd', value: 'nonstandard-cookie-value' });
  });

  it('extracts Slack team ID from app client URL', () => {
    assert.strictEqual(
      teamIdFromUrl('https://app.slack.com/client/T0FAKEIDENT006/C0FAKECH002'),
      'T0FAKEIDENT006'
    );
    assert.strictEqual(teamIdFromUrl('https://acmecoai.slack.com/'), null);
  });

  it('extracts Slack team ID from outgoing API request route', () => {
    assert.strictEqual(
      teamIdFromRequestUrl('https://app.slack.com/api/client.counts?slack_route=T0FAKEIDENT011'),
      'T0FAKEIDENT011'
    );
    assert.strictEqual(teamIdFromRequestUrl('https://app.slack.com/api/client.counts'), null);
  });

  it('extracts xoxc token from outgoing Slack API request body', () => {
    assert.deepStrictEqual(
      extractRequestTokenPayload(
        'https://app.slack.com/api/conversations.history?slack_route=T0FAKEIDENT011',
        'token=xoxc-f0011&channel=C123',
        'T0FAKEIDENT011'
      ),
      { token: 'xoxc-f0011', teamId: 'T0FAKEIDENT011' }
    );
  });

  it('ignores outgoing Slack API tokens for a different requested team', () => {
    assert.deepStrictEqual(
      extractRequestTokenPayload(
        'https://app.slack.com/api/conversations.history?slack_route=T0OTHER',
        'token=xoxc-f0012&channel=C123',
        'T0FAKEIDENT012'
      ),
      { token: null, teamId: 'T0OTHER' }
    );
  });

  it('captures token through client.boot fallback without response interception', async () => {
    const result = await captureViaClientBoot({
      workspaceName: 'acmecoai',
      cookie: 'xoxd-cookie',
      currentTeamId: 'T_FROM_URL',
      refreshFn: async (workspaceName, cookie) => {
        assert.strictEqual(workspaceName, 'acmecoai');
        assert.strictEqual(cookie, 'xoxd-cookie');
        return { success: true, token: 'xoxc-f0014', team_id: 'T_FROM_BOOT' };
      },
    });

    assert.deepStrictEqual(result, { token: 'xoxc-f0014', teamId: 'T_FROM_BOOT' });
  });

  it('keeps polling state when client.boot fallback throws', async () => {
    const result = await captureViaClientBoot({
      workspaceName: 'acmecoai',
      cookie: 'xoxd-cookie',
      currentTeamId: 'T_FROM_URL',
      refreshFn: async () => {
        throw new Error('temporary Slack failure');
      },
    });

    assert.deepStrictEqual(result, { token: null, teamId: 'T_FROM_URL' });
  });

  it('accepts captured credentials when Slack auth validates the same team', async () => {
    const result = await validateCapturedCredentials({
      token: 'xoxc-token',
      cookie: 'xoxd-cookie',
      capturedTeamId: 'T_MATCH',
      validateFn: async () => ({ valid: true, team_id: 'T_MATCH', user: 'owner' }),
    });

    assert.deepStrictEqual(result, { valid: true, teamId: 'T_MATCH' });
  });

  it('rejects captured credentials when the token belongs to a different Slack team', async () => {
    const result = await validateCapturedCredentials({
      token: 'xoxc-token',
      cookie: 'xoxd-cookie',
      capturedTeamId: 'T_REQUESTED',
      validateFn: async () => ({ valid: true, team_id: 'T_OTHER', user: 'owner' }),
    });

    assert.strictEqual(result.valid, false);
    assert.match(result.error, /T_OTHER/);
    assert.match(result.error, /T_REQUESTED/);
  });

  it('rejects captured credentials until the loaded Slack team is known', async () => {
    const result = await validateCapturedCredentials({
      token: 'xoxc-token',
      cookie: 'xoxd-cookie',
      capturedTeamId: null,
      validateFn: async () => ({ valid: true, team_id: 'T_OTHER', user: 'owner' }),
    });

    assert.strictEqual(result.valid, false);
    assert.match(result.error, /team id/i);
  });
});
