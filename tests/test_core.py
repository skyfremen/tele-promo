import asyncio
from copy import deepcopy
import pytest

from promo.core import Engine, Group, permitted, video_id, rules_hash
from promo.store import validate_state

NOW = 1_800_000_000
RULES = 'YouTube Shorts promotion is allowed.'

def config():
    return {'dry_run': True, 'keywords': ['comedy'], 'shorts': [{'url': 'https://youtube.com/shorts/abcdefghijk', 'caption': 'Original comedy'}], 'limits': {'joins_per_run': 1, 'posts_per_run': 1, 'posts_per_day': 2, 'cooldown_hours': 48, 'discovery_results': 5}, 'approved_groups': []}

class Memory:
    def __init__(self):
        self.state = {'version': 1, 'groups': {}, 'posts': {}, 'blocked_until': 0}
        self.saved = []
    def save(self, state):
        self.saved.append(deepcopy(state))

class Telegram:
    def __init__(self, group=None):
        self.group = group or Group(123, 'comedy_test', 'Comedy', RULES, '', True, False)
        self.joined = self.sent = 0
        self.failure = False
    async def discover(self, keywords, limit):
        return [self.group]
    async def inspect(self, username):
        return self.group
    async def join(self, group):
        self.joined += 1
        self.group.member = True
    async def can_post(self, group):
        return True
    async def send(self, group, text, random_id):
        self.sent += 1
        if self.failure:
            raise TimeoutError('private details must never be logged')
        return 77


def test_permission_requires_explicit_rules_or_current_approval():
    g = Telegram().group
    assert permitted(g, [], NOW)
    for rule in ['Members may chat.', 'YouTube Shorts promotion is not allowed.', 'Do not believe: YouTube Shorts promotion is allowed.', RULES + '\nAsk an admin first.']:
        g.about = rule
        assert not permitted(g, [], NOW)
    approval = {'id': 123, 'rules_hash': rules_hash(g), 'expires_at': NOW + 100, 'evidence': 'Admin explicitly approved YouTube promotion', 'approved': True}
    assert permitted(g, [approval], NOW)
    g.pinned = 'No advertising'
    assert not permitted(g, [approval], NOW)


def test_missing_rules_and_restricted_group_fail_closed():
    g = Telegram().group
    g.rules_complete = False
    assert not permitted(g, [], NOW)
    g.rules_complete = True
    g.is_group = False
    assert not permitted(g, [], NOW)


def test_video_identity_removes_tracking_and_validates_domain():
    assert video_id('https://youtu.be/abcdefghijk?si=abc') == video_id('https://youtube.com/shorts/abcdefghijk')
    with pytest.raises(ValueError):
        video_id('https://youtube.com.attacker.invalid/shorts/abcdefghijk')


def test_dry_run_discovers_without_join_or_post():
    store, tg = Memory(), Telegram()
    asyncio.run(Engine(tg, store, config(), now=lambda: NOW).run(live=False))
    assert tg.joined == tg.sent == 0
    assert store.state['groups']['123']['status'] == 'eligible'
    assert not store.state['posts']


def test_live_has_dual_gate():
    store, tg = Memory(), Telegram()
    asyncio.run(Engine(tg, store, config(), now=lambda: NOW).run(live=True))
    assert tg.sent == 0


def test_write_ahead_duplicate_and_cooldown_across_runs():
    store, tg, cfg = Memory(), Telegram(), config()
    cfg['dry_run'] = False
    engine = Engine(tg, store, cfg, now=lambda: NOW)
    asyncio.run(engine.run(live=True))
    assert tg.joined == tg.sent == 1
    assert any(p['status'] == 'pending' for s in store.saved for p in s['posts'].values())
    asyncio.run(engine.run(live=True))
    assert tg.sent == 1
    cfg['shorts'] = [{'url': 'https://youtube.com/shorts/123456789ab', 'caption': 'Another'}]
    asyncio.run(engine.run(live=True))
    assert tg.sent == 1
    engine.now = lambda: NOW + 49 * 3600
    asyncio.run(engine.run(live=True))
    assert tg.sent == 2


def test_uncertain_send_is_never_retried():
    store, tg, cfg = Memory(), Telegram(), config()
    cfg['dry_run'] = False
    tg.failure = True
    engine = Engine(tg, store, cfg, now=lambda: NOW)
    with pytest.raises(TimeoutError):
        asyncio.run(engine.run(live=True))
    assert next(iter(store.state['posts'].values()))['status'] == 'uncertain'
    tg.failure = False
    asyncio.run(engine.run(live=True))
    assert tg.sent == 1


def test_persistence_failure_prevents_send():
    store, tg, cfg = Memory(), Telegram(), config()
    cfg['dry_run'] = False
    def broken(state):
        if state['posts']:
            raise OSError('state unavailable')
    store.save = broken
    with pytest.raises(OSError):
        asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=True))
    assert tg.sent == 0


def test_private_state_validation_never_silently_resets():
    with pytest.raises(ValueError):
        validate_state({'version': 999})
    with pytest.raises(ValueError):
        validate_state({'version': 1, 'groups': {}, 'posts': []})


def test_revocation_overrides_explicit_public_rule():
    store, tg, cfg = Memory(), Telegram(), config()
    cfg['dry_run'] = False
    store.state['denied_groups'] = [123]
    asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=True))
    assert tg.sent == tg.joined == 0


def test_rules_change_after_join_blocks_post():
    store, tg, cfg = Memory(), Telegram(), config()
    cfg['dry_run'] = False
    original = tg.join
    async def join(group):
        await original(group)
        tg.group.about = 'No promotion'
    tg.join = join
    asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=True))
    assert tg.joined == 1 and tg.sent == 0


def test_persistent_daily_limit_applies_to_all_groups():
    store, tg, cfg = Memory(), Telegram(), config()
    cfg['dry_run'] = False
    store.state['posts'] = {str(i): {'group_id': i, 'at': NOW - 100, 'status': 'confirmed'} for i in (1, 2)}
    asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=True))
    assert tg.sent == 0


def test_known_posting_restrictions_block_joining():
    store, tg, cfg = Memory(), Telegram(), config()
    cfg['dry_run'] = False
    tg.group.posting_allowed = False
    asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=True))
    assert tg.joined == tg.sent == 0


def test_approved_group_minimum_cooldown_is_respected():
    store, tg, cfg = Memory(), Telegram(), config()
    cfg['dry_run'] = False
    tg.group.about = 'Comedy group; ask admin for promotion permission.'
    cfg['approved_groups'] = [{'id': 123, 'rules_hash': rules_hash(tg.group), 'approved': True, 'evidence': 'Admin allows one promotion weekly', 'expires_at': NOW + 7 * 86400, 'cooldown_hours': 168}]
    store.state['posts'] = {'123:123456789ab': {'group_id': 123, 'at': NOW - 72 * 3600, 'status': 'confirmed'}}
    asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=True))
    assert tg.sent == 0
