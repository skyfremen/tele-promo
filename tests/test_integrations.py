import asyncio
import base64
from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from urllib.error import HTTPError
import pytest
from telethon import types
from telethon.errors import FloodWaitError
from promo.core import Group
from promo.store import GitHubStore
from promo.telegram import Telegram
from promo import __main__ as cli
from test_core import Memory, config


def test_state_refuses_public_repository(monkeypatch):
    monkeypatch.setenv('STATE_REPO', 'owner/state')
    monkeypatch.setenv('STATE_TOKEN', 'fake-test-value')
    monkeypatch.setattr(GitHubStore, 'request', lambda *a: {'private': False})
    with pytest.raises(ValueError, match='private'):
        GitHubStore()


def test_state_compare_and_swap_and_reload(monkeypatch):
    monkeypatch.setenv('STATE_REPO', 'owner/state')
    monkeypatch.setenv('STATE_TOKEN', 'fake-test-value')
    state = {'version': 1, 'groups': {}, 'posts': {}, 'blocked_until': 0}
    writes = []
    def request(self, method, url, data=None):
        if method == 'PUT':
            writes.append(deepcopy(data))
            return {'content': {'sha': 'new-sha'}}
        if '/contents/' in url:
            return {'sha': 'old-sha', 'content': base64.b64encode(json.dumps(state).encode()).decode()}
        return {'private': True, 'default_branch': 'main'}
    monkeypatch.setattr(GitHubStore, 'request', request)
    store = GitHubStore()
    store.save(store.state)
    assert writes[0]['sha'] == 'old-sha'
    assert json.loads(base64.b64decode(writes[0]['content'])) == state
    store.save(store.state)
    assert writes[1]['sha'] == 'new-sha'


def test_missing_state_is_not_automatically_initialized(monkeypatch):
    monkeypatch.setenv('STATE_REPO', 'owner/state')
    monkeypatch.setenv('STATE_TOKEN', 'fake-test-value')
    def request(self, method, url, data=None):
        if '/contents/' in url:
            raise HTTPError(url, 404, 'not found', {}, None)
        return {'private': True, 'default_branch': 'main'}
    monkeypatch.setattr(GitHubStore, 'request', request)
    assert GitHubStore().state is None


def channel(**kwargs):
    return types.Channel(id=123, title='Comedy', photo=types.ChatPhotoEmpty(), date=datetime.now(timezone.utc), megagroup=True, username='comedy_test', left=False, **kwargs)


class Client:
    def __init__(self, entity, participant):
        self.entity, self.participant = entity, participant
    async def get_entity(self, _):
        return self.entity
    async def __call__(self, request):
        return SimpleNamespace(participant=self.participant)


def test_telegram_permissions_respect_text_and_link_restrictions():
    tg = object.__new__(Telegram)
    g = Group(123, 'comedy_test', 'Comedy', '', '', True, True)
    participant = types.ChannelParticipant(user_id=999, date=datetime.now(timezone.utc))
    for field in ('send_messages', 'send_plain', 'embed_links', 'view_messages'):
        rights = types.ChatBannedRights(until_date=None, **{field: True})
        tg.client = Client(channel(default_banned_rights=rights), participant)
        assert not asyncio.run(tg.can_post(g))
    tg.client = Client(channel(), participant)
    assert asyncio.run(tg.can_post(g))
    tg.client = Client(channel(), types.ChannelParticipantLeft(types.PeerUser(999)))
    assert not asyncio.run(tg.can_post(g))


def test_floodwait_saved_without_retry_or_private_logs(monkeypatch, capsys):
    store = Memory()
    store.state['config'] = config()
    class Limited:
        def __init__(self, *a):
            self.client = self
        async def connect(self):
            raise FloodWaitError(request=None, capture=600)
        async def disconnect(self):
            pass
    monkeypatch.setattr(cli, 'GitHubStore', lambda: store)
    monkeypatch.setattr(cli, 'Telegram', Limited)
    for name in ('TG_SESSION', 'TG_API_ID', 'TG_API_HASH'):
        monkeypatch.setenv(name, 'private-test-value')
    monkeypatch.setattr('sys.argv', ['promo', 'run'])
    cli.main()
    assert store.state['blocked_until'] > 0
    assert 'private-test-value' not in capsys.readouterr().out


def test_candidate_json_round_trip_preserves_list_and_uses_sha(monkeypatch):
    monkeypatch.setenv('STATE_REPO', 'owner/state')
    monkeypatch.setenv('STATE_TOKEN', 'fake-test-value')
    state = {'version': 1, 'groups': {}, 'posts': {}, 'blocked_until': 0}
    candidates = {'candidates': [
        {'url': 'https://t.me/testgroup', 'status': 'pending', 'source': 'chatgpt'},
        {'url': 'https://t.me/othergroup', 'status': 'joined', 'group_id': 123},
    ]}
    writes = []
    def request(self, method, url, data=None):
        if method == 'PUT':
            writes.append(deepcopy(data))
            return {'content': {'sha': 'updated-candidate-sha'}}
        if '/contents/candidate-groups.json' in url:
            return {'sha': 'candidate-sha',
                    'content': base64.b64encode(json.dumps(candidates).encode()).decode()}
        if '/contents/state.json' in url:
            return {'sha': 'state-sha',
                    'content': base64.b64encode(json.dumps(state).encode()).decode()}
        return {'private': True, 'default_branch': 'main'}
    monkeypatch.setattr(GitHubStore, 'request', request)
    store = GitHubStore()
    assert store.state['posts'] == {}
    document = store.load_candidates()
    document['candidates'][0]['status'] = 'promotion_unverified'
    store.save_candidates(document)
    assert writes[0]['sha'] == 'candidate-sha'
    assert json.loads(base64.b64decode(writes[0]['content'])) == document
    assert len(document['candidates']) == 2
    assert store.sha == 'state-sha'
    assert store.candidate_sha == 'updated-candidate-sha'
