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
    approve = sub.add_parser('approve', help='Attest that rules/admin explicitly permit YouTube Shorts promotion')
    approve.add_argument('--group', type=int, required=True)
    approve.add_argument('--rules-sha256', required=True)
    approve.add_argument('--evidence', required=True, help='Explicit unconditional promotion permission; conditional/time-window rules are unsupported')
    approve.add_argument('--cooldown-hours', type=int, default=48, help='Minimum interval required by the group (24 to 8760 hours)')
    approve.add_argument('--confirm-promotion-permitted', action='store_true', required=True)
    revoke = sub.add_parser('revoke')
    revoke.add_argument('--group', type=int, required=True)
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
    elif args.command == 'approve':
        group = state['groups'][str(args.group)]
        if args.rules_sha256 != group['rules_hash'] or not args.evidence.strip() or not group['rules_complete'] or not 24 <= args.cooldown_hours <= 8760:
            raise ValueError('Review current, readable rules before approving')
        approval = {'id': args.group, 'rules_hash': args.rules_sha256, 'evidence': args.evidence,
                    'approved': True, 'expires_at': time.time() + 7 * 86400, 'cooldown_hours': args.cooldown_hours}
        state['config']['approved_groups'] = [a for a in state['config']['approved_groups'] if a['id'] != args.group] + [approval]
        state['denied_groups'] = [gid for gid in state.get('denied_groups', []) if gid != args.group]
        store.save(state)
    elif args.command == 'revoke':
        state['config']['approved_groups'] = [a for a in state['config']['approved_groups'] if a['id'] != args.group]
        # Explicit denials override machine-readable permission too.
        state.setdefault('denied_groups', [])
        if args.group not in state['denied_groups']:
            state['denied_groups'].append(args.group)
        store.save(state)
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
