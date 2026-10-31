#!/usr/bin/env node
// auth.js — Captures Slack session tokens via Puppeteer browser login
//
// Usage:
//   node auth.js --workspace stellardigital
//   node auth.js --workspace stellardigital --push https://bridge-host/slack

const http = require('http');
const https = require('https');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { isDesktopHandoff, pairingTimeoutMs, parseWorkspaceList, resolveSlug, siblingsNeedingRefresh, waitTimeoutReason } = require('./lib/pairing-session');

const DEFAULT_CHROME_PATHS = [
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta',
  '/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary',
];

function resolveChromeExecutable({ env = process.env, existsSync = fs.existsSync } = {}) {
  if (env.CHROME_EXECUTABLE_PATH) return env.CHROME_EXECUTABLE_PATH;
  return DEFAULT_CHROME_PATHS.find(candidate => existsSync(candidate)) || null;
}

function buildChromeLaunchOptions({
  env = process.env,
  existsSync = fs.existsSync,
  homeDir = os.homedir(),
} = {}) {
  const executablePath = resolveChromeExecutable({ env, existsSync });
  const userDataDir = env.SLACK_PAIRING_CHROME_USER_DATA_DIR
    || path.join(homeDir, '.slack-bridge-chrome-profile');

  const options = {
    headless: false,
    defaultViewport: null,
    userDataDir,
    ignoreDefaultArgs: ['--enable-automation'],
    args: [
      '--window-size=1200,800',
      '--disable-blink-features=AutomationControlled',
    ],
  };

  if (executablePath) options.executablePath = executablePath;
  return options;
}

function adminTokensUrl(baseUrl) {
  const url = new URL(baseUrl);
  const prefix = url.pathname.replace(/\/+$/, '');
  url.pathname = `${prefix}/admin/tokens`.replace(/\/{2,}/g, '/');
  url.search = '';
  url.hash = '';
  return url;
}

function findXoxcToken(value, seen = new Set()) {
  if (typeof value === 'string') {
    const match = value.match(/xoxc-[A-Za-z0-9-]+/);
    return match ? match[0] : null;
  }
  if (!value || typeof value !== 'object' || seen.has(value)) return null;
  seen.add(value);

  for (const item of Array.isArray(value) ? value : Object.values(value)) {
    const token = findXoxcToken(item, seen);
    if (token) return token;
  }
  return null;
}

function extractTokenPayload(body) {
  const directToken = body?.api_token || body?.token || null;
  const token = (typeof directToken === 'string' && directToken.startsWith('xoxc-'))
    ? directToken
    : findXoxcToken(body);
  const teamId = body?.team?.id || body?.team_id || body?.team?.team_id || null;
  if (token) {
    return { token, teamId };
  }
  return { token: null, teamId };
}

function extractResponseTokenPayload(body, expectedTeamId = null) {
  const payload = extractTokenPayload(body);
  if (expectedTeamId && payload.teamId && expectedTeamId !== payload.teamId) {
    return { token: null, teamId: payload.teamId };
  }
  return payload;
}

function teamIdFromUrl(url) {
  const match = String(url || '').match(/\/client\/(T[A-Z0-9]+)/i);
  return match ? match[1] : null;
}

function teamIdFromRequestUrl(url) {
  try {
    const route = new URL(String(url || '')).searchParams.get('slack_route');
    return /^T[A-Z0-9]+$/i.test(route || '') ? route : null;
  } catch (_) {
    return null;
  }
}

function extractRequestTokenPayload(url, postData, expectedTeamId = null) {
  const teamId = teamIdFromRequestUrl(url);
  if (expectedTeamId && teamId && expectedTeamId !== teamId) {
    return { token: null, teamId };
  }
  return {
    token: findXoxcToken(postData || ''),
    teamId,
  };
}

async function getSlackCookie(page, workspaceName) {
  const urls = [
    `https://${workspaceName}.slack.com`,
    'https://app.slack.com',
    'https://slack.com',
  ];
  const batches = await Promise.all(urls.map(async url => {
    try {
      return await page.cookies(url);
    } catch (_) {
      return [];
    }
  }));
  return batches.flat().find(cookie => (
    cookie.name === 'd'
    && typeof cookie.value === 'string'
    && cookie.value.length > 0
  )) || null;
}

