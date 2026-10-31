// lib/security-limits.js

function positiveInt(value, fallback) {
  const parsed = Number.parseInt(String(value ?? ''), 10);
  if (!Number.isFinite(parsed) || parsed <= 0) return fallback;
  return parsed;
}

function envPositiveInt(name, fallback) {
  return positiveInt(process.env[name], fallback);
}

function createSlidingWindowLimiter({ max, windowMs, now = () => Date.now() }) {
  const timestamps = [];
  const limit = positiveInt(max, 1);
  const window = positiveInt(windowMs, 60_000);

  return {
    tryRemoveToken() {
      const current = now();
      while (timestamps.length > 0 && timestamps[0] <= current - window) {
        timestamps.shift();
      }
      if (timestamps.length >= limit) {
        const err = new Error(`rate limit exceeded: ${limit} per ${Math.round(window / 1000)}s`);
        err.statusCode = 429;
        return { ok: false, error: err, remaining: 0 };
      }
      timestamps.push(current);
      return { ok: true, remaining: limit - timestamps.length };
    },
  };
}

// The one gate for a route that posts to Slack: find the workspace first, then take a slot.
// A channel no workspace knows reaches nobody, so it must not use up the limit for real sends.
function admitSend({ pollers, channel, workspace, limiter }) {
  let poller = null;
  for (const [teamId, candidate] of pollers) {
    if (workspace && teamId !== workspace && candidate.name !== workspace) continue;
    if (candidate.channelMap.has(channel)) {
      poller = candidate;
      break;
    }
  }
  if (!poller) return { status: 404, error: 'Channel not found in any connected workspace' };
  if (!limiter.tryRemoveToken().ok) return { status: 429, error: 'Rate limit: 20/min' };
  return { status: 200, poller };
}

function createConcurrencyGate(maxConcurrent) {
  const limit = positiveInt(maxConcurrent, 1);
  let active = 0;

  return {
    active() {
      return active;
    },

    async run(fn) {
      if (active >= limit) {
        const err = new Error(`too many transcription requests in flight: max ${limit}`);
        err.statusCode = 429;
        throw err;
      }
      active += 1;
      try {
        return await fn();
      } finally {
        active -= 1;
      }
    },
  };
}

async function runLimitedWork({ limiter, gate }, fn) {
  return gate.run(async () => {
    const admission = limiter.tryRemoveToken();
    if (!admission.ok) throw admission.error;
    return fn();
  });
}

function createBoundedMap(maxEntries) {
  const limit = positiveInt(maxEntries, 1);
  const map = new Map();

  return {
    get size() {
      return map.size;
    },

    get(key) {
      return map.get(key);
    },

    has(key) {
      return map.has(key);
    },

    set(key, value) {
      if (map.has(key)) map.delete(key);
      map.set(key, value);
      while (map.size > limit) {
        const oldest = map.keys().next().value;
        map.delete(oldest);
      }
      return this;
    },

    delete(key) {
      return map.delete(key);
    },

    clear() {
      map.clear();
    },

    entries() {
      return map.entries();
    },

    keys() {
      return map.keys();
    },

    values() {
      return map.values();
    },

    [Symbol.iterator]() {
      return map[Symbol.iterator]();
    },
  };
}


function splitTranscriptionWork(items, { maxNew, isCached }) {
  const cached = [];
  const toProcess = [];
  const skipped = [];
  const limit = positiveInt(maxNew, 1);

  for (const item of items) {
    if (isCached(item)) {
      cached.push(item);
    } else if (toProcess.length < limit) {
      toProcess.push(item);
    } else {
      skipped.push(item);
    }
  }

  return {
    cached,
    toProcess,
    skipped,
    total: items.length,
    maxNew: limit,
  };
}

function isDetailedHealthAuthorized(providedKey, configuredKey, compare = (a, b) => a === b) {
  const configured = String(configuredKey ?? '');
  if (!configured) return false;
  return compare(String(providedKey ?? ''), configured);
}

function buildHealthPayload(base, { detailed = false } = {}) {
  const payload = {
    status: 'ok',
    uptime_seconds: base.uptime_seconds,
  };

  if (detailed) {
    payload.total_messages = base.total_messages;
    payload.workspaces = base.workspaces;
  }

  return payload;
}

module.exports = {
  createBoundedMap,
  buildHealthPayload,
  createConcurrencyGate,
  admitSend,
  createSlidingWindowLimiter,
  envPositiveInt,
  isDetailedHealthAuthorized,
  positiveInt,
  runLimitedWork,
  splitTranscriptionWork,
};
