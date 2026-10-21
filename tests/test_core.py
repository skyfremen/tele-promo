import asyncio
from copy import deepcopy
import pytest

from promo.core import Engine, Group, video_id
from promo.store import validate_state

NOW = 1_800_000_000
RULES = 'YouTube Shorts promotion is allowed.'

def config():
    return {'dry_run': True, 'keywords': ['comedy'], 'shorts': [{'url': 'https://youtube.com/shorts/abcdefghijk', 'caption': 'Original comedy'}], 'limits': {'joins_per_run': 1, 'posts_per_run': 1, 'posts_per_day': 2, 'cooldown_hours': 48, 'discovery_results': 5}}

class Memory:
    def __init__(self):
        self.state = {'version': 1, 'groups': {}, 'posts': {}, 'blocked_until': 0}
        self.saved = []
    def save(self, state):
        self.saved.append(deepcopy(state))

class Telegram:
    def __init__(self, group=None):
        self.group = group or Group(123, 'comedy_test', 'Comedy', RULES, '', True, False, member_count=150)
        self.joined = self.sent = 0
        self.failure = False
    async def list_joined(self, limit):
        return [self.group] if self.group.member else []
    async def discover(self, keywords, limit, exclude_ids=None):
        return [] if self.group.id in (exclude_ids or set()) else [self.group]
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




def test_video_identity_removes_tracking_and_validates_domain():
    assert video_id('https://youtu.be/abcdefghijk?si=abc') == video_id('https://youtube.com/shorts/abcdefghijk')
    with pytest.raises(ValueError):
        video_id('https://youtube.com.attacker.invalid/shorts/abcdefghijk')


def test_dry_run_discovers_without_join_or_post():
    store, tg = Memory(), Telegram()
    asyncio.run(Engine(tg, store, config(), now=lambda: NOW).run(live=False))
    assert tg.joined == tg.sent == 0
    assert store.state['groups']['123']['status'] == 'join_candidate'
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


def test_manual_selection_not_rule_text_controls_posting():
    store, tg, cfg = Memory(), Telegram(), config()
    cfg['dry_run'] = False
    original = tg.join
    async def join(group):
        await original(group)
        tg.group.about = 'No promotion'
    tg.join = join
    asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=True))
    assert tg.joined == 1 and tg.sent == 1


def test_persistent_daily_limit_applies_to_all_groups():
    store, tg, cfg = Memory(), Telegram(), config()
    cfg['dry_run'] = False
    store.state['posts'] = {str(i): {'group_id': i, 'at': NOW - 100, 'status': 'confirmed'} for i in (1, 2)}
    asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=True))
    assert tg.sent == 0


def test_read_only_groups_are_not_joined_or_posted():
    store, tg, cfg = Memory(), Telegram(), config()
    cfg['dry_run'] = False
    tg.group.posting_allowed = False
    asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=True))
    assert tg.joined == tg.sent == 0
    assert store.state['groups']['123']['status'] == 'messaging_disabled'


def test_global_group_cooldown_is_respected():
    store, tg, cfg = Memory(), Telegram(), config()
    cfg['dry_run'] = False
    cfg['limits']['cooldown_hours'] = 168
    store.state['posts'] = {'123:123456789ab': {'group_id': 123, 'at': NOW - 72 * 3600, 'status': 'confirmed'}}
    asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=True))
    assert tg.sent == 0



EXACT_SUB4SUB_MESSAGE = (
    "Here is my link: https://youtu.be/m8IxHy_WPJk\n\n"
    "Please make sure to watch and before subscribing so YouTube doesn't flag it as spam and remove the sub. I will do the exact same for yours.\n\n"
    "DM/Inbox me"
)



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
    tg = Telegram(Group(456, 'sub_for_sub', 'Sub4Sub', 'Sub4Sub is welcome.', '', True, False, member_count=150))
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
        async def discover(self, keywords, limit, exclude_ids=None):
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
    assert events.index('send') < events.index('discover')
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
        async def discover(self, keywords, limit, exclude_ids=None):
            self.searches += 1
            return []
    cfg = config()
    cfg['keywords'] = ['sub4sub']
    store, tg = Memory(), Existing()
    asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=False))
    assert tg.joined == tg.sent == 0
    assert tg.searches == 1


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
        async def discover(self, keywords, limit, exclude_ids=None):
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