async function captureViaClientBoot({ workspaceName, cookie, currentTeamId = null, refreshFn }) {
  try {
    const result = await refreshFn(workspaceName, cookie);
    if (result?.success && result.token) {
      return {
        token: result.token,
        teamId: result.team_id || currentTeamId,
      };
    }
  } catch (_) {
    // Keep polling; response interception or a later client.boot retry may still work.
  }
  return { token: null, teamId: currentTeamId };
}

async function validateCapturedCredentials({ token, cookie, capturedTeamId, validateFn }) {
  if (!token || !cookie) {
    return { valid: false, error: 'token and cookie are required' };
  }
  if (!capturedTeamId) {
    return { valid: false, error: 'captured Slack team id is required before saving credentials' };
  }


  const validation = await validateFn(token, cookie);
  if (!validation.valid) {
    return { valid: false, error: validation.error || 'auth.test failed' };
  }

  const validatedTeamId = validation.team_id || null;
  if (capturedTeamId && validatedTeamId && capturedTeamId !== validatedTeamId) {
    return {
      valid: false,
      error: `Captured token is for ${validatedTeamId}, but the loaded Slack page is ${capturedTeamId}`,
    };
  }

  return { valid: true, teamId: capturedTeamId };
}

function expectedTeamIdForWorkspace(workspaceName) {
  try {
    const { TokenStore } = require('./lib/token-store');
    const store = new TokenStore();
    return store.listWorkspaces().find(ws => ws.name === workspaceName)?.team_id || null;
  } catch (_) {
    return null;
  }
}

function isFrameDetachedNavigationError(error) {
  return /Navigating frame was detached|frame was detached/i.test(error?.message || '');
}

function isNavigationTimeoutError(error) {
  return /Navigation timeout/i.test(error?.message || '');
}

function isRecoverableSlackNavigationError(error) {
  return isFrameDetachedNavigationError(error) || isNavigationTimeoutError(error);
}

function isPageClosed(page) {
  return typeof page?.isClosed === 'function' && page.isClosed();
}

function isSlackPage(page) {
  if (!page || isPageClosed(page) || typeof page.url !== 'function') return false;
  return /\.slack\.com\b/i.test(page.url());
}

function pageHostname(page) {
  if (!page || typeof page.url !== 'function') return '';
  try {
    return new URL(page.url()).hostname.toLowerCase();
  } catch (_) {
    return '';
  }
}

function isRequestedWorkspacePage(page, workspaceName) {
  return pageHostname(page) === `${workspaceName.toLowerCase()}.slack.com`;
}

function findRecoveredSlackPage(pages, workspaceName, pagesBefore) {
  return pages.find(candidate => isRequestedWorkspacePage(candidate, workspaceName))
    || pages.find(candidate => !pagesBefore.has(candidate) && isSlackPage(candidate))
    || null;
}

async function navigateToWorkspace(browser, page, workspaceName, { settleMs = 2000 } = {}) {
  const targetUrl = `https://${workspaceName}.slack.com/`;
  const pagesBefore = new Set(await browser.pages());
  try {
    await page.goto(targetUrl, { waitUntil: 'networkidle2' });
    return page;
  } catch (error) {
    if (!isRecoverableSlackNavigationError(error)) throw error;
    if (isRequestedWorkspacePage(page, workspaceName)) return page;
  }

  console.log('Slack changed or slowed the active login tab; continuing with the loaded Slack page...');
  if (settleMs > 0) {
    await new Promise(resolve => setTimeout(resolve, settleMs));
  }

  const pages = await browser.pages();
  const slackPage = findRecoveredSlackPage(pages, workspaceName, pagesBefore);
  if (slackPage) return slackPage;

  const fallbackPage = pages.find(candidate => !isPageClosed(candidate)) || page;
  await fallbackPage.goto(targetUrl, { waitUntil: 'domcontentloaded' });
  return fallbackPage;
}
const args = process.argv.slice(2);
const workspaceIdx = args.indexOf('--workspace');
const pushIdx = args.indexOf('--push');

const workspace = workspaceIdx >= 0 ? args[workspaceIdx + 1] : null;
const pushUrl = pushIdx >= 0 ? args[pushIdx + 1] : process.env.REMOTE_BRIDGE_URL || null;
const signInFirst = args.includes('--sign-in');

if (!workspace && require.main === module) {
  console.error('Usage: node auth.js --workspace <slack-workspace-name>');
  console.error('Example: node auth.js --workspace stellardigital');
  process.exit(1);
}

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const SIBLING_WAIT_MS = 60 * 1000;
const UNKNOWN_WORKSPACE_EXIT = 3;

