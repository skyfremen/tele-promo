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
    member_count: int | None = None


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
    optional_limits = {
        'min_group_members': (100, 0, 1000000),
        'joined_scan_limit': (100, 0, 500),
        'joined_inspect_limit': (20, 0, 50),
    }
    for name, (default, minimum, maximum) in optional_limits.items():
        value = limits.get(name, default)
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError('Invalid ' + name + ' limit')
    for name, ceiling in ceilings.items():
        if type(limits.get(name)) is not int or not 0 <= limits[name] <= ceiling:
            raise ValueError('Invalid operation limit')
    if type(limits.get('cooldown_hours')) is not int or not 24 <= limits['cooldown_hours'] <= 8760:
        raise ValueError('Cooldown must be at least 24 hours')
    selected = config.get('selected_groups', [])
    if (not isinstance(selected, list) or len(selected) > 100
            or any(type(gid) is not int or gid <= 0 for gid in selected)
            or len(set(selected)) != len(selected)):
        raise ValueError('selected_groups must contain up to 100 unique numeric group IDs')
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
        if state.get('blocked_until', 0) > self.now():
            return
        limits = config['limits']
        mutate = live and not config['dry_run']
        min_members = limits.get('min_group_members', 100)
        joins = posts = eligible_joined = 0
        seen = set()
        keywords = [k.lower() for k in config['keywords']]
        selected = set(config.get('selected_groups', []))

        async def examine(reference, source):
            nonlocal joins, posts, eligible_joined
            group = await self.tg.inspect(reference)
            if group.id in seen:
                return
            seen.add(group.id)
            gid = str(group.id)
            relevant = any(k in (group.title + ' ' + group.about).lower() for k in keywords)
            # Permission is obtained separately by the operator. Only explicitly
            # selected group IDs may be joined or messaged; search results alone
            # do not grant consent to receive automated promotional posts.
            allowed = (group.id in selected
                       and group.id not in state.get('denied_groups', [])
                       and relevant and group.is_group and group.posting_allowed)
            minimum_met = (group.member or
                           (type(group.member_count) is int and group.member_count >= min_members))
            previous = state['groups'].get(gid, {})
            if not relevant:
                status = 'irrelevant'
            elif group.id not in selected:
                status = 'not_selected'
            elif not group.is_group or not group.posting_allowed or group.id in state.get('denied_groups', []):
                status = 'restricted'
            elif not minimum_met:
                status = 'member_count_unknown' if group.member_count is None else 'below_member_minimum'
            else:
                status = 'eligible'
            state['groups'][gid] = {
                'username': group.username, 'title': group.title,
                'about': group.about, 'pinned': group.pinned,
                'rules_hash': rules_hash(group), 'rules_complete': group.rules_complete,
                'member': group.member, 'member_count': group.member_count,
                'source': source, 'seen_at': self.now(), 'status': status,
                'join_attempted_at': previous.get('join_attempted_at', 0),
            }
            self.store.save(state)
            if not allowed or not minimum_met:
                return
            if group.member:
                eligible_joined += 1
            if not mutate or not config['shorts']:
                return

            ledger = state['posts']
            recent = [p for p in ledger.values() if self.now() - p['at'] < 86400]
            if posts >= limits['posts_per_run'] or len(recent) >= limits['posts_per_day']:
                return
            interval = max([limits['cooldown_hours']] + [
                a.get('cooldown_hours', 48)
                for a in config['approved_groups'] if a.get('id') == group.id
            ])
            if any(p['group_id'] == group.id and self.now() - p['at'] < interval * 3600
                   for p in ledger.values()):
                return
            short = next((short for short in config['shorts']
                          if f"{group.id}:{video_id(short['url'])}" not in ledger), None)
            if short is None:
                return

            if not group.member:
                if (joins >= limits['joins_per_run'] or
                        (previous.get('join_attempted_at') and
                         self.now() - previous['join_attempted_at'] < 7 * 86400)):
                    return
                # Persist the attempt before joining to avoid repeated join requests.
                state['groups'][gid]['join_attempted_at'] = self.now()
                self.store.save(state)
                joins += 1
                await self.tg.join(group)
                group = await self.tg.inspect(group.username)
                if group.id != int(gid) or not group.member:
                    return
                state['groups'][gid]['member'] = True
                self.store.save(state)

            # Check identity, membership and posting rights immediately
            # before sending. Only operator-selected groups can receive posts.
            group = await self.tg.inspect(group.username or group.id)
            if (group.id != int(gid) or group.id not in selected
                    or group.id in state.get('denied_groups', [])
                    or not group.is_group or not group.member
                    or not group.posting_allowed):
                return
            if not await self.tg.can_post(group):
                state['groups'][gid]['status'] = 'restricted'
                self.store.save(state)
                return

            vid = video_id(short['url'])
            key = f'{group.id}:{vid}'
            entry = {'group_id': group.id, 'video_id': vid, 'at': self.now(),
                     'status': 'pending', 'random_id': secrets.randbits(63)}
            ledger[key] = entry
            self.store.save(state)
            try:
                message = (short['message'] if 'message' in short else
                           short['caption'] + '\nhttps://www.youtube.com/shorts/' + vid)
                message_id = await self.tg.send(group, message, entry['random_id'])
            except BaseException:
                entry['status'] = 'uncertain'
                self.store.save(state)
                raise
            entry.update(status='confirmed', message_id=message_id)
            self.store.save(state)
            posts += 1

        # Pass 1: scan existing memberships before any global Telegram search.
        joined_limit = limits.get('joined_scan_limit', 100)
        inspect_limit = limits.get('joined_inspect_limit', 20)
        joined = await self.tg.list_joined(joined_limit)
        matching = [g for g in joined if any(k in g.title.lower() for k in keywords)]
        others = [g for g in joined if not any(k in g.title.lower() for k in keywords)]
        # Spread coverage across runs: a long unrelated chat list must not hide
        # lower-ranked groups forever.
        matching.sort(key=lambda g: state['groups'].get(str(g.id), {}).get('seen_at', 0))
        first = matching[:inspect_limit]
        remaining = inspect_limit - len(first)
        cursor = state.get('joined_scan_cursor', 0)
        if type(cursor) is not int or cursor < 0:
            cursor = 0
        if others and remaining:
            first.extend(others[(cursor + i) % len(others)] for i in range(min(remaining, len(others))))
            state['joined_scan_cursor'] = (cursor + remaining) % len(others)
        elif not others:
            state['joined_scan_cursor'] = 0
        for candidate in first:
            await examine(candidate.username or candidate.id, 'already_joined')

        # Pass 2: search for new groups only if there is still capacity.
        # In dry-run, an eligible existing group suffices for discovery.
        if (posts >= limits['posts_per_run'] or
                (not mutate and eligible_joined > 0)):
            return
        discovered = await self.tg.discover(config['keywords'], limits['discovery_results'])
        known = [item for item in state['groups'].values()
                 if item.get('username') and item.get('status') != 'irrelevant']
        candidates = [g.username for g in discovered] + [
            item['username'] for item in known if item.get('username')
        ]
        for reference in dict.fromkeys(candidates):
            if len(seen) >= inspect_limit + limits['discovery_results']:
                break
            if posts >= limits['posts_per_run']:
                break
            await examine(reference, 'discovery')
