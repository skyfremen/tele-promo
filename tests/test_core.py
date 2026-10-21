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
    async def list_joined(self, limit):
        return []
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



EXACT_SUB4SUB_MESSAGE = (
    "Here is my link: https://youtu.be/m8IxHy_WPJk\n\n"
    "Please make sure to watch and before subscribing so YouTube doesn't flag it as spam and remove the sub. I will do the exact same for yours.\n\n"
    "DM/Inbox me"
)


def test_sub4sub_permission_requires_explicit_standalone_rules():
    group = Group(456, 'sub_for_sub', 'Sub4Sub', 'Sub4Sub is welcome.', '', True, False)
    assert permitted(group, [], NOW)
    group.pinned = 'No promotion'
    assert not permitted(group, [], NOW)
    group.about = 'Sub4Sub is welcome, but only at designated times'
    group.pinned = ''
    assert not permitted(group, [], NOW)


def test_exact_message_validation_and_identity():
    from promo.core import validate_config
    cfg = config()
    cfg['shorts'] = [{'url': 'https://youtu.be/m8IxHy_WPJk', 'message': EXACT_SUB4SUB_MESSAGE}]
    assert validate_config(cfg) == cfg
    cfg['shorts'][0]['message'] += '\nhttps://t.me/second_link'
    with pytest.raises(ValueError):
        validate_config(cfg)
    cfg['shorts'][0]['message'] = EXACT_SUB4SUB_MESSAGE.replace('m8IxHy_WPJk', 'abcdefghijk')
    with pytest.raises(ValueError):
        validate_config(cfg)


def test_exact_message_sent_unchanged_to_explicitly_permitting_group():
    cfg = config()
    cfg['dry_run'] = False
    cfg['keywords'] = ['sub4sub']
    cfg['shorts'] = [{'url': 'https://youtu.be/m8IxHy_WPJk', 'message': EXACT_SUB4SUB_MESSAGE}]
    tg = Telegram(Group(456, 'sub_for_sub', 'Sub4Sub', 'Sub4Sub is welcome.', '', True, False))
    received = []
    original_send = tg.send

    async def capture(group, text, random_id):
        received.append(text)
        return await original_send(group, text, random_id)

    tg.send = capture
    store = Memory()
    asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=True))
    assert tg.joined == 1 and tg.sent == 1
    assert received == [EXACT_SUB4SUB_MESSAGE]
    assert len(store.state['posts']) == 1


def test_exact_message_dry_run_cannot_post():
    cfg = config()
    cfg['keywords'] = ['sub4sub']
    cfg['shorts'] = [{'url': 'https://youtu.be/m8IxHy_WPJk', 'message': EXACT_SUB4SUB_MESSAGE}]
    tg = Telegram(Group(456, 'sub_for_sub', 'Sub4Sub', 'Sub4Sub is welcome.', '', True, False))
    store = Memory()
    asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=False))
    assert tg.joined == tg.sent == 0


def test_existing_joined_group_is_prioritized_and_never_rejoined():
    events = []
    class ExistingFirst(Telegram):
        def __init__(self):
            super().__init__(Group(1001, '', 'Sub4Sub friends',
                                   'Sub4Sub is welcome.', '', True, True,
                                   member_count=12))
        async def list_joined(self, limit):
            events.append('list_joined')
            return [self.group]
        async def discover(self, keywords, limit):
            events.append('discover')
            return []
        async def inspect(self, reference):
            assert reference == self.group.id
            events.append('inspect')
            return self.group
        async def send(self, group, text, random_id):
            events.append('send')
            return await super().send(group, text, random_id)
    cfg = config()
    cfg['dry_run'] = False
    cfg['keywords'] = ['sub4sub']
    cfg['limits']['min_group_members'] = 100
    store, tg = Memory(), ExistingFirst()
    asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=True))
    assert events[0] == 'list_joined'
    assert 'discover' not in events
    assert tg.joined == 0 and tg.sent == 1
    assert store.state['groups']['1001']['status'] == 'eligible'
    assert store.state['groups']['1001']['member_count'] == 12
    assert store.state['groups']['1001']['source'] == 'already_joined'


def test_below_threshold_or_unknown_members_are_never_joined():
    class CountedDiscovery(Telegram):
        def __init__(self, count):
            super().__init__(Group(1002, 'sub_for_sub_group', 'Sub4Sub',
                                   'Sub4Sub is welcome.', '', True, False,
                                   member_count=count))
        async def list_joined(self, limit):
            return []
    for count, expected in [(99, 'below_member_minimum'),
                             (None, 'member_count_unknown')]:
        cfg = config()
        cfg['dry_run'] = False
        cfg['keywords'] = ['sub4sub']
        cfg['limits']['min_group_members'] = 100
        store, tg = Memory(), CountedDiscovery(count)
        asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=True))
        assert tg.joined == 0 and tg.sent == 0
        assert store.state['groups']['1002']['status'] == expected


def test_meets_minimum_new_group_can_join():
    class CountedDiscovery(Telegram):
        async def list_joined(self, limit):
            return []
    cfg = config()
    cfg['dry_run'] = False
    cfg['keywords'] = ['sub4sub']
    cfg['limits']['min_group_members'] = 100
    group = Group(1003, 'sub4sub_group', 'Sub4Sub',
                  'Sub4Sub is welcome.', '', True, False, member_count=100)
    store, tg = Memory(), CountedDiscovery(group)
    asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=True))
    assert tg.joined == tg.sent == 1


def test_dry_run_scans_joined_first_without_global_search_when_eligible():
    class Existing(Telegram):
        def __init__(self):
            super().__init__(Group(1004, 'sub4sub_existing', 'Sub4Sub',
                                   'Sub4Sub is welcome.', '', True, True))
            self.searches = 0
        async def list_joined(self, limit):
            return [self.group]
        async def discover(self, keywords, limit):
            self.searches += 1
            return []
    cfg = config()
    cfg['keywords'] = ['sub4sub']
    store, tg = Memory(), Existing()
    asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=False))
    assert tg.joined == tg.sent == 0
    assert tg.searches == 0


def test_joined_scan_rotates_over_unrelated_group_titles():
    class ManyExisting(Telegram):
        def __init__(self):
            super().__init__()
            self.checked = []
        async def list_joined(self, limit):
            return [Group(2001, 'first', 'General A', '', '', True, True),
                    Group(2002, 'second', 'General B', '', '', True, True)]
        async def inspect(self, ref):
            self.checked.append(ref)
            return Group(2001 if ref == 'first' else 2002, ref,
                         'General', '', '', True, True)
        async def discover(self, keywords, limit):
            return []
    cfg = config()
    cfg['keywords'] = ['sub4sub']
    cfg['limits']['joined_inspect_limit'] = 1
    store, tg = Memory(), ManyExisting()
    engine = Engine(tg, store, cfg, now=lambda: NOW)
    asyncio.run(engine.run(live=False))
    asyncio.run(engine.run(live=False))
    assert tg.checked[:2] == ['first', 'second']


def test_minimum_group_member_config_is_validated():
    from promo.core import validate_config
    for invalid in [-1, 1000001, '100', True]:
        cfg = config()
        cfg['limits']['min_group_members'] = invalid
        with pytest.raises(ValueError):
            validate_config(cfg)
    cfg = config()
    cfg['limits']['min_group_members'] = 500
    assert validate_config(cfg) == cfg
