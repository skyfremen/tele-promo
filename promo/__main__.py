import argparse
import asyncio
import json
import logging
import os
import sys
import time
import yaml
from telethon.errors import FloodWaitError, SlowModeWaitError
from .core import Engine, validate_config
from .store import GitHubStore
from .telegram import Telegram


def local_only():
    if os.environ.get('GITHUB_ACTIONS'):
        raise ValueError('Management commands must run locally')


def main():
    logging.disable(logging.CRITICAL)  # third-party exception text can contain PII
    parser = argparse.ArgumentParser(description='Private-state Telegram promotion runner')
    sub = parser.add_subparsers(dest='command', required=True)
    run = sub.add_parser('run')
    run.add_argument('--live', action='store_true', help='Also requires private config dry_run: false')
    run.add_argument('--mode', choices=('join', 'send'), default='join',
                     help='Explicit join or send operation (default: join only)')
    init = sub.add_parser('init', help='Initialize state once; existing state is never overwritten')
    init.add_argument('--config', default='config.yaml')
    cfg = sub.add_parser('configure', help='Replace private settings without resetting history')
    cfg.add_argument('--config', required=True)
    sub.add_parser('review', help='Print private group/rules report locally')
    args = parser.parse_args()
    if args.command != 'run':
        local_only()
    store = GitHubStore()
    if args.command == 'init':
        if store.state is not None:
            raise ValueError('Private state already exists')
        with open(args.config) as stream:
            config = validate_config(yaml.safe_load(stream))
        if not config['dry_run']:
            raise ValueError('Initialize with dry_run: true')
        store.save({'version': 1, 'config': config, 'groups': {}, 'posts': {}, 'blocked_until': 0})
        print('Private state initialized in dry-run mode.')
        return
    if store.state is None:
        raise ValueError('Initialize private state locally first')
    state = store.state
    if args.command == 'configure':
        with open(args.config) as stream:
            state['config'] = validate_config(yaml.safe_load(stream))
        store.save(state)
    elif args.command == 'review':
        print(json.dumps(state['groups'], indent=2, ensure_ascii=False))
    else:
        config = validate_config(state['config'])
        async def execute():
            tg = Telegram(os.environ['TG_SESSION'], os.environ['TG_API_ID'], os.environ['TG_API_HASH'])
            try:
                await tg.connect()
                await Engine(tg, store, config).run(live=args.live, mode=args.mode)
            except (FloodWaitError, SlowModeWaitError) as exc:
                state['blocked_until'] = time.time() + exc.seconds + 60
                store.save(state)
                print('Telegram cooldown saved; no retry was attempted.')
            finally:
                await tg.client.disconnect()
        asyncio.run(execute())
        print('Run finished. Review details only in the private state repository.')


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception:
        # Never echo exception text, URLs, request payloads, IDs, tokens or traceback.
        print('Operation stopped safely. Check credentials, private state, and configuration locally.', file=sys.stderr)
        sys.exit(1)
