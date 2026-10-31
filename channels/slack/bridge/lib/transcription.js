// lib/transcription.js

const MIME_EXTENSION_MAP = new Map([
  ['audio/flac', 'flac'],
  ['audio/m4a', 'm4a'],
  ['audio/mp4', 'm4a'],
  ['audio/mpeg', 'mp3'],
  ['audio/mp3', 'mp3'],
  ['audio/mpga', 'mpga'],
  ['audio/ogg', 'ogg'],
  ['audio/wav', 'wav'],
  ['audio/wave', 'wav'],
  ['audio/webm', 'webm'],
  ['audio/x-m4a', 'm4a'],
  ['audio/x-wav', 'wav'],
  ['video/mp4', 'mp4'],
  ['video/mpeg', 'mpeg'],
  ['video/webm', 'webm'],
]);

const EXTENSION_ALIASES = new Map([
  ['oga', 'ogg'],
]);

const TRANSCRIBABLE_EXTENSIONS = new Set([
  'flac', 'm4a', 'mp3', 'mp4', 'mpeg', 'mpga', 'ogg', 'wav', 'webm',
]);

const TRANSCRIBABLE_VIDEO_MIMES = new Set([
  'video/mp4',
  'video/mpeg',
  'video/webm',
]);

function normalizeMime(mimetype) {
  return String(mimetype || '').split(';', 1)[0].trim().toLowerCase();
}

function normalizeExtension(ext) {
  const normalized = String(ext || '')
    .trim()
    .toLowerCase()
    .replace(/^\.+/, '');
  if (!/^[a-z0-9]{2,8}$/.test(normalized)) return '';
  return EXTENSION_ALIASES.get(normalized) || normalized;
}

function extensionFromName(name) {
  const text = String(name || '').trim();
  const dot = text.lastIndexOf('.');
  if (dot < 0 || dot === text.length - 1) return '';
  return normalizeExtension(text.slice(dot + 1));
}

function mimeToExtension(mimetype) {
  const normalized = normalizeMime(mimetype);
  if (!normalized) return '';
  const mapped = MIME_EXTENSION_MAP.get(normalized);
  if (mapped) return mapped;
  return '';
}

function inferExtension(fileOrMimetype) {
  if (typeof fileOrMimetype === 'string') {
    return mimeToExtension(fileOrMimetype);
  }

  const file = fileOrMimetype || {};
  return mimeToExtension(file.mimetype || file.type)
    || extensionFromName(file.name || file.title)
    || normalizeExtension(file.filetype)
    || extensionFromName(file.url_private_download || file.url_private)
    || '';
}

function isTranscribableFile(file) {
  if (!file) return false;

  const mimetype = normalizeMime(file.mimetype || file.type);
  if (MIME_EXTENSION_MAP.has(mimetype)) return true;
  if (TRANSCRIBABLE_VIDEO_MIMES.has(mimetype)) return true;

  const ext = inferExtension(file);
  return TRANSCRIBABLE_EXTENSIONS.has(ext);
}

async function parseOpenAIError(response) {
  try {
    const data = await response.json();
    return data?.error?.message || data?.message || `Whisper API returned ${response.status}`;
  } catch (_) {
    try {
      const text = await response.text();
      return text || `Whisper API returned ${response.status}`;
    } catch (__) {
      return `Whisper API returned ${response.status}`;
    }
  }
}

async function whisperTranscribe(fileBuffer, fileOrMimetype, options = {}) {
  const apiKey = Object.prototype.hasOwnProperty.call(options, 'apiKey')
    ? options.apiKey
    : (process.env.OPENAI_API_KEY || '');
  if (!apiKey) {
    throw new Error('OPENAI_API_KEY not set');
  }

  const fetchImpl = options.fetchImpl || globalThis.fetch;
  if (typeof fetchImpl !== 'function') {
    throw new Error('fetch is not available');
  }

  const mimetype = typeof fileOrMimetype === 'string'
    ? normalizeMime(fileOrMimetype)
    : normalizeMime(fileOrMimetype?.mimetype || fileOrMimetype?.type) || 'application/octet-stream';
  const ext = inferExtension(fileOrMimetype) || 'mp3';
  const filename = `slack-audio.${ext}`;

  const form = new FormData();
  form.append('file', new Blob([fileBuffer], { type: mimetype }), filename);
  form.append('model', 'whisper-1');
  form.append('response_format', 'verbose_json');

  const response = await fetchImpl('https://api.openai.com/v1/audio/transcriptions', {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${apiKey}`,
    },
    body: form,
  });

  if (!response.ok) {
    throw new Error(await parseOpenAIError(response));
  }

  const data = await response.json();
  return {
    text: data.text || '',
    language: data.language || null,
    duration: data.duration || null,
  };
}

module.exports = {
  inferExtension,
  isTranscribableFile,
  whisperTranscribe,
};
