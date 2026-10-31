#!/usr/bin/env node
// sync.js — one-command Slack workspace pair/status/sync helper.

const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');
const { defaultRemoteBridgeUrl, loadEnvFile } = require('./pair');

const bridgeDir = __dirname;
const repoRoot = path.resolve(bridgeDir, process.env.SLACK_BRIDGE_REPO_ROOT || 'workspace-root');

function loadLocalEnv() {
  loadEnvFile(path.join(bridgeDir, '.env'));
  loadEnvFile(path.join(repoRoot, '.env'));
}

function normalizeArgs(args) {
  const normalized = [];
  for (const arg of args) {
    const match = arg.match(/^(--(?:workspace|workspaces|pair|bridge-url|days))=(.*)$/);
    if (match) {
      normalized.push(match[1], match[2]);
    } else {
      normalized.push(arg);
    }
  }
  return normalized;
}

function splitWorkspaces(value) {
  return String(value || '')
    .split(',')
    .map(item => item.trim())
    .filter(Boolean);
}

function valueForFlag(args, flag) {
  const idx = args.indexOf(flag);
  if (idx === -1) return null;
  const value = args[idx + 1];
  return value && !value.startsWith('--') ? value : null;
}

function requiredValueForFlag(args, flag) {
  const idx = args.indexOf(flag);
  if (idx === -1) return null;
  const value = args[idx + 1];
  if (!value || value.startsWith('--')) {
    throw new Error(`${flag} requires a value`);
  }
  return value;
}

function hasFlag(args, flag) {
  return args.includes(flag);
}

function collectWorkspaces(args) {
  const workspaces = [];
  const flags = new Set(['--workspace', '--workspaces', '--pair']);

  for (let i = 0; i < args.length; i += 1) {
    if (!flags.has(args[i])) continue;
    const value = args[i + 1];
    if (!value || value.startsWith('--')) {
      throw new Error(`${args[i]} requires a Slack workspace slug`);
    }
    workspaces.push(...splitWorkspaces(value));
    i += 1;
  }

  return [...new Set(workspaces)];
}

function normalizeBridgeUrl(value) {
  return String(value || '').replace(/\/+$/, '');
}

function parseOptions(argv = process.argv.slice(2), env = process.env) {
  const args = normalizeArgs(argv);
  if (hasFlag(args, '--help') || hasFlag(args, '-h')) {
    return { help: true };
  }

  const bridgeUrl = requiredValueForFlag(args, '--bridge-url')
    || env.SLACK_BRIDGE_URL_OVERRIDE
    || env.REMOTE_BRIDGE_URL
    || defaultRemoteBridgeUrl();

  const days = requiredValueForFlag(args, '--days') || '3';

  return {
    bridgeUrl: normalizeBridgeUrl(bridgeUrl),
    days,
    dryRun: hasFlag(args, '--dry-run'),
    noSync: hasFlag(args, '--no-sync'),
    signIn: hasFlag(args, '--sign-in'),
    statusOnly: hasFlag(args, '--status'),
    workspaces: collectWorkspaces(args),
  };
}

function usage() {
  console.error('Usage: npm run sync -- [--workspace <slug[,slug]>] [--sign-in] [--days N] [--dry-run] [--status]');
  console.error('  --sign-in  skip the workspace-list lookup and open <slug>.slack.com directly');
  console.error('Examples:');
  console.error('  npm run sync -- --status');
  console.error('  npm run sync -- --dry-run --days 7');
  console.error('  npm run sync -- --workspace acmecoai,widgetco --days 7');
}

function requireEnv(keys, context) {
  const missing = keys.filter(key => !process.env[key]);
  if (missing.length > 0) {
    throw new Error(`${missing.join(', ')} must be set in .env or the shell before ${context}`);
  }
}

function urlWithPath(baseUrl, suffix) {
  const url = new URL(baseUrl);
  const prefix = url.pathname.replace(/\/+$/, '');
  url.pathname = `${prefix}/${suffix.replace(/^\/+/, '')}`.replace(/\/{2,}/g, '/');
  url.search = '';
  url.hash = '';
  return url.toString();
}