function clockIn(ms) {
  return new Date(Date.now() + ms).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', timeZoneName: 'short' });
}

function fingerprint(value) {
  return require('crypto').createHash('sha256').update(String(value)).digest('hex').slice(0, 8);
}

// Each workspace link with the text of its row: Slack puts the name beside the "Open" button,
// not inside it. A row stops growing at the first parent that holds a second workspace's link.
async function readWorkspaceAnchors(page) {
  return page.evaluate(() => {
    const isWorkspaceLink = href => /\/ssb\/redirect|\/t\/[^/]+\/login\//.test(href);
    return [...document.querySelectorAll('a[href]')].filter(a => isWorkspaceLink(a.href)).map(a => {
      let row = a;
      while (row.parentElement) {
        const hrefs = new Set([...row.parentElement.querySelectorAll('a[href]')]
          .map(x => x.href.split('?')[0]).filter(isWorkspaceLink));
        if (hrefs.size > 1) break;
        row = row.parentElement;
      }
      return { href: a.href, text: row.innerText || '' };
    });
  }).catch(() => []);
}

// Before an email sign-in, slack.com/signin lists the workspaces this window is signed in to;
// after one ("Welcome back!"), every workspace of that email. Empty when it lists none.
async function readWorkspaceList(page, { navigate = true } = {}) {
  if (navigate) {
    try {
      await page.goto('https://slack.com/signin', { waitUntil: 'networkidle2', timeout: 30000 });
    } catch (error) {
      if (!isRecoverableSlackNavigationError(error)) throw error;
    }
  }
  for (let attempt = 0; attempt < (navigate ? 8 : 1); attempt += 1) {
    const list = parseWorkspaceList(await readWorkspaceAnchors(page));
    if (list.length > 0) return list;
    if (navigate) await sleep(1000);
  }
  return [];
}

function printWorkspaceList(list) {
  for (const ws of list) console.error(`  ${ws.slug.padEnd(28)} ${ws.name}`);
}

// The name is not among the workspaces this window is signed in to. Today that was the normal
// case for a new workspace: sign in by email in the window, and the full list appears.
// On a timeout it returns { seen }: the longest list any tab showed, so the caller can tell
// "nobody signed in" from "signed in, but the name is not there".
async function waitForWorkspaceInList(browser, wanted, maxWaitMs) {
  const deadline = Date.now() + maxWaitMs;
  let seen = [];
  while (Date.now() < deadline) {
    for (const candidate of await browser.pages()) {
      if (isPageClosed(candidate)) continue;
      const list = await readWorkspaceList(candidate, { navigate: false });
      if (list.length > seen.length) seen = list;
      const resolved = resolveSlug(wanted, list);
      if (resolved.slug || resolved.ambiguous) return { ...resolved, page: candidate, list };
    }
    await sleep(3000);
  }
  return { slug: null, seen };
}

// A workspace listed with a one-time sign-in link opens through that link, in this tab. The
// link is held only in this function, never printed or stored.
async function followSignInLink(page, slug) {
  const href = await page.evaluate(wanted => {
    const link = [...document.querySelectorAll('a[href]')]
      .find(a => a.href.startsWith(`https://app.slack.com/t/${wanted}/login/`));
    return link ? link.href : null;
  }, slug).catch(() => null);
  if (!href) return false;
  try {
    await page.goto(href, { waitUntil: 'domcontentloaded', timeout: 30000 });
  } catch (error) {
    if (!isRecoverableSlackNavigationError(error)) throw error;
  }
  return true;
}

async function leaveDesktopHandoff(pages) {
  for (const candidate of pages) {
    if (isPageClosed(candidate) || !isDesktopHandoff(candidate.url())) continue;
    const clicked = await candidate.evaluate(() => {
      const link = [...document.querySelectorAll('a')]
        .find(a => /use slack in (your|the) browser/i.test(a.textContent || ''));
      if (!link) return false;
      link.click();
      return true;
    }).catch(() => false);
    if (clicked) {
      console.log('Slack offered the desktop app; chose "use Slack in your browser".');
      return candidate;
    }
  }
  return null;
}