def test_join_workflow_never_posts():
    cfg = config()
    cfg['dry_run'] = False
    store, tg = Memory(), Telegram()
    asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=True, mode='join'))
    assert tg.joined == 1 and tg.sent == 0
    assert store.state['groups']['123']['status'] == 'eligible'


def test_selected_group_requires_real_posting_rights():
    cfg = config()
    cfg['dry_run'] = False
    store, tg = Memory(), Telegram()
    tg.group.posting_allowed = False
    asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=True))
    assert tg.joined == tg.sent == 0
    assert store.state['groups']['123']['status'] == 'messaging_disabled'


def test_joined_group_does_not_depend_on_rule_text():
    cfg = config()
    cfg['dry_run'] = False
    cfg['keywords'] = ['sub4sub']
    g = Group(1005, 'sub4sub_private_ok', 'Sub4Sub community',
              'Rules to be checked with group admin externally',
              '', True, False, rules_complete=False, member_count=800)
    class Selected(Telegram):
        async def list_joined(self, limit):
            return []
    store, tg = Memory(), Selected(g)
    asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=True))
    assert tg.joined == tg.sent == 1


def test_read_only_group_is_rejected_without_selection_or_shorts():
    cfg = config()
    cfg['dry_run'] = False
    cfg['shorts'] = []
    cfg['keywords'] = ['sub4sub']
    cfg['limits']['min_group_members'] = 500
    g = Group(7000, 'public_sub4sub', 'Sub4Sub community',
              'Please read rules before promoting.', '', True, False,
              rules_complete=False, posting_allowed=False, member_count=800)
    store, tg = Memory(), Telegram(g)
    asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=True))
    assert tg.joined == tg.sent == 0
    assert store.state['groups']['7000']['member'] is False
    assert store.state['groups']['7000']['status'] == 'messaging_disabled'
    assert not store.state['posts']


def test_join_only_still_respects_500_member_minimum():
    cfg = config()
    cfg['dry_run'] = False
    cfg['keywords'] = ['sub4sub']
    cfg['limits']['min_group_members'] = 500
    for count in (None, 0, 499):
        g = Group(7001, 'small_sub4sub', 'Sub4Sub', '', '', True, False,
                  member_count=count)
        store, tg = Memory(), Telegram(g)
        asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=True))
        assert tg.joined == tg.sent == 0


def test_join_only_dry_run_and_disallowed_group_stay_blocked():
    cfg = config()
    cfg['keywords'] = ['sub4sub']
    cfg['limits']['min_group_members'] = 500
    g = Group(7002, 'large_sub4sub', 'Sub4Sub', '', '', True, False,
              member_count=1000)
    store, tg = Memory(), Telegram(g)
    asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=True))
    assert tg.joined == 0
    cfg['dry_run'] = False
    store.state['denied_groups'] = [7002]
    asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=True))
    assert tg.joined == 0


def test_join_only_one_per_run_and_repeat_attempt_is_suppressed():
    class TwoGroups(Telegram):
        def __init__(self):
            super().__init__()
            self.groups = {
                'first_sub4sub': Group(7101, 'first_sub4sub', 'Sub4Sub 1',
                                      '', '', True, False, member_count=900),
                'second_sub4sub': Group(7102, 'second_sub4sub', 'Sub4Sub 2',
                                       '', '', True, False, member_count=900),
            }
        async def discover(self, keywords, limit, exclude_ids=None):
            return list(self.groups.values())
        async def inspect(self, username):
            return self.groups[username]
        async def join(self, group):
            self.joined += 1
            self.groups[group.username].member = True

    cfg = config()
    cfg['dry_run'] = False
    cfg['keywords'] = ['sub4sub']
    store, tg = Memory(), TwoGroups()
    engine = Engine(tg, store, cfg, now=lambda: NOW)
    asyncio.run(engine.run(live=True, mode='join'))
    assert tg.joined == 1 and tg.sent == 0
    assert sum(g['member'] for g in store.state['groups'].values()) == 1
    asyncio.run(engine.run(live=True, mode='join'))
    assert tg.joined == 2 and tg.sent == 0
    asyncio.run(engine.run(live=True, mode='join'))
    assert tg.joined == 2


def test_no_public_username_cannot_trigger_automated_join():
    cfg = config()
    cfg['dry_run'] = False
    cfg['keywords'] = ['sub4sub']
    group = Group(7201, '', 'Sub4Sub private group', '', '',
                  True, False, member_count=900)
    store, tg = Memory(), Telegram(group)
    asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=True))
    assert tg.joined == tg.sent == 0


