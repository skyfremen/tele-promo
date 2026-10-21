"""Operator-selected group targets and write-ahead posting ledger."""
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
    if not isinstance(config.get('shorts'), list):
        raise ValueError('shorts must be a list')
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

    async def run(self, live=False, mode="all"):
        # "all" is retained only for existing local tests/callers; production
        # workflows explicitly select one non-overlapping operation.
        if mode not in ("join", "send", "all"):
            raise ValueError("Unsupported operation mode")
        state, config = self.store.state, self.config
        if state.get('blocked_until', 0) > self.now():
            return
        limits = config['limits']
        mutate = live and not config['dry_run']
        min_members = limits.get('min_group_members', 100)
        joins = posts = 0
        seen = set()
        new_examined = set()
        keywords = [k.lower() for k in config['keywords']]
        selected = set(config.get('selected_groups', []))

        async def examine(reference, source):
            nonlocal joins, posts
            group = await self.tg.inspect(reference)
            if group.id in seen:
                return
            seen.add(group.id)
            if not group.member:
                new_examined.add(group.id)
            gid = str(group.id)
            relevant = any(k in (group.title + ' ' + group.about).lower() for k in keywords)
            # Joining a relevant public group does not require text-based
            # promotional approval. Only sending messages requires that this
            # exact group ID was separately selected by the operator.
            denied = group.id in state.get('denied_groups', [])
            minimum_met = (group.member or
                           (type(group.member_count) is int and group.member_count >= min_members))
            # Do not join read-only groups. Default rights can be inspected
            # before joining; member-specific rights require a second check
            # once already joined.
            can_message = bool(group.posting_allowed)
            if group.member and can_message:
                can_message = bool(await self.tg.can_post(group))
            joinable = (not denied and relevant and group.is_group
                        and not group.member and bool(group.username)
                        and minimum_met and can_message)
            postable = (not denied and relevant and group.is_group
                        and group.id in selected and can_message)
            previous = state['groups'].get(gid, {})
            if not relevant:
                status = 'irrelevant'
            elif not group.is_group or denied:
                status = 'restricted'
            elif not minimum_met:
                status = 'member_count_unknown' if group.member_count is None else 'below_member_minimum'
            elif not can_message:
                status = 'messaging_disabled'
            elif group.member:
                status = 'eligible' if postable else 'joined_unselected'
            elif postable:
                status = 'eligible'
            elif joinable:
                status = 'join_candidate'
            else:
                status = 'not_selected'
            state['groups'][gid] = {
                'username': group.username, 'title': group.title,
                'about': group.about, 'pinned': group.pinned,
                'rules_hash': rules_hash(group), 'rules_complete': group.rules_complete,
                'member': group.member, 'member_count': group.member_count,
                'messaging_allowed': can_message,
                'source': source, 'seen_at': self.now(), 'status': status,
                'join_attempted_at': previous.get('join_attempted_at', 0),
            }
            self.store.save(state)
            # Joining has an independent, tightly rate-limited path. It does
            # not require Shorts configuration or permission for promotional
            # messages, and never increments the posting ledger.
            if mode != "send" and mutate and joinable and joins < limits['joins_per_run'] and not (
                    previous.get('join_attempted_at') and
                    self.now() - previous['join_attempted_at'] < 7 * 86400):
                state['groups'][gid]['join_attempted_at'] = self.now()
                self.store.save(state)
                joins += 1
                await self.tg.join(group)
                group = await self.tg.inspect(group.username)
                if group.id != int(gid) or not group.member:
                    return
                # Telegram may impose member-specific restrictions after join.
                # Recheck actual membership rights and record any restriction.
                can_message = (bool(group.posting_allowed)
                               and bool(await self.tg.can_post(group)))
                postable = (not denied and relevant and group.is_group
                            and group.id in selected and can_message)
                state['groups'][gid]['member'] = True
                state['groups'][gid]['messaging_allowed'] = can_message
                state['groups'][gid]['status'] = (
                    'messaging_disabled' if not can_message else
                    'eligible' if postable else 'joined_unselected')
                self.store.save(state)
            if (mode == "join" or not mutate or not postable
                    or not group.member or not config['shorts']):
                return

            ledger = state['posts']
            recent = [p for p in ledger.values() if self.now() - p['at'] < 86400]
            if posts >= limits['posts_per_run'] or len(recent) >= limits['posts_per_day']:
                return
            interval = limits['cooldown_hours']
            if any(p['group_id'] == group.id and self.now() - p['at'] < interval * 3600
                   for p in ledger.values()):
                return
            short = next((short for short in config['shorts']
                          if f"{group.id}:{video_id(short['url'])}" not in ledger), None)
            if short is None:
                return

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
        joined_ids = {g.id for g in joined}
        # The persisted membership state also excludes groups outside the
        # current dialog scan. Search results are checked again for membership.
        joined_ids.update(int(gid) for gid, info in state['groups'].items()
                          if info.get('member') is True)
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
            if mode != "send" or candidate.id in selected:
                await examine(candidate.username or candidate.id, 'already_joined')

        if mode == "send":
            # Reinspect known, expressly selected targets outside the dialog
            # scan. Telegram's current membership and send permissions are
            # checked for every target. Never search or join in send mode.
            for gid in sorted(selected):
                if gid in seen or gid in state.get('denied_groups', []):
                    continue
                saved = state['groups'].get(str(gid), {})
                if saved.get('username') and saved.get('member') is True:
                    await examine(saved['username'], 'send_selected')
            return

        # Pass 2: inspect up to the configured number of distinct, unjoined
        # public groups. Joined groups and duplicate search hits do not consume
        # the new-group discovery target.
        target = limits['discovery_results']
        discovered = await self.tg.discover(config['keywords'], target,
                                            exclude_ids=joined_ids)
        candidates = [g.username for g in discovered
                      if g.id not in joined_ids and not g.member]
        # Revisit known unjoined groups if Telegram returns fewer fresh results.
        # Each group still counts at most once and must pass current checks.
        candidates += [
            item['username'] for gid, item in state['groups'].items()
            if item.get('username') and item.get('member') is not True
            and int(gid) not in joined_ids and item.get('status') != 'irrelevant'
        ]
        for reference in dict.fromkeys(candidates):
            if len(new_examined) >= target:
                break
            await examine(reference, 'discovery')
