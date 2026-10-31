// lib/pairing-session.js — pure rules for pairing several workspaces from one Chrome profile.
//
// Every workspace is paired from the same profile, so they all share one Slack `d` cookie.
// A new sign-in there replaces it, and Slack then rejects the old cookie the bridge holds for
// every other workspace (2026-10-13: three live workspaces went invalid_auth at once).

const DEFAULT_TIMEOUT_MINUTES = 15;
const RESERVED_SUBDOMAINS = new Set(['app', 'api', 'files', 'slack', 'status', 'www']);

// The "Welcome back!" list on slack.com/signin links each workspace one of two ways:
// <slug>.slack.com/ssb/redirect (already signed in here) or app.slack.com/t/<slug>/login/<one-time
// sign-in token> (needs that link first). Only the slug is kept; the token never leaves this function.
function workspaceFromLink(href) {
  let url;
  try {
    url = new URL(String(href || ''));
  } catch (_) {
    return null;
  }
  const host = url.hostname.toLowerCase();
  const sub = host.match(/^([a-z0-9-]+)\.slack\.com$/);
  if (sub && !RESERVED_SUBDOMAINS.has(sub[1]) && /^\/ssb\/redirect\/?$/.test(url.pathname)) {
    return { slug: sub[1], needsLogin: false };
  }
  if (host === 'app.slack.com') {
    const login = url.pathname.match(/^\/t\/([a-z0-9-]+)\/login\//i);
    if (login) return { slug: login[1].toLowerCase(), needsLogin: true };
  }
  return null;
}

function displayName(text, slug) {
  const lines = String(text || '').split('\n').map(line => line.trim()).filter(Boolean);
  const name = lines.find(line => !/^open$/i.test(line)
    && !/^[\d,]+\s+members?$/i.test(line)
    && !/\.slack\.com$/i.test(line)
    && !/^[A-Z]{1,2}$/.test(line));
  return name || slug;
}

function parseWorkspaceList(anchors) {
  const bySlug = new Map();
  for (const anchor of anchors || []) {
    const found = workspaceFromLink(anchor && anchor.href);
    if (!found || bySlug.has(found.slug)) continue;
    bySlug.set(found.slug, { slug: found.slug, name: displayName(anchor.text, found.slug), needsLogin: found.needsLogin });
  }
  return [...bySlug.values()];
}

function normalize(value) {
  return String(value || '').toLowerCase().replace(/[^a-z0-9]/g, '');
}

function cleanInput(input) {
  return String(input || '').trim().toLowerCase()
    .replace(/^https?:\/\//, '')
    .replace(/\.slack\.com.*$/, '')
    .replace(/\/+$/, '');
}

// "Northwind" is the display name; its slug is northwindgroup. An exact slug wins, then
// one unique match on the name or slug with spaces and punctuation removed. Never a guess.
function resolveSlug(input, workspaces) {
  const want = cleanInput(input);
  const exact = workspaces.find(ws => ws.slug === want);
  if (exact) return { slug: exact.slug, how: 'slug', workspace: exact };

  const key = normalize(want);
  const matches = workspaces.filter(ws => normalize(ws.name) === key || normalize(ws.slug) === key);
  if (matches.length === 1) return { slug: matches[0].slug, how: 'name', workspace: matches[0] };
  if (matches.length > 1) return { slug: null, ambiguous: matches };
  return { slug: null };
}

// "Launching <name>. Click Open Slack" hands the sign-in to the desktop app, so the browser never
// gets a session. The page's "use Slack in your browser" link is the way through.
function isDesktopHandoff(href) {
  try {
    const url = new URL(String(href || ''));
    return /\.slack\.com$/i.test(url.hostname) && /^\/ssb\/(signin_)?redirect\/?$/.test(url.pathname);
  } catch (_) {
    return false;
  }
}

function siblingsNeedingRefresh(stored, currentTeamId, newCookie) {
  return (stored || []).filter(ws => ws.team_id !== currentTeamId && ws.cookie !== newCookie);
}

function pairingTimeoutMs(env = process.env) {
  const minutes = Number(env.SLACK_PAIRING_TIMEOUT_MINUTES);
  const use = Number.isFinite(minutes) && minutes > 0 ? minutes : DEFAULT_TIMEOUT_MINUTES;
  return use * 60 * 1000;
}

// Why the wait for a sign-in ended without the name. An email sign-in makes the list show every
// workspace of that email, so a list with nothing new means nobody signed in.
function waitTimeoutReason(signedInBefore, seenLast) {
  const before = new Set((signedInBefore || []).map(ws => ws.slug));
  return (seenLast || []).some(ws => !before.has(ws.slug)) ? 'name-not-listed' : 'no-sign-in';
}

module.exports = {
  waitTimeoutReason,
  isDesktopHandoff,
  pairingTimeoutMs,
  parseWorkspaceList,
  resolveSlug,
  siblingsNeedingRefresh,
  workspaceFromLink,
};
