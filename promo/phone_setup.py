"""One-time manual Actions login. Temporary authentication lives only in Secrets."""
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, SessionPasswordNeededError


class Vault:
    def __init__(self, repository):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
            raise ValueError('Invalid repository')
        self.repository = repository

    def put(self, name, value):
        self._run(['gh', 'secret', 'set', name, '--repo', self.repository], value)

    def delete(self, name):
        self._run(['gh', 'secret', 'delete', name, '--repo', self.repository], missing_ok=True)

    @staticmethod
    def _run(args, value=None, missing_ok=False):
        # Secret bodies use stdin. Never put them in arguments or print stderr.
        result = subprocess.run(args, input=value, text=True, capture_output=True,
                                timeout=45, check=False)
        if missing_ok and result.returncode and 'HTTP 404' in result.stderr:
            return
        if result.returncode:
            raise RuntimeError('Secret operation failed')


class Setup:
    TEMPORARY = ('TG_LOGIN_PENDING', 'TG_PHONE', 'TG_CODE', 'TG_PASSWORD', 'SETUP_TOKEN')

    def __init__(self, vault, client_factory, now=time.time):
        self.vault, self.client_factory, self.now = vault, client_factory, now

    def save_pending(self, pending):
        self.vault.put('TG_LOGIN_PENDING', json.dumps(pending))

    async def request_code(self, phone, existing_session, previous='', resume=False):
        if existing_session:
            raise ValueError('A final session already exists')
        old_session = ''
        if previous:
            pending = json.loads(previous)
            if pending.get('retry_after', 0) > self.now():
                raise ValueError('Telegram cooldown still active')
            if self.now() - pending['created_at'] < 900:
                if not resume or pending.get('phone_code_hash') or self.now() - pending['created_at'] < 60 or pending.get('resume_attempted'):
                    raise ValueError('Request already pending or cooling down')
                if pending['phone'] != phone:
                    raise ValueError('Phone changed during setup')
                old_session = pending['session']
        if not re.fullmatch(r'\+[1-9][0-9]{6,14}', phone):
            raise ValueError('Phone must include country code')
        client = self.client_factory(old_session)
        try:
            await client.connect()
            pending = {'session': client.session.save(), 'phone': phone,
                       'created_at': self.now(), 'phone_code_hash': '',
                       'password_required': False, 'resume_attempted': resume}
            # Verify write access before asking Telegram to send a code.
            self.save_pending(pending)
            print('Temporary authentication saved; requesting Telegram code.')
            try:
                sent = await client.send_code_request(phone)
            except FloodWaitError as exc:
                pending['retry_after'] = self.now() + exc.seconds + 60
                self.save_pending(pending)
                raise
            pending['phone_code_hash'] = sent.phone_code_hash
            pending['session'] = client.session.save()
            self.save_pending(pending)
        finally:
            await client.disconnect()

    async def finish(self, pending_json, code, password):
        pending = json.loads(pending_json)
        if self.now() - pending['created_at'] > 900 or not pending.get('phone_code_hash'):
            raise ValueError('Login request missing or expired')
        client = self.client_factory(pending['session'])
        try:
            await client.connect()
            # A previous run may have signed in but failed to save TG_SESSION.
            if not await client.is_user_authorized():
                if pending.get('password_required'):
                    if not password:
                        return 'password_required'
                    await client.sign_in(password=password)
                else:
                    if not code or not re.fullmatch(r'[0-9]{5,8}', code):
                        raise ValueError('A Telegram login code is required')
                    try:
                        await client.sign_in(phone=pending['phone'], code=code,
                                             phone_code_hash=pending['phone_code_hash'])
                    except SessionPasswordNeededError:
                        pending['password_required'] = True
                        self.save_pending(pending)
                        if not password:
                            return 'password_required'
                        await client.sign_in(password=password)
            me = await client.get_me()
            if not me or me.bot:
                raise ValueError('Personal account required')
            self.vault.put('TG_SESSION', client.session.save())
            cleanup_ok = True
            for name in self.TEMPORARY:
                try:
                    self.vault.delete(name)
                except Exception:
                    cleanup_ok = False
            return 'complete' if cleanup_ok else 'cleanup_required'
        finally:
            await client.disconnect()


async def main(stage):
    logging.disable(logging.CRITICAL)
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        raise ValueError('Use the manually triggered GitHub Actions workflow')
    if not os.environ.get('GH_TOKEN'):
        raise ValueError('SETUP_TOKEN is required')
    if os.environ.get('TG_SESSION'):
        print('TG_SESSION is already configured; existing credentials were preserved.')
        return
    vault = Vault(os.environ['GITHUB_REPOSITORY'])
    def client_factory(session):
        return TelegramClient(StringSession(session), int(os.environ['TG_API_ID']),
                              os.environ['TG_API_HASH'].strip(), request_retries=0,
                              connection_retries=1, flood_sleep_threshold=0,
                              raise_last_call_error=True)
    setup = Setup(vault, client_factory)
    if stage in ('request-code', 'resume-request'):
        await setup.request_code(os.environ.get('TG_PHONE', '').strip(), '',
                                 os.environ.get('TG_LOGIN_PENDING', ''), resume=stage == 'resume-request')
        print('Code requested. Add TG_CODE in GitHub Secrets, then run finish-login within 15 minutes. No groups joined or messages posted.')
    elif stage == 'finish-login':
        result = await setup.finish(os.environ.get('TG_LOGIN_PENDING', ''),
                                    os.environ.get('TG_CODE', '').strip(),
                                    os.environ.get('TG_PASSWORD', ''))
        if result == 'password_required':
            print('Telegram requires your existing 2FA password. Add TG_PASSWORD in GitHub Secrets, then run finish-login again.')
            sys.exit(1)
        print('TG_SESSION saved securely. No groups joined or messages posted.')
        if result == 'cleanup_required':
            print('Remove setup-only secrets: TG_LOGIN_PENDING, TG_PHONE, TG_CODE, TG_PASSWORD and SETUP_TOKEN.')
        print('Revoke the temporary setup token in your GitHub account settings.')
    else:
        raise ValueError('Unknown setup stage')


def safe_error(exc):
    return f'Setup stopped safely ({type(exc).__name__}). No exception payload or credentials were logged.'


if __name__ == '__main__':
    try:
        asyncio.run(main(sys.argv[1]))
    except FloodWaitError as exc:
        sys.exit(f'Telegram rate limit: wait at least {exc.seconds} seconds. No automatic retry was attempted.')
    except Exception as exc:
        sys.exit(safe_error(exc))
