// tests/pairing-session.test.js — the 2026-10-13 pairing failures, one test per way it broke.
const { describe, it } = require('node:test');
const assert = require('node:assert');

const {
  isDesktopHandoff,
  pairingTimeoutMs,
  parseWorkspaceList,
  resolveSlug,
  siblingsNeedingRefresh,
  waitTimeoutReason,
} = require('../lib/pairing-session');

// Shapes copied from slack.com/signin ("Welcome back!") in the pairing profile, 2026-10-13.
// The login token in the second shape is a one-time sign-in link and must never be kept.
const LOGIN_TOKEN = 'z-app-6871244391489-12297818873059-69a101e220782a44e67a37604a62a3c7';
const SIGNIN_ANCHORS = [
  { href: 'https://contosoconsultinginc.slack.com/ssb/redirect', text: 'EC\nContoso Consulting, Inc\n12 members\nOpen' },
  { href: 'https://samdoe.slack.com/ssb/redirect', text: 'Sam Doe\n34 members' },
  { href: `https://app.slack.com/t/northwindgroup/login/${LOGIN_TOKEN}`, text: 'S\nNorthwind\n17 members' },
  { href: 'https://app.slack.com/t/fabrikamstrategy/login/z-app-1-2-abc', text: 'XS\nXTI Strategy\n4 members' },
  { href: 'https://initech.slack.com/ssb/redirect', text: 'Initech\n652 members' },
  { href: 'https://slack.com/help/articles/212681477', text: 'Not seeing your workspace?' },
  { href: 'https://app.slack.com/client/T0FAKEIDENT002', text: 'Northwind' },
];

describe('parseWorkspaceList', () => {
  const list = parseWorkspaceList(SIGNIN_ANCHORS);

  it('reads the slug from both link shapes Slack uses', () => {
    assert.deepStrictEqual(
      list.map(w => w.slug),
      ['contosoconsultinginc', 'samdoe', 'northwindgroup', 'fabrikamstrategy', 'initech']
    );
  });

  it('reads the name from the row Slack draws around the Open button', () => {
    const row = parseWorkspaceList([{
      href: 'https://northwindgroup.slack.com/ssb/redirect?entry_point=get_started',
      text: 'S\nNorthwind\nnorthwindgroup.slack.com\nOpen',
    }]);
    assert.deepStrictEqual(row, [{ slug: 'northwindgroup', name: 'Northwind', needsLogin: false }]);
  });

  it('takes the display name, not the avatar initials, member count or Open label', () => {
    assert.strictEqual(list[0].name, 'Contoso Consulting, Inc');
    assert.strictEqual(list[2].name, 'Northwind');
    assert.strictEqual(list[3].name, 'XTI Strategy');
  });

  it('marks a workspace that still needs its sign-in link as needing a login', () => {
    assert.strictEqual(list.find(w => w.slug === 'northwindgroup').needsLogin, true);
    assert.strictEqual(list.find(w => w.slug === 'samdoe').needsLogin, false);
  });

  it('never keeps the one-time sign-in link anywhere in its output', () => {
    assert.ok(!JSON.stringify(list).includes('z-app-'));
  });

  it('keeps the first entry when Slack repeats a workspace', () => {
    const twice = parseWorkspaceList([SIGNIN_ANCHORS[1], { ...SIGNIN_ANCHORS[1], text: 'Open' }]);
    assert.deepStrictEqual(twice, [{ slug: 'samdoe', name: 'Sam Doe', needsLogin: false }]);
  });
});

