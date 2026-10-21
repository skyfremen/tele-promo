"""Permission gates and write-ahead posting ledger; no Telegram-specific policy."""
from dataclasses import dataclass
import hashlib
import json
import secrets
import time
import re
from urllib.parse import urlparse, parse_qs


@dataclass
class Group:
    id: int
    username: str
    title: str
    about: str
    pinned: str
    is_group: bool
    member: bool
    rules_complete: bool = True
    posting_allowed: bool = True


def rules_hash(group):
    return hashlib.sha256(json.dumps([group.id, group.about, group.pinned], ensure_ascii=False).encode()).hexdigest()


def video_id(url):
    p = urlparse(url)
    if p.scheme != 'https' or p.username or p.password or p.port:
        raise ValueError('Use an HTTPS YouTube URL')
    if p.hostname == 'youtu.be':
        value = p.path.strip('/')
    elif p.hostname in ('youtube.com', 'www.youtube.com', 'm.youtube.com'):
        if p.path.startswith('/shorts/'):
            value = p.path[len('/shorts/'):].strip('/')
        elif p.path == '/watch':
            value = parse_qs(p.query).get('v', [''])[0]
        else:
            value = ''
    else:
        value = ''
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', value):
        raise ValueError('Invalid YouTube video ID')
    return value


def permitted(group, approvals, now):
    if not group.is_group or not group.rules_complete or not group.posting_allowed:
        return False
    # Automatic permission is deliberately limited to an entire, unqualified
    # rules source. Additional text, linked rules, or conditions require review.
    sources = [s.strip() for s in (group.about, group.pinned) if s.strip()]
    explicit = {
        'youtube shorts promotion is allowed.',
        'youtube shorts promotion is welcome.',
        'sub4sub is allowed.',
        'sub4sub is welcome.',
        'sub4sub links are allowed.',
        'sub4sub links are welcome.',
    }
    if sources and all(s.lower() in explicit for s in sources):
        return True
    return any(
        a.get('id') == group.id and a.get('approved') is True
        and a.get('rules_hash') == rules_hash(group)
        and isinstance(a.get('expires_at'), (int, float)) and a['expires_at'] > now
        and bool(a.get('evidence', '').strip())
        for a in approvals
    )


def validate_config(config):
    if not isinstance(config, dict) or type(config.get('dry_run')) is not bool:
        raise ValueError('dry_run must be boolean')
    keywords = config.get('keywords')
    if not isinstance(keywords, list) or not 1 <= len(keywords) <= 5 or any(not isinstance(k, str) or len(k.strip()) < 3 for k in keywords):
        raise ValueError('Configure one to five keywords of at least three characters')
    ceilings = {'joins_per_run': 2, 'posts_per_run': 3, 'posts_per_day': 5, 'discovery_results': 20}
    limits = config.get('limits', {})
    for name, ceiling in ceilings.items():
        if type(limits.get(name)) is not int or not 0 <= limits[name] <= ceiling:
            raise ValueError('Invalid operation limit')
    if type(limits.get('cooldown_hours')) is not int or not 24 <= limits['cooldown_hours'] <= 8760:
        raise ValueError('Cooldown must be at least 24 hours')
    if not isinstance(config.get('shorts'), list) or not isinstance(config.get('approved_groups'), list):
        raise ValueError('shorts and approved_groups must be lists')
    for approval in config['approved_groups']:
        interval = approval.get('cooldown_hours', 48)
        if type(interval) is not int or not 24 <= interval <= 8760:
            raise ValueError('Invalid approved group cooldown')
    for short in config['shorts']:
        video_id(short['url'])
        if 'message' in short:
            message = short['message']
            if not isinstance(message, str) or not 1 <= len(message) <= 1024:
                raise ValueError('Message must contain 1 to 1024 characters')
            # Preserve exact wording and line breaks, while verifying that the
            # single embedded YouTube URL identifies the configured video.
            links = re.findall(r'https?://[^\s<>]+', message, re.I)
            if len(links) != 1 or video_id(links[0]) != video_id(short['url']):
                raise ValueError('Message must include only the configured YouTube URL')
        else:
            if not isinstance(short.get('caption'), str) or not 1 <= len(short['caption']) <= 500:
                raise ValueError('Caption must contain 1 to 500 characters')
            if re.search(r'https?://|t\.me/', short['caption'], re.I):
                raise ValueError('Put the sole promotional URL in the url field')
    return config