// Waits until this workspace's token and the profile's d cookie validate together.
// Returns { token, cookie, teamId, page } or null when maxWaitMs runs out.
async function captureSession({ browser, page, workspaceName, expectedTeamId = null, maxWaitMs }) {
  let token = null;
  let cookie = null;
  let teamId = expectedTeamId;
  let announced = false;
  const watched = new Set();
  const rejectedTokens = new Set();

  const takeToken = (extracted, source) => {
    if (!extracted.token || rejectedTokens.has(extracted.token)) return;
    token = extracted.token;
    teamId = extracted.teamId || teamId;
    if (!announced) {
      announced = true;
      console.log(`Captured a session token from ${source} for team ${teamId || 'unknown'}`);
    }
  };
  const onRequest = request => {
    const url = request.url();
    if (!url.includes('/api/')) return;
    const postData = typeof request.postData === 'function' ? request.postData() : '';
    takeToken(extractRequestTokenPayload(url, postData, teamId), 'a Slack API request');
  };
  const onResponse = async response => {
    const url = response.url();
    if (!url.includes('/api/client.boot') && !url.includes('/api/client.counts')) return;
    try {
      takeToken(extractResponseTokenPayload(await response.json(), teamId), 'client.boot');
    } catch (_) {
      // Not JSON; the other paths still run.
    }
  };
  const watch = candidate => {
    if (!candidate || watched.has(candidate)) return;
    watched.add(candidate);
    candidate.on('request', onRequest);
    candidate.on('response', onResponse);
  };
  const onTarget = async target => {
    if (target.type() === 'page') watch(await target.page().catch(() => null));
  };
  (await browser.pages()).forEach(watch);
  browser.on('targetcreated', onTarget);

  const { refreshToken, validateToken } = require('./lib/slack-client');
  const startTime = Date.now();
  let lastClientBootAttempt = 0;
  let result = null;

  try {
    while (Date.now() - startTime < maxWaitMs) {
      const pages = await browser.pages();
      const handedOff = await leaveDesktopHandoff(pages);
      if (handedOff) page = handedOff;
      const onWorkspace = pages.find(candidate => !isPageClosed(candidate) && teamIdFromUrl(candidate.url()));
      if (onWorkspace && (!teamId || teamIdFromUrl(onWorkspace.url()) === teamId)) page = onWorkspace;

      const teamIdFromPage = teamIdFromUrl(page.url());
      if (teamIdFromPage && !teamId) teamId = teamIdFromPage;

      const dCookie = await getSlackCookie(page, workspaceName);
      if (dCookie) cookie = dCookie.value;

      if (cookie && !token && Date.now() - lastClientBootAttempt > 10000) {
        lastClientBootAttempt = Date.now();
        const boot = await captureViaClientBoot({ workspaceName, cookie, currentTeamId: teamId, refreshFn: refreshToken });
        takeToken(boot, 'client.boot');
      }

      if (!token) {
        const pageToken = await page.evaluate(() => {
          const findToken = (value, seen = new Set()) => {
            if (typeof value === 'string') {
              const match = value.match(/xoxc-[A-Za-z0-9-]+/);
              return match ? match[0] : null;
            }
            if (!value || typeof value !== 'object' || seen.has(value)) return null;
            seen.add(value);
            for (const entry of Array.isArray(value) ? value : Object.values(value)) {
              const found = findToken(entry, seen);
              if (found) return found;
            }
            return null;
          };
          const bootData = document.querySelector('[data-boot]');
          if (bootData) {
            const found = findToken(JSON.parse(bootData.getAttribute('data-boot')));
            if (found) return found;
          }
          const globalsToken = findToken(window.boot_data) || findToken(window.TS?.boot_data);
          if (globalsToken) return globalsToken;
          for (const storage of [window.localStorage, window.sessionStorage]) {
            for (let i = 0; i < storage.length; i += 1) {
              const found = findToken(storage.getItem(storage.key(i)));
              if (found) return found;
            }
          }
          return findToken(document.documentElement?.innerHTML || '');
        }).catch(() => null);
        if (pageToken) takeToken({ token: pageToken, teamId }, 'the page');
      }

      if (token && cookie) {
        const validation = await validateCapturedCredentials({
          token,
          cookie,
          capturedTeamId: teamId,
          validateFn: validateToken,
        });
        if (validation.valid) {
          result = { token, cookie, teamId: validation.teamId, page };
          break;
        }
        console.error(`Captured token did not match this workspace yet: ${validation.error}`);
        rejectedTokens.add(token);
        token = null;
        announced = false;
      }

      await sleep(2000);
    }
  } finally {
    browser.off('targetcreated', onTarget);
    for (const candidate of watched) {
      candidate.off('request', onRequest);
      candidate.off('response', onResponse);
    }
  }
  return result;
}

