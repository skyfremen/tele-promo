"""Local, interactive one-time authentication. Never run this in Actions."""
import asyncio
import getpass
import logging
import os
from pathlib import Path
from telethon import TelegramClient
from telethon.sessions import StringSession


async def main():
    if os.environ.get('CI') or os.environ.get('GITHUB_ACTIONS'):
        raise ValueError('Run locally only')
    logging.disable(logging.CRITICAL)
    api_id = os.environ.get('TG_API_ID') or input('Telegram API ID: ').strip()
    api_hash = os.environ.get('TG_API_HASH') or getpass.getpass('Telegram API hash: ')
    client = TelegramClient(StringSession(), int(api_id), api_hash)
    try:
        await client.start(phone=lambda: input('Phone number (local only): '),
                           code_callback=lambda: getpass.getpass('Telegram login code: '),
                           password=lambda: getpass.getpass('Telegram 2FA password: '))
        path = Path('.tg-session')
        # Refuse to overwrite or follow symlinks. Session is never printed.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(client.session.save())
        print('Saved .tg-session. Put its contents in TG_SESSION, then delete the file.')
    finally:
        await client.disconnect()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception:
        raise SystemExit('Authentication failed; retry locally. No credentials were printed.')