def test_join_without_shorts_only_for_writable_groups():
    cfg = config()
    cfg['dry_run'] = False
    cfg['shorts'] = []
    cfg['keywords'] = ['sub4sub']
    cfg['limits']['min_group_members'] = 500
    g = Group(7300, 'writeable_sub4sub', 'Sub4Sub', '', '', True, False,
              posting_allowed=True, member_count=700)
    store, tg = Memory(), Telegram(g)
    asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=True))
    assert tg.joined == 1 and tg.sent == 0
    assert store.state['groups']['7300']['status'] == 'eligible'
    assert store.state['groups']['7300']['messaging_allowed']


def test_member_specific_read_only_rights_block_posting_after_join():
    class MemberBannedTelegram(Telegram):
        async def can_post(self, group):
            return False
    cfg = config()
    cfg['dry_run'] = False
    cfg['keywords'] = ['sub4sub']
    g = Group(7301, 'sub4sub_member_restricted', 'Sub4Sub',
              '', '', True, False, posting_allowed=True, member_count=800)
    store, tg = Memory(), MemberBannedTelegram(g)
    asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=True))
    assert tg.joined == 1 and tg.sent == 0
    assert store.state['groups']['7301']['status'] == 'messaging_disabled'
    assert store.state['groups']['7301']['messaging_allowed'] is False


def test_existing_member_specific_read_only_rights_are_recorded():
    class ExistingReadOnly(Telegram):
        def __init__(self):
            super().__init__(Group(7302, 'sub4sub_read_only', 'Sub4Sub',
                                   '', '', True, True, posting_allowed=True,
                                   member_count=500))
        async def list_joined(self, limit):
            return [self.group]
        async def can_post(self, group):
            return False
    cfg = config()
    cfg['dry_run'] = False
    cfg['keywords'] = ['sub4sub']
    store, tg = Memory(), ExistingReadOnly()
    asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=True))
    assert tg.joined == tg.sent == 0
    assert store.state['groups']['7302']['status'] == 'messaging_disabled'


def test_discovery_excludes_existing_memberships_from_ten_group_target():
    class NewGroups(Telegram):
        def __init__(self):
            super().__init__()
            self.seen_exclusions = None
            self.inspected = []
            self.groups = {}
            for idx in range(10):
                gid = 8000 + idx
                group = Group(gid, f'sub4sub_{gid}', f'Sub4Sub {idx}', '', '',
                              True, False, member_count=850)
                self.groups[group.username] = group
        async def list_joined(self, limit):
            return [Group(123, 'sub4sub_old', 'Sub4Sub old', '', '',
                          True, True, member_count=1000)]
        async def discover(self, keywords, limit, exclude_ids=None):
            self.seen_exclusions = set(exclude_ids or [])
            assert limit == 10
            return list(self.groups.values())
        async def inspect(self, reference):
            self.inspected.append(reference)
            if reference == 'sub4sub_old':
                return Group(123, 'sub4sub_old', 'Sub4Sub old', '', '',
                             True, True, member_count=1000)
            return self.groups[reference]
        async def join(self, group):
            self.joined += 1
            group.member = True

    cfg = config()
    cfg['keywords'] = ['sub4sub']
    cfg['limits']['discovery_results'] = 10
    cfg['limits']['joins_per_run'] = 2
    cfg['dry_run'] = False
    store, tg = Memory(), NewGroups()
    asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=True, mode='join'))
    assert tg.seen_exclusions == {123}
    assert len(set(tg.inspected)) == 11
    assert tg.joined == 2 and tg.sent == 0
    assert len([g for g in store.state['groups'].values() if g['source'] == 'discovery']) == 10


def test_discovery_in_dry_run_still_runs_with_existing_selected_group():
    class JoinedAndNew(Telegram):
        def __init__(self):
            super().__init__(Group(1001, 'old', 'Sub4Sub old', '', '',
                                   True, True, member_count=600))
            self.discovered_count = 0
        async def list_joined(self, limit):
            return [self.group]
        async def discover(self, keywords, limit, exclude_ids=None):
            self.discovered_count += 1
            assert 1001 in exclude_ids
            return []
    cfg = config()
    cfg['keywords'] = ['sub4sub']
    cfg['limits']['discovery_results'] = 10
    store, tg = Memory(), JoinedAndNew()
    asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=False))
    assert tg.discovered_count == 1
    assert tg.joined == tg.sent == 0