// Without the API key /health leaves out the workspace list, and the summary below then said
// "none paired yet" right after a successful pairing.
async function fetchHealth(bridgeUrl, fetchImpl = globalThis.fetch, apiKey = process.env.BRIDGE_API_KEY) {
  if (typeof fetchImpl !== 'function') {
    throw new Error('fetch is not available');
  }
  const headers = apiKey ? { 'X-Api-Key': apiKey } : {};
  const response = await fetchImpl(urlWithPath(bridgeUrl, '/health'), { headers });
  if (!response.ok) {
    throw new Error(`health check failed: HTTP ${response.status}`);
  }
  return response.json();
}

function printHealth(health) {
  const workspaces = health.workspaces || [];
  console.error(`Bridge: ${health.status || 'unknown'}; buffered messages: ${health.total_messages || 0}`);
  if (workspaces.length === 0) {
    console.error('Workspaces: none paired yet');
    return;
  }

  console.error('Workspaces:');
  for (const ws of workspaces) {
    const name = ws.name || ws.team_id || 'unknown';
    console.error(`  - ${name}: ${ws.status || 'unknown'} (${ws.buffered_messages || 0} buffered messages)`);
  }
}

function missingDependencies(dir = bridgeDir, existsSync = fs.existsSync) {
  return !existsSync(path.join(dir, 'node_modules', 'puppeteer', 'package.json'));
}

// node_modules vanished once and pairing died on "Cannot find module 'puppeteer'". auth.js drives
// the installed Google Chrome, so Puppeteer's own browser download is skipped.
function ensureDependencies() {
  if (!missingDependencies()) return 0;
  console.error('Pairing packages are missing; installing them from package-lock.json...');
  const result = spawnSync('npm', ['ci', '--no-audit', '--no-fund'], {
    cwd: bridgeDir,
    env: { ...process.env, PUPPETEER_SKIP_DOWNLOAD: '1' },
    stdio: 'inherit',
  });
  return result.status === null ? 1 : result.status;
}

function pairArgs(dir, workspace, bridgeUrl, { signIn = false } = {}) {
  const args = [path.join(dir, 'pair.js'), '--workspace', workspace, '--push', bridgeUrl];
  if (signIn) args.push('--sign-in');
  return args;
}

function runPair(workspace, bridgeUrl, options = {}) {
  requireEnv(['ADMIN_SECRET', 'BRIDGE_API_KEY'], 'pairing');
  console.error(`\nPairing ${workspace}...`);
  const result = spawnSync(process.execPath, pairArgs(bridgeDir, workspace, bridgeUrl, options), {
    cwd: bridgeDir,
    env: process.env,
    stdio: 'inherit',
  });
  return result.status === null ? 1 : result.status;
}

function syncArgs({ days, dryRun }) {
  const args = ['execution/infrastructure/bi_sync_slack.py', '--days', String(days)];
  if (dryRun) args.push('--dry-run');
  return args;
}

function runSync(options) {
  requireEnv(['BRIDGE_API_KEY', 'SUPABASE_URL', 'SUPABASE_SERVICE_ROLE_KEY'], 'syncing');
  console.error(`\nSyncing Slack via ${options.bridgeUrl}${options.dryRun ? ' (dry run)' : ''}...`);
  const result = spawnSync('python3', syncArgs(options), {
    cwd: repoRoot,
    env: { ...process.env, SLACK_BRIDGE_URL: options.bridgeUrl },
    stdio: 'inherit',
  });
  return result.status === null ? 1 : result.status;
}

async function main(argv = process.argv.slice(2)) {
  loadLocalEnv();
  const options = parseOptions(argv);
  if (options.help) {
    usage();
    return 0;
  }

  if (options.workspaces.length > 0) {
    const installed = ensureDependencies();
    if (installed !== 0) return installed;
  }

  for (const workspace of options.workspaces) {
    const code = runPair(workspace, options.bridgeUrl, { signIn: options.signIn });
    if (code !== 0) return code;
  }

  try {
    const health = await fetchHealth(options.bridgeUrl);
    printHealth(health);
  } catch (error) {
    console.error(`Bridge health warning: ${error.message}`);
  }

  if (options.statusOnly || options.noSync) return 0;
  return runSync(options);
}

if (require.main === module) {
  main().then(
    code => process.exit(code),
    error => {
      console.error(error.message);
      process.exit(1);
    }
  );
}

module.exports = {
  collectWorkspaces,
  fetchHealth,
  missingDependencies,
  pairArgs,
  normalizeArgs,
  normalizeBridgeUrl,
  parseOptions,
  splitWorkspaces,
  syncArgs,
  urlWithPath,
};