describe('resolveSlug', () => {
  const list = parseWorkspaceList(SIGNIN_ANCHORS);

  it('maps the display name to the real slug (Northwind is northwindgroup)', () => {
    const result = resolveSlug('northwind', list);
    assert.strictEqual(result.slug, 'northwindgroup');
    assert.strictEqual(result.how, 'name');
  });

  it('accepts the name with spaces and capitals, or a full workspace URL', () => {
    assert.strictEqual(resolveSlug('North Wind', list).slug, 'northwindgroup');
    assert.strictEqual(resolveSlug('https://samdoe.slack.com/', list).slug, 'samdoe');
  });

  it('keeps an exact slug as it is', () => {
    assert.deepStrictEqual(resolveSlug('initech', list).slug, 'initech');
    assert.strictEqual(resolveSlug('initech', list).how, 'slug');
  });

  it('returns no slug for a name that matches nothing, so pairing stops instead of timing out', () => {
    assert.strictEqual(resolveSlug('north', list).slug, null);
  });

  it('does not guess when two workspaces share a name', () => {
    const twins = [
      { slug: 'acme', name: 'Acme', needsLogin: false },
      { slug: 'acme-team', name: 'ACME', needsLogin: false },
    ];
    const result = resolveSlug('acme team', twins);
    assert.strictEqual(result.slug, 'acme-team');
    const ambiguous = resolveSlug('Acme', [{ slug: 'one', name: 'Acme' }, { slug: 'two', name: 'acme' }]);
    assert.strictEqual(ambiguous.slug, null);
    assert.deepStrictEqual(ambiguous.ambiguous.map(w => w.slug), ['one', 'two']);
  });
});

describe('isDesktopHandoff', () => {
  it('detects the "Launching <name> / Open Slack" pages', () => {
    assert.strictEqual(isDesktopHandoff('https://northwindgroup.slack.com/ssb/redirect'), true);
    assert.strictEqual(isDesktopHandoff('https://northwindgroup.slack.com/ssb/signin_redirect?x=1'), true);
  });

  it('leaves the browser client and look-alike hosts alone', () => {
    assert.strictEqual(isDesktopHandoff('https://app.slack.com/client/T0FAKEIDENT002/C0FAKECH001'), false);
    assert.strictEqual(isDesktopHandoff('https://evil.example/ssb/redirect'), false);
    assert.strictEqual(isDesktopHandoff('not a url'), false);
  });
});

describe('siblingsNeedingRefresh', () => {
  const stored = [
    { team_id: 'T1', name: 'acmecoai', cookie: 'old' },
    { team_id: 'T2', name: 'samdoe', cookie: 'new' },
    { team_id: 'T3', name: 'northwindgroup', cookie: 'old' },
    { team_id: 'T4', name: 'widgetco', cookie: 'older' },
  ];

  it('returns every other workspace whose stored cookie the new sign-in replaced', () => {
    assert.deepStrictEqual(
      siblingsNeedingRefresh(stored, 'T3', 'new').map(w => w.name),
      ['acmecoai', 'widgetco']
    );
  });

  it('returns nothing when every workspace already uses the new cookie', () => {
    assert.deepStrictEqual(siblingsNeedingRefresh(stored.slice(1, 2), 'T3', 'new'), []);
  });
});

describe('pairingTimeoutMs', () => {
  it('gives a person 15 minutes by default (5 ran out 3 s after a Google sign-in)', () => {
    assert.strictEqual(pairingTimeoutMs({}), 15 * 60 * 1000);
  });

  it('takes SLACK_PAIRING_TIMEOUT_MINUTES and ignores values that are not a positive number', () => {
    assert.strictEqual(pairingTimeoutMs({ SLACK_PAIRING_TIMEOUT_MINUTES: '30' }), 30 * 60 * 1000);
    assert.strictEqual(pairingTimeoutMs({ SLACK_PAIRING_TIMEOUT_MINUTES: '0' }), 15 * 60 * 1000);
    assert.strictEqual(pairingTimeoutMs({ SLACK_PAIRING_TIMEOUT_MINUTES: 'soon' }), 15 * 60 * 1000);
  });
});

// 2026-10-13 4:28pm EDT: `pair "Tailspin Systems"` waited 15 minutes, nobody signed in, and it said
// "Check the name Slack shows for it": advice for a different failure.
describe('waitTimeoutReason', () => {
  const before = [{ slug: 'widgetco', name: 'Widget Co' }, { slug: 'globex', name: 'Globex' }];

  it('says nobody signed in when the window still lists only what it listed before', () => {
    assert.strictEqual(waitTimeoutReason(before, before), 'no-sign-in');
    assert.strictEqual(waitTimeoutReason(before, []), 'no-sign-in');
  });

  it('says the name is not listed when a sign-in added workspaces but none matched', () => {
    const after = [...before, { slug: 'tailspin', name: 'Tailspin Labs' }];
    assert.strictEqual(waitTimeoutReason(before, after), 'name-not-listed');
  });
});