async function pushTokens(pushTarget, payload) {
  const url = adminTokensUrl(pushTarget);
  const adminSecret = process.env.ADMIN_SECRET;
  const bridgeApiKey = process.env.BRIDGE_API_KEY;
  if (!adminSecret) throw new Error('ADMIN_SECRET not set — cannot push to remote bridge');
  if (!bridgeApiKey) throw new Error('BRIDGE_API_KEY not set — cannot push to remote bridge (baseline API auth required)');

  const body = JSON.stringify(payload);
  const lib = url.protocol === 'https:' ? https : http;
  return new Promise((resolve, reject) => {
    const req = lib.request(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(body),
        'X-Admin-Secret': adminSecret,
        'X-Api-Key': bridgeApiKey,
      },
    }, res => {
      let data = '';
      res.on('data', chunk => { data += chunk; });
      res.on('end', () => {
        if (res.statusCode === 200) resolve(data);
        else reject(new Error(`Push failed (${res.statusCode}): ${data}`));
      });
    });
    req.on('error', reject);
    req.write(body);
    req.end();
  });
}

async function saveAndPush(store, workspaceName, captured) {
  store.setWorkspace(captured.teamId, {
    name: workspaceName,
    token: captured.token,
    cookie: captured.cookie,
  });
  if (pushUrl) {
    const answer = await pushTokens(pushUrl, {
      workspace: workspaceName,
      team_id: captured.teamId,
      token: captured.token,
      cookie: captured.cookie,
    });
    console.log(`Pushed ${workspaceName} to the bridge: ${answer}`);
  }
}

// Every workspace shares this profile's d cookie. When a sign-in replaced it, re-capture each
// other workspace with the new one before Slack rejects the old (2026-10-13: three went dark).
// If a capture itself changes the cookie again, the sweep starts over, at most twice.
async function refreshSiblings({ browser, page, store, cookie }) {
  const workspaceList = await readWorkspaceList(page);
  const listed = new Map(workspaceList.map(ws => [ws.slug, ws]));
  const results = new Map();
  for (let pass = 1; pass <= 2; pass += 1) {
    const stale = siblingsNeedingRefresh(store.listWorkspaces(), null, cookie)
      .filter(ws => !results.has(ws.team_id) || results.get(ws.team_id) === 'refreshed');
    if (stale.length === 0) break;
    console.log(`\nThe sign-in changed this window's Slack cookie; refreshing ${stale.length} other workspace(s) with it...`);
    let rotated = false;
    for (const ws of stale) {
      const entry = listed.get(ws.name);
      if (workspaceList.length > 0 && (!entry || entry.needsLogin)) {
        results.set(ws.team_id, 'needs pair');
        console.log(`  ${ws.name}: not signed in to this window; run: pair ${ws.name}`);
        continue;
      }
      page = await navigateToWorkspace(browser, page, ws.name);
      const captured = await captureSession({
        browser, page, workspaceName: ws.name, expectedTeamId: ws.team_id, maxWaitMs: SIBLING_WAIT_MS,
      });
      if (!captured) {
        results.set(ws.team_id, 'needs pair');
        console.log(`  ${ws.name}: no session within 60 s; run: pair ${ws.name}`);
        continue;
      }
      page = captured.page;
      await saveAndPush(store, ws.name, captured);
      results.set(ws.team_id, 'refreshed');
      if (captured.cookie !== cookie) {
        console.log('  The Slack cookie changed again while refreshing; starting the sweep over.');
        cookie = captured.cookie;
        rotated = true;
        break;
      }
    }
    if (!rotated) break;
  }
  return results;
}

// The check that was missing on 2026-10-13: every stored workspace, its own token and cookie,
// asked to Slack directly. A row that is not "ok" will stop polling on the bridge.
async function verifyAllWorkspaces(store) {
  const { validateToken } = require('./lib/slack-client');
  const rows = [];
  for (const ws of store.listWorkspaces()) {
    const check = await validateToken(ws.token, ws.cookie);
    rows.push({ name: ws.name, ok: check.valid, detail: check.valid ? '' : check.error });
  }
  console.log('\nEvery paired workspace, checked with Slack now:');
  for (const row of rows) {
    console.log(`  ${row.ok ? 'ok      ' : 'BROKEN  '} ${row.name}${row.ok ? '' : `  (${row.detail}; run: pair ${row.name})`}`);
  }
  return rows;
}