class Engine:
    def __init__(self, telegram, store, config, now=time.time):
        self.tg, self.store, self.config, self.now = telegram, store, validate_config(config), now

    async def run(self, live=False):
        state, config = self.store.state, self.config
        now = self.now()
        if state.get('blocked_until', 0) > now:
            return
        limits = config['limits']
        mutate = live and not config['dry_run']
        # Refresh old groups as well as newly discovered ones, bounded per run.
        found = await self.tg.discover(config['keywords'], limits['discovery_results'])
        usernames = list(dict.fromkeys([g.username for g in found] + [g['username'] for g in state['groups'].values()]))
        joins = posts = 0
        for username in usernames[:max(1, limits['discovery_results'])]:
            group = await self.tg.inspect(username)
            gid = str(group.id)
            relevant = any(k.lower() in (group.title + ' ' + group.about).lower() for k in config['keywords'])
            allowed = group.id not in state.get('denied_groups', []) and relevant and permitted(group, config['approved_groups'], self.now())
            previous = state['groups'].get(gid, {})
            state['groups'][gid] = {
                'username': group.username, 'title': group.title,
                'about': group.about, 'pinned': group.pinned,
                'rules_hash': rules_hash(group), 'rules_complete': group.rules_complete,
                'member': group.member, 'seen_at': self.now(),
                'status': 'eligible' if allowed else 'needs_approval',
                'join_attempted_at': previous.get('join_attempted_at', 0),
            }
            self.store.save(state)
            if not allowed or not mutate:
                continue
            if not group.member:
                if joins >= limits['joins_per_run'] or (previous.get('join_attempted_at') and self.now() - previous['join_attempted_at'] < 7 * 86400):
                    continue
                # Journal membership attempts too, so join requests aren't repeated.
                state['groups'][gid]['join_attempted_at'] = self.now()
                self.store.save(state)
                joins += 1
                await self.tg.join(group)
                group = await self.tg.inspect(username)
                if group.id != int(gid) or not group.member:
                    continue
                state['groups'][gid]['member'] = True
                self.store.save(state)
            # Re-read authoritative rules after joining and immediately before send.
            group = await self.tg.inspect(username)
            if group.id != int(gid) or not permitted(group, config['approved_groups'], self.now()):
                continue
            if not await self.tg.can_post(group):
                state['groups'][gid]['status'] = 'restricted'
                self.store.save(state)
                continue
            ledger = state['posts']
            recent = [p for p in ledger.values() if self.now() - p['at'] < 86400]
            if posts >= limits['posts_per_run'] or len(recent) >= limits['posts_per_day']:
                continue
            interval = max([limits['cooldown_hours']] + [a.get('cooldown_hours', 48) for a in config['approved_groups'] if a.get('id') == group.id])
            if any(p['group_id'] == group.id and self.now() - p['at'] < interval * 3600 for p in ledger.values()):
                continue
            for short in config['shorts']:
                vid = video_id(short['url'])
                key = f'{group.id}:{vid}'
                if key in ledger:  # pending/uncertain deliveries also block repeats forever
                    continue
                entry = {'group_id': group.id, 'video_id': vid, 'at': self.now(), 'status': 'pending', 'random_id': secrets.randbits(63)}
                ledger[key] = entry
                self.store.save(state)  # abort before sending if durable persistence fails
                try:
                    message = short['message'] if 'message' in short else short['caption'] + '\nhttps://www.youtube.com/shorts/' + vid
                    message_id = await self.tg.send(group, message, entry['random_id'])
                except BaseException:
                    entry['status'] = 'uncertain'
                    self.store.save(state)
                    raise
                entry.update(status='confirmed', message_id=message_id)
                self.store.save(state)
                posts += 1
                break