def test_telegram_discovery_deduplicates_and_excludes_joined_ids(monkeypatch):
    from types import SimpleNamespace
    import promo.telegram as telegram_mod

    class FakeChannel:
        def __init__(self, gid, joined=False):
            self.id = gid
            self.username = f'sub4sub_{gid}'
            self.title = f'Sub4Sub {gid}'
            self.megagroup = True
            self.scam = self.fake = self.restricted = False
            self.left = not joined
            self.participants_count = 800

    class FakeClient:
        def __init__(self):
            self.queries = []
        async def __call__(self, request):
            self.queries.append((request.q, request.limit))
            if request.q == 'first':
                groups = [FakeChannel(101), FakeChannel(102),
                          FakeChannel(103, joined=True)]
                groups += [FakeChannel(gid) for gid in range(200, 208)]
            else:
                groups = [FakeChannel(gid) for gid in range(200, 211)]
            return SimpleNamespace(chats=groups)

    monkeypatch.setattr(telegram_mod.types, 'Channel', FakeChannel)
    telegram = object.__new__(telegram_mod.Telegram)
    telegram.client = FakeClient()
    groups = asyncio.run(telegram.discover(['first', 'second', 'third'], 10,
                                            exclude_ids={101, 102}))
    assert len(groups) == 10
    assert len({g.id for g in groups}) == 10
    assert all(not g.member for g in groups)
    assert {g.id for g in groups} == set(range(200, 210))
    assert telegram.client.queries == [('first', 20), ('second', 20)]


def test_join_mode_never_posts_even_when_selected_and_live():
    cfg = config()
    cfg['dry_run'] = False
    store, tg = Memory(), Telegram()
    asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=True, mode='join'))
    assert tg.joined == 1
    assert tg.sent == 0
    assert store.state['posts'] == {}


def test_send_mode_never_discovers_or_joins_new_groups():
    class WatchTelegram(Telegram):
        def __init__(self):
            super().__init__()
            self.searches = 0
        async def discover(self, keywords, limit, exclude_ids=None):
            self.searches += 1
            return await super().discover(keywords, limit, exclude_ids)
    cfg = config()
    cfg['dry_run'] = False
    store, tg = Memory(), WatchTelegram()
    store.state['groups']['123'] = {'username': 'comedy_test', 'member': False}
    asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=True, mode='send'))
    assert tg.searches == tg.joined == tg.sent == 0
    assert not store.state['posts']


def test_send_mode_posts_to_already_joined_group():
    cfg = config()
    cfg['dry_run'] = False
    store, tg = Memory(), Telegram()
    tg.group.member = True
    store.state['groups']['123'] = {'username': 'comedy_test', 'member': True}
    asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=True, mode='send'))
    assert tg.joined == 0
    assert tg.sent == 1
    assert len(store.state['posts']) == 1


def test_send_mode_scans_saved_joined_group_outside_dialog_window():
    class MissingDialog(Telegram):
        async def list_joined(self, limit):
            return []
        async def discover(self, keywords, limit, exclude_ids=None):
            raise AssertionError('sending must not perform discovery')
    cfg = config()
    cfg['dry_run'] = False
    store, tg = Memory(), MissingDialog()
    tg.group.member = True
    store.state['groups']['123'] = {'username': 'comedy_test', 'member': True}
    asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=True, mode='send'))
    assert tg.joined == 0 and tg.sent == 1


def test_send_mode_posts_to_joined_relevant_group_without_selection():
    cfg = config()
    cfg['dry_run'] = False
    store, tg = Memory(), Telegram()
    tg.group.member = True
    asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=True, mode='send'))
    assert tg.joined == 0 and tg.sent == 1
    assert len(store.state['posts']) == 1


def test_run_mode_validation():
    cfg = config()
    store, tg = Memory(), Telegram()
    with pytest.raises(ValueError):
        asyncio.run(Engine(tg, store, cfg).run(mode='unsupported'))


