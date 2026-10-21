import asyncio
from copy import deepcopy
import json

import pytest

from promo.core import Engine, Group, validate_config
from promo.store import validate_state
from test_core import Memory, Telegram, NOW, RULES, config


def posting_config():
    cfg = config()
    cfg['dry_run'] = False
    cfg['limits'].update(posts_per_run=5, posts_per_day=20, cooldown_hours=12)
    return cfg


class JoinedGroups(Telegram):
    def __init__(self):
        super().__init__()
        self.groups = [Group(i, f'comedy_{i}', 'Comedy', RULES, '', True, True)
                       for i in range(100, 106)]
        self.sent_groups = []

    async def list_joined(self, limit):
        return self.groups + self.groups

    async def inspect(self, reference):
        return next(g for g in self.groups if reference in (g.id, g.username))

    async def discover(self, *args, **kwargs):
        pytest.fail('Manual posting must not discover groups')

    async def send(self, group, text, random_id):
        self.sent_groups.append(group.id)
        return await super().send(group, text, random_id)


def test_requested_limits_are_valid():
    assert validate_config(posting_config()) == posting_config()


@pytest.mark.parametrize('name,value', [
    ('posts_per_run', 6), ('posts_per_day', 21), ('cooldown_hours', 11),
    ('posts_per_run', True), ('posts_per_day', 20.0), ('cooldown_hours', 12.0),
])
def test_invalid_posting_limits_are_rejected(name, value):
    cfg = posting_config()
    cfg['limits'][name] = value
    with pytest.raises(ValueError):
        validate_config(cfg)


def test_manual_run_caps_five_distinct_groups_and_preserves_write_ahead():
    store, tg = Memory(), JoinedGroups()
    cfg = posting_config()
    cfg['shorts'].append({'url': 'https://youtu.be/123456789ab', 'caption': 'Another'})
    asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=True, mode='send'))
    assert len(tg.sent_groups) == len(set(tg.sent_groups)) == 5
    assert tg.joined == 0
    assert len(store.state['posts']) == 5
    assert any(p['status'] == 'pending' for s in store.saved for p in s['posts'].values())


@pytest.mark.parametrize('age,expected', [(12 * 3600 - 1, 0), (12 * 3600, 1)])
def test_legacy_confirmed_same_video_reposts_only_after_cooldown(age, expected):
    store, tg = Memory(), Telegram()
    tg.group.member = True
    old = {'group_id': 123, 'video_id': 'abcdefghijk', 'at': NOW - age,
           'status': 'confirmed', 'random_id': 42, 'message_id': 7}
    store.state['posts']['123:abcdefghijk'] = deepcopy(old)
    asyncio.run(Engine(tg, store, posting_config(), now=lambda: NOW).run(live=True, mode='send'))
    assert tg.sent == expected
    assert store.state['posts']['123:abcdefghijk'] == old
    assert len(store.state['posts']) == 1 + expected
    assert validate_state(json.loads(json.dumps(store.state))) == store.state


def test_repeated_same_video_posts_keep_each_attempt_and_count_toward_cap():
    store, tg, cfg = Memory(), Telegram(), posting_config()
    tg.group.member = True
    engine = Engine(tg, store, cfg, now=lambda: NOW)
    for offset in (0, 12 * 3600, 24 * 3600):
        engine.now = lambda offset=offset: NOW + offset
        asyncio.run(engine.run(live=True, mode='send'))
    assert tg.sent == len(store.state['posts']) == 3
    assert len({p['random_id'] for p in store.state['posts'].values()}) == 3
    assert sorted(p['at'] for p in store.state['posts'].values()) == [
        NOW, NOW + 12 * 3600, NOW + 24 * 3600]


@pytest.mark.parametrize('status', ['pending', 'uncertain'])
def test_unresolved_same_video_is_not_reposted_after_cooldown(status):
    store, tg = Memory(), Telegram()
    tg.group.member = True
    old = {'group_id': 123, 'video_id': 'abcdefghijk', 'at': NOW - 48 * 3600,
           'status': status}
    store.state['posts']['123:abcdefghijk'] = deepcopy(old)
    asyncio.run(Engine(tg, store, posting_config(), now=lambda: NOW).run(live=True, mode='send'))
    assert tg.sent == 0
    assert store.state['posts'] == {'123:abcdefghijk': old}


@pytest.mark.parametrize('age,expected', [(86400 - 1, 0), (86400, 5)])
def test_cap_uses_rolling_twenty_four_hours_and_all_attempt_statuses(age, expected):
    store, tg = Memory(), JoinedGroups()
    store.state['posts'] = {
        str(i): {'group_id': i, 'at': NOW - age,
                 'status': ('pending', 'uncertain', 'confirmed')[i % 3]}
        for i in range(20)}
    old = deepcopy(store.state['posts'])
    asyncio.run(Engine(tg, store, posting_config(), now=lambda: NOW).run(live=True, mode='send'))
    assert tg.sent == expected
    assert all(store.state['posts'][key] == value for key, value in old.items())


def test_only_remaining_rolling_budget_is_used():
    store, tg = Memory(), JoinedGroups()
    store.state['posts'] = {str(i): {'group_id': i, 'at': NOW - 23 * 3600,
                                    'status': 'confirmed'} for i in range(19)}
    asyncio.run(Engine(tg, store, posting_config(), now=lambda: NOW).run(live=True, mode='send'))
    assert tg.sent == 1
    assert len(store.state['posts']) == 20


def test_dry_run_leaves_posting_ledger_unchanged():
    store, tg = Memory(), JoinedGroups()
    cfg = posting_config()
    cfg['dry_run'] = True
    asyncio.run(Engine(tg, store, cfg, now=lambda: NOW).run(live=True, mode='send'))
    assert tg.sent == tg.joined == 0
    assert store.state['posts'] == {}
