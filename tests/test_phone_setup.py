import asyncio
import json
import pytest
from telethon.errors import SessionPasswordNeededError
from promo.phone_setup import Setup


class Vault:
    def __init__(self):
        self.values, self.deleted = {}, []
    def put(self, name, value):
        self.values[name] = value
    def delete(self, name):
        self.deleted.append(name)
        self.values.pop(name, None)


class Client:
    def __init__(self):
        self.session = self
        self.sent = self.signed = self.password_signed = 0
        self.need_password = False
        self.disconnected = False
        self.authorized = False
    async def connect(self):
        pass
    async def disconnect(self):
        self.disconnected = True
    def save(self):
        return 'test-session-value'
    async def send_code_request(self, phone):
        self.sent += 1
        return type('Sent', (), {'phone_code_hash': 'test-code-hash'})()
    async def is_user_authorized(self):
        return self.authorized
    async def sign_in(self, **kwargs):
        self.signed += 1
        if 'password' in kwargs:
            self.password_signed += 1
        elif self.need_password:
            raise SessionPasswordNeededError(request=None)
        self.authorized = True
    async def get_me(self):
        return type('Me', (), {'bot': False})()


def setup():
    vault, client = Vault(), Client()
    return Setup(vault, lambda session: client, now=lambda: 1000), vault, client


def test_request_keeps_temporary_session_in_secrets_only():
    runner, vault, client = setup()
    asyncio.run(runner.request_code('+6500000000', existing_session=''))
    data = json.loads(vault.values['TG_LOGIN_PENDING'])
    assert data['session'] == 'test-session-value'
    assert data['phone_code_hash'] == 'test-code-hash'
    assert client.sent == 1 and client.disconnected
    assert 'TG_SESSION' not in vault.values


def test_secret_write_failure_prevents_requesting_code():
    runner, vault, client = setup()
    def broken(*args):
        raise OSError('No permission')
    vault.put = broken
    with pytest.raises(OSError):
        asyncio.run(runner.request_code('+6500000000', existing_session=''))
    assert client.sent == 0 and client.disconnected


def test_success_stores_final_session_and_removes_setup_secrets(capsys):
    runner, vault, client = setup()
    asyncio.run(runner.request_code('+6500000000', existing_session=''))
    result = asyncio.run(runner.finish(vault.values['TG_LOGIN_PENDING'], code='12345', password=''))
    assert result == 'complete'
    assert vault.values['TG_SESSION'] == 'test-session-value'
    assert 'TG_LOGIN_PENDING' in vault.deleted
    assert 'TG_CODE' in vault.deleted and 'SETUP_TOKEN' in vault.deleted
    assert 'test-session-value' not in capsys.readouterr().out


def test_two_factor_can_finish_without_reusing_consumed_code():
    runner, vault, client = setup()
    asyncio.run(runner.request_code('+6500000000', existing_session=''))
    client.need_password = True
    assert asyncio.run(runner.finish(vault.values['TG_LOGIN_PENDING'], code='12345', password='')) == 'password_required'
    assert 'TG_SESSION' not in vault.values
    assert json.loads(vault.values['TG_LOGIN_PENDING'])['password_required'] is True
    assert asyncio.run(runner.finish(vault.values['TG_LOGIN_PENDING'], code='', password='test-password')) == 'complete'
    assert client.password_signed == 1


def test_expired_authentication_and_existing_sessions_are_protected():
    runner, vault, client = setup()
    with pytest.raises(ValueError):
        asyncio.run(runner.request_code('+6500000000', existing_session='already-set'))
    assert client.sent == 0
    asyncio.run(runner.request_code('+6500000000', existing_session=''))
    runner.now = lambda: 2000
    with pytest.raises(ValueError):
        asyncio.run(runner.finish(vault.values['TG_LOGIN_PENDING'], code='12345', password=''))
    assert client.signed == 0


def test_final_secret_failure_preserves_recovery_state():
    runner, vault, client = setup()
    asyncio.run(runner.request_code('+6500000000', existing_session=''))
    original = vault.put
    def put(name, value):
        if name == 'TG_SESSION':
            raise OSError('Cannot save')
        original(name, value)
    vault.put = put
    with pytest.raises(OSError):
        asyncio.run(runner.finish(vault.values['TG_LOGIN_PENDING'], code='12345', password=''))
    assert not vault.deleted
    assert 'TG_LOGIN_PENDING' in vault.values
    vault.put = original
    assert asyncio.run(runner.finish(vault.values['TG_LOGIN_PENDING'], code='', password='')) == 'complete'


def test_repeated_code_requests_are_blocked():
    runner, vault, client = setup()
    asyncio.run(runner.request_code('+6500000000', existing_session=''))
    with pytest.raises(ValueError):
        asyncio.run(runner.request_code('+6500000000', existing_session='', previous=vault.values['TG_LOGIN_PENDING']))
    assert client.sent == 1


def test_cleanup_ignores_secrets_that_were_never_set(monkeypatch):
    from types import SimpleNamespace
    from promo.phone_setup import Vault as GitHubVault
    monkeypatch.setattr('promo.phone_setup.subprocess.run', lambda *a, **kw: SimpleNamespace(returncode=1, stderr='HTTP 404: Not Found'))
    GitHubVault('owner/repo').delete('TG_PASSWORD')


def test_partial_request_can_resume_once_without_overriding_cooldowns():
    runner, vault, client = setup()
    partial = json.dumps({'session': 'test-session-value', 'phone': '+6500000000', 'created_at': 800, 'phone_code_hash': '', 'password_required': False})
    asyncio.run(runner.request_code('+6500000000', existing_session='', previous=partial, resume=True))
    assert client.sent == 1
    blocked = json.dumps({'session': 'test-session-value', 'phone': '+6500000000', 'created_at': 800, 'phone_code_hash': '', 'retry_after': 2000})
    with pytest.raises(ValueError):
        asyncio.run(runner.request_code('+6500000000', existing_session='', previous=blocked, resume=True))
    assert client.sent == 1


def test_error_diagnostics_never_include_exception_payload():
    from promo.phone_setup import safe_error
    message = safe_error(ValueError('sensitive-private-value'))
    assert 'ValueError' in message
    assert 'sensitive-private-value' not in message
