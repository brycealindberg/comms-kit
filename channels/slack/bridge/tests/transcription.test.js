// tests/transcription.test.js
const { describe, it } = require('node:test');
const assert = require('node:assert');

const {
  inferExtension,
  isTranscribableFile,
  whisperTranscribe,
} = require('../lib/transcription');

describe('transcription helpers', () => {
  it('detects Slack audio files and OpenAI-supported clips as transcribable', () => {
    assert.strictEqual(isTranscribableFile({ mimetype: 'audio/mpeg' }), true);
    assert.strictEqual(isTranscribableFile({ type: 'audio/ogg; codecs=opus' }), true);
    assert.strictEqual(isTranscribableFile({ mimetype: 'audio/flac' }), true);
    assert.strictEqual(isTranscribableFile({ mimetype: 'video/mp4' }), true);
    assert.strictEqual(isTranscribableFile({ mimetype: 'video/webm' }), true);
    assert.strictEqual(isTranscribableFile({ mimetype: 'video/quicktime' }), false);
    assert.strictEqual(isTranscribableFile({ mimetype: 'application/octet-stream', name: 'slack-clip.mov' }), false);
    assert.strictEqual(isTranscribableFile({ mimetype: 'audio/aac', name: 'voice.aac' }), false);
    assert.strictEqual(isTranscribableFile({ mimetype: 'image/png', name: 'screen.png' }), false);
  });

  it('infers Whisper-friendly extensions from mimetypes and file metadata', () => {
    assert.strictEqual(inferExtension('audio/mpeg'), 'mp3');
    assert.strictEqual(inferExtension('audio/mp4'), 'm4a');
    assert.strictEqual(inferExtension({ mimetype: 'audio/ogg', name: 'voice.oga' }), 'ogg');
    assert.strictEqual(inferExtension({ name: 'Voice Note.WEBM' }), 'webm');
    assert.strictEqual(inferExtension({ filetype: 'mp4' }), 'mp4');
  });

  it('calls Whisper with native fetch and FormData when a fetch mock is supplied', async () => {
    let called = false;
    const fetchImpl = async (url, init) => {
      called = true;
      assert.strictEqual(url, 'https://api.openai.com/v1/audio/transcriptions');
      assert.strictEqual(init.method, 'POST');
      assert.strictEqual(init.headers.Authorization, 'Bearer test-key');
      assert.ok(init.body instanceof FormData);
      assert.strictEqual(init.body.get('model'), 'whisper-1');
      assert.strictEqual(init.body.get('response_format'), 'verbose_json');
      assert.ok(init.body.get('file') instanceof Blob);
      return {
        ok: true,
        json: async () => ({ text: 'hello world', language: 'en', duration: 1.25 }),
      };
    };

    const result = await whisperTranscribe(Buffer.from('fake-audio'), { mimetype: 'audio/webm' }, {
      apiKey: 'test-key',
      fetchImpl,
    });

    assert.strictEqual(called, true);
    assert.deepStrictEqual(result, { text: 'hello world', language: 'en', duration: 1.25 });
  });

  it('fails closed before fetch when OPENAI_API_KEY is missing', async () => {
    await assert.rejects(
      whisperTranscribe(Buffer.from('fake-audio'), 'audio/mpeg', {
        apiKey: '',
        fetchImpl: async () => {
          throw new Error('fetch should not be called');
        },
      }),
      /OPENAI_API_KEY not set/
    );
  });
});

describe('WorkspacePoller audio-only file filtering', () => {
  it('does not skip an audio-only Slack file message', () => {
    const Module = require('node:module');
    const originalLoad = Module._load;
    Module._load = function mockSlackWebApi(request, parent, isMain) {
      if (request === '@slack/web-api') {
        return { WebClient: class WebClient {} };
      }
      return originalLoad.call(this, request, parent, isMain);
    };

    try {
      delete require.cache[require.resolve('../lib/slack-client')];
      delete require.cache[require.resolve('../lib/poller')];
      const { WorkspacePoller } = require('../lib/poller');
      const poller = new WorkspacePoller({
        teamId: 'T1',
        name: 'test',
        token: 'xoxc-test',
        cookie: 'xoxd-test',
        buffer: { addBatch() {} },
      });

      assert.strictEqual(poller._shouldSkip({ text: '', files: [{ mimetype: 'audio/mpeg' }] }), false);
      assert.strictEqual(poller._shouldSkip({ text: '', files: [{ mimetype: 'image/png' }] }), true);
    } finally {
      Module._load = originalLoad;
    }
  });
});