def test_extended_discovery_search_joins_from_second_batch():
    class SecondBatch(Telegram):
        def __init__(self):
            super().__init__()
            self.calls = 0
            self.groups = {
                'small': Group(111, 'small', 'Comedy group', '', '', True, False, member_count=5),
                'large': Group(222, 'large', 'Comedy group', '', '', True, False, member_count=800),
            }
        async def list_joined(self, limit):
            return []
        async def discover(self, keywords, limit, exclude_ids=None):
            self.calls += 1
            return [self.groups['small']] if self.calls == 1 else [self.groups['large']]
        async def inspect(self, reference):
            return self.groups[reference]
        async def join(self, group):
            self.joined += 1
            group.member = True
    cfg = config()
    cfg['dry_run'] = False
    cfg['limits']['discovery_results'] = 1
    store, tg = Memory(), SecondBatch()
    asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=True, mode='join'))
    assert tg.calls == 2
    assert tg.joined == 1 and tg.sent == 0
    assert store.state['groups']['222']['member'] is True


def test_extended_discovery_stops_when_no_new_candidates():
    class NoCandidates(Telegram):
        def __init__(self):
            super().__init__()
            self.calls = 0
        async def list_joined(self, limit):
            return []
        async def discover(self, keywords, limit, exclude_ids=None):
            self.calls += 1
            return []
    cfg = config()
    cfg['dry_run'] = False
    store, tg = Memory(), NoCandidates()
    asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=True, mode='join'))
    assert tg.calls == 2
    assert tg.joined == tg.sent == 0


def test_candidate_links_review_before_discovery_and_status_saved():
    from promo.core import public_group_username, has_promotion_permission
    assert public_group_username('https://t.me/Comedy_test/') == 'Comedy_test'
    for url in ('http://t.me/comedy_test', 'https://t.me/+invite',
                'https://t.me/comedy_test/123', 'https://example.com/comedy_test'):
        with pytest.raises(ValueError):
            public_group_username(url)
    assert has_promotion_permission(Group(1, 'comedy_test', 'Comedy', RULES, '', True, False))
    assert not has_promotion_permission(
        Group(1, 'comedy_test', 'Comedy', 'No YouTube promotion allowed.', '', True, False))

    class CandidateStore(Memory):
        def __init__(self):
            super().__init__()
            self.candidates = {'candidates': [
                {'url': 'https://t.me/comedy_test', 'source': 'chatgpt', 'status': 'pending'}]}
        def load_candidates(self):
            return deepcopy(self.candidates)
        def save_candidates(self, document):
            self.candidates = deepcopy(document)

    class CandidateFirst(Telegram):
        async def discover(self, keywords, limit, exclude_ids=None):
            assert self.joined == 1  # Candidate processing precedes search.
            return []
    cfg = config()
    cfg['dry_run'] = False
    store, tg = CandidateStore(), CandidateFirst()
    asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=True, mode='join'))
    entry = store.candidates['candidates'][0]
    assert entry['status'] == 'joined' and entry['group_id'] == 123
    assert tg.joined == 1 and tg.sent == 0


def test_candidate_without_explicit_permission_cannot_rejoin_via_discovery():
    class CandidateStore(Memory):
        def __init__(self):
            super().__init__()
            self.candidates = {'candidates': [
                {'url': 'https://t.me/comedy_test', 'status': 'pending'},
                {'url': 'https://t.me/Comedy_Test', 'status': 'pending'}]}
        def load_candidates(self):
            return deepcopy(self.candidates)
        def save_candidates(self, document):
            self.candidates = deepcopy(document)
    cfg = config()
    cfg['dry_run'] = False
    g = Group(123, 'comedy_test', 'Comedy', 'No YouTube promotion allowed.',
              '', True, False, member_count=900)
    store, tg = CandidateStore(), Telegram(g)
    asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=True, mode='join'))
    assert tg.joined == tg.sent == 0
    assert store.candidates['candidates'][0]['status'] == 'promotion_unverified'
    assert store.candidates['candidates'][1]['status'] == 'duplicate'
    # The same group cannot be silently joined through ordinary discovery.
    asyncio.run(Engine(tg, store, cfg, now=lambda: NOW + 100).run(live=True, mode='join'))
    assert tg.joined == 0


def test_candidate_list_never_used_for_manual_sending():
    class CandidateStore(Memory):
        def load_candidates(self):
            raise AssertionError('Manual sending must not read candidate links')
    cfg = config()
    cfg['dry_run'] = False
    store, tg = CandidateStore(), Telegram()
    tg.group.member = True
    asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=True, mode='send'))
    assert tg.sent == 1 and tg.joined == 0
