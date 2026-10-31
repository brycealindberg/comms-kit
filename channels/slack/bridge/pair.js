#!/usr/bin/env node
// pair.js — local wrapper for pairing a Slack workspace with the remote bridge.

const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');

const bridgeDir = __dirname;
const repoRoot = path.resolve(bridgeDir, process.env.SLACK_BRIDGE_REPO_ROOT || 'workspace-root');

function unquoteEnvValue(value) {
  const trimmed = value.trim();
  if (trimmed.length >= 2) {
    const quote = trimmed[0];
    if ((quote === '"' || quote === "'") && trimmed[trimmed.length - 1] === quote) {
      return trimmed.slice(1, -1).replace(/\\n/g, '\n');
    }
  }
  return trimmed;
}

function loadEnvFile(filePath) {
  if (!fs.existsSync(filePath)) return false;

  const contents = fs.readFileSync(filePath, 'utf8');
  for (const rawLine of contents.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#')) continue;

    const withoutExport = line.startsWith('export ') ? line.slice('export '.length).trim() : line;
    const separatorIdx = withoutExport.indexOf('=');
    if (separatorIdx <= 0) continue;

    const key = withoutExport.slice(0, separatorIdx).trim();
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) continue;
    if (process.env[key] !== undefined) continue;

    process.env[key] = unquoteEnvValue(withoutExport.slice(separatorIdx + 1));
  }

  return true;
}

function loadLocalEnv() {
  loadEnvFile(path.join(bridgeDir, '.env'));
  loadEnvFile(path.join(repoRoot, '.env'));
}

function normalizeKnownArgs(args) {
  const normalized = [];
  for (const arg of args) {
    if (arg.startsWith('--workspace=')) {
      normalized.push('--workspace', arg.slice('--workspace='.length));
    } else if (arg.startsWith('--push=')) {
      normalized.push('--push', arg.slice('--push='.length));
    } else {
      normalized.push(arg);
    }
  }
  return normalized;
}

function valueForFlag(args, flag) {
  const idx = args.indexOf(flag);
  if (idx === -1) return null;
  const value = args[idx + 1];
  return value && !value.startsWith('--') ? value : null;
}

function hasFlag(args, flag) {
  return args.includes(flag);
}

function defaultRemoteBridgeUrl() {
  const deployPath = path.join(repoRoot, 'apps/vps/deploy.json');
  const deploy = JSON.parse(fs.readFileSync(deployPath, 'utf8'));
  const hostname = deploy.server && deploy.server.hostname;
  const prefix = deploy.services && deploy.services['slack-bridge'] && deploy.services['slack-bridge'].path_prefix;

  if (!hostname) {
    throw new Error('apps/vps/deploy.json is missing server.hostname');
  }

  const url = new URL(`https://${hostname}`);
  url.pathname = prefix || '';
  return url.toString().replace(/\/+$/, '');
}

function usage() {
  console.error('Usage: npm run pair -- --workspace <slack-workspace-name> [--push <remote-url>]');
  console.error('Example: npm run pair -- --workspace stellardigital');
}

function validatePairingEnv() {
  const missing = [];
  if (!process.env.ADMIN_SECRET) missing.push('ADMIN_SECRET');
  if (!process.env.BRIDGE_API_KEY) missing.push('BRIDGE_API_KEY');

  if (missing.length > 0) {
    throw new Error(`${missing.join(' and ')} must be set in .env or the shell before pairing`);
  }
}

function main(argv = process.argv.slice(2)) {
  loadLocalEnv();

  const args = normalizeKnownArgs(argv);
  const workspace = valueForFlag(args, '--workspace');
  if (!workspace) {
    usage();
    return 1;
  }

  validatePairingEnv();

  const authArgs = [...args];
  if (hasFlag(authArgs, '--push')) {
    if (!valueForFlag(authArgs, '--push')) {
      console.error('--push requires a remote bridge URL');
      return 1;
    }
  } else {
    const remoteBridgeUrl = process.env.REMOTE_BRIDGE_URL || defaultRemoteBridgeUrl();
    authArgs.push('--push', remoteBridgeUrl);
  }

  const result = spawnSync(process.execPath, [path.join(bridgeDir, 'auth.js'), ...authArgs], {
    cwd: bridgeDir,
    env: process.env,
    stdio: 'inherit',
  });

  if (result.error) {
    console.error(`Failed to start auth.js: ${result.error.message}`);
    return 1;
  }
  if (result.signal) {
    console.error(`auth.js stopped by signal ${result.signal}`);
    return 1;
  }
  return result.status === null ? 1 : result.status;
}

if (require.main === module) {
  try {
    process.exit(main());
  } catch (error) {
    console.error(error.message);
    process.exit(1);
  }
}

module.exports = {
  defaultRemoteBridgeUrl,
  loadEnvFile,
  main,
  normalizeKnownArgs,
};