async function captureTokens() {
  const puppeteer = require('puppeteer');
  const { TokenStore } = require('./lib/token-store');
  const store = new TokenStore();
  const launchOptions = buildChromeLaunchOptions();
  console.log(launchOptions.executablePath
    ? `Using Chrome: ${launchOptions.executablePath}`
    : 'Google Chrome app not found; falling back to Puppeteer bundled browser.');
  console.log(`Pairing browser profile: ${launchOptions.userDataDir}`);

  const browser = await puppeteer.launch(launchOptions);
  let page = (await browser.pages())[0] || await browser.newPage();
  let target = workspace;
  let opened = false;

  try {
    if (!signInFirst) {
      const signedIn = await readWorkspaceList(page);
      let resolved = resolveSlug(workspace, signedIn);
      if (!resolved.slug && !resolved.ambiguous) {
        const waitMs = pairingTimeoutMs();
        if (signedIn.length > 0) {
          console.log(`\n"${workspace}" is not one of the workspaces this window is signed in to:`);
          printWorkspaceList(signedIn);
        }
        console.log(`\nIn the Chrome window, sign in with the email you use for "${workspace}" (use the email code, not "Open Slack").`);
        console.log(`Slack then lists all your workspaces and pairing goes on by itself (until ${clockIn(waitMs)}).`);
        const found = await waitForWorkspaceInList(browser, workspace, waitMs);
        if (!found.slug && !found.ambiguous) {
          if (waitTimeoutReason(signedIn, found.seen) === 'no-sign-in') {
            console.error(`\nNo email sign-in reached the Chrome window by ${clockIn(0)}, so nothing was paired. Run pair again when you can sign in.`);
          } else {
            console.error(`\n"${workspace}" is not in the list Slack showed after the sign-in. Check the name Slack shows for it:`);
            printWorkspaceList(found.seen);
          }
          return UNKNOWN_WORKSPACE_EXIT;
        }
        resolved = found;
        page = found.page;
      }
      if (resolved.ambiguous) {
        console.error(`\n"${workspace}" matches more than one workspace. Pair one of these by its slug:`);
        printWorkspaceList(resolved.ambiguous);
        return UNKNOWN_WORKSPACE_EXIT;
      }
      if (resolved.slug !== workspace) {
        console.log(`"${workspace}" is the workspace "${resolved.workspace.name}"; its address is ${resolved.slug}.slack.com. Pairing ${resolved.slug}.`);
      }
      target = resolved.slug;
      if (resolved.workspace.needsLogin) opened = await followSignInLink(page, target);
    }

    if (!opened) page = await navigateToWorkspace(browser, page, target);

    const maxWaitMs = pairingTimeoutMs();
    console.log(`\nWaiting for ${target}.slack.com to open in the window (until ${clockIn(maxWaitMs)}). If it asks, sign in there.`);

    const captured = await captureSession({
      browser, page, workspaceName: target, expectedTeamId: expectedTeamIdForWorkspace(target), maxWaitMs,
    });
    if (!captured) {
      console.error(`\nFailed to capture tokens for ${target}. Make sure you completed the login.`);
      console.error('If the name is not the workspace\'s web address, run pair with the name Slack shows; it finds the address.');
      return 1;
    }

    console.log(`\nTokens captured for ${target}: team ${captured.teamId}, token #${fingerprint(captured.token)}, cookie #${fingerprint(captured.cookie)}`);
    await saveAndPush(store, target, captured);

    if (process.env.SLACK_PAIRING_REFRESH_OTHERS !== '0') {
      await refreshSiblings({ browser, page: captured.page, store, cookie: captured.cookie });
    }
  } finally {
    await browser.close().catch(() => {});
  }

  const rows = await verifyAllWorkspaces(store);
  console.log('\nDone! The bridge polls the pushed workspaces automatically.');
  return rows.find(row => row.name === target && !row.ok) ? 1 : 0;
}

if (require.main === module) {
  captureTokens().then(
    code => process.exit(code || 0),
    e => {
      console.error('Fatal error:', e.message);
      process.exit(1);
    }
  );
}

module.exports = { adminTokensUrl, buildChromeLaunchOptions, captureViaClientBoot, extractRequestTokenPayload, extractResponseTokenPayload, extractTokenPayload, findXoxcToken, getSlackCookie, navigateToWorkspace, resolveChromeExecutable, teamIdFromRequestUrl, teamIdFromUrl, validateCapturedCredentials };
