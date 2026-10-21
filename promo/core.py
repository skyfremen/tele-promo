"""Separate discovery and joining from manual posting."""
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


def public_group_username(url):
    """Accept only public Telegram group links; never invite or message links."""
    if not isinstance(url, str):
        raise ValueError('Candidate URL must be a string')
    parsed = urlparse(url)
    if (parsed.scheme != 'https' or parsed.hostname not in ('t.me', 'telegram.me')
            or parsed.username or parsed.password or parsed.port
            or parsed.query or parsed.fragment):
        raise ValueError('Not a public Telegram group link')
    name = parsed.path.strip('/')
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}', name):
        raise ValueError('Not a public Telegram group username')
    if name.lower() in ('joinchat', 'share', 'proxy', 'addstickers'):
        raise ValueError('Unsupported Telegram link')
    return name


def has_promotion_permission(group):
    """Require affirmative promotion rules, not merely the ability to send."""
    if not group.rules_complete:
        return False
    rules = (group.about + '\\n' + group.pinned).lower()
    subject = r'(?:promot(?:ion|ions|e)?|advertis(?:e|ing|ements?)?|youtube|shorts|videos?|channels?|links?|sub4sub)'
    deny = r'\b(?:no|not|never|banned?|forbidden|prohibited?)\b.{0,45}\b' + subject
    if re.search(deny, rules):
        return False
    action = r'(?:allow(?:ed)?|welcom(?:e|ed)|permit(?:ted)?|share|post(?:ing)?|drop|promote)'
    return bool(re.search(r'\b' + action + r'\b.{0,70}\b' + subject + r'\b', rules)
                or re.search(r'\b' + subject + r'\b.{0,70}\b' + action + r'\b', rules))


def validate_config(config):
    if not isinstance(config, dict) or type(config.get('dry_run')) is not bool:
        raise ValueError('dry_run must be boolean')
    keywords = config.get('keywords')
    if not isinstance(keywords, list) or not 1 <= len(keywords) <= 5 or any(not isinstance(k, str) or len(k.strip()) < 3 for k in keywords):
        raise ValueError('Configure one to five keywords of at least three characters')
    ceilings = {'joins_per_run': 2, 'posts_per_run': 5, 'posts_per_day': 20, 'discovery_results': 20}
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
    if type(limits.get('cooldown_hours')) is not int or not 12 <= limits['cooldown_hours'] <= 8760:
        raise ValueError('Cooldown must be at least 12 hours')
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
        candidate_usernames = set()
        keywords = [k.lower() for k in config['keywords']]

        async def examine(reference, source, candidate=False):
            nonlocal joins, posts
            group = reference if isinstance(reference, Group) else await self.tg.inspect(reference)
            if group.id in seen:
                return
            seen.add(group.id)
            if not group.member:
                new_examined.add(group.id)
            gid = str(group.id)
            # Accept common Sub4Sub spellings without matching unrelated 'sub' words.
            haystack = (group.title + ' ' + group.about).lower()
            normalized = re.sub(r'[^a-z0-9]+', '', haystack)
            relevant = any(k in haystack or (re.sub(r'[^a-z0-9]+', '', k) in normalized
                           and len(re.sub(r'[^a-z0-9]+', '', k)) >= 5)
                           for k in keywords)
            relevant = relevant or bool(re.search(r'\bsub\s*[24]\s*sub\b', haystack, re.I))
            # The independent manual sending workflow verifies membership and
            # Telegram's current messaging rights before publishing.
            denied = group.id in state.get('denied_groups', [])
            minimum_met = (group.member or
                           (type(group.member_count) is int and group.member_count >= min_members))
            # Do not join read-only groups. Default rights can be inspected
            # before joining; member-specific rights require a second check
            # once already joined.
            can_message = bool(group.posting_allowed)
            if group.member and can_message:
                can_message = bool(await self.tg.can_post(group))
            permission_ok = not candidate or has_promotion_permission(group)
            joinable = (not denied and relevant and group.is_group
                        and not group.member and bool(group.username)
                        and minimum_met and can_message and permission_ok)
            postable = (not denied and relevant and group.is_group
                        and group.member and can_message)
            previous = state['groups'].get(gid, {})
            if not relevant:
                status = 'irrelevant'
            elif not group.is_group or denied:
                status = 'restricted'
            elif not minimum_met:
                status = 'member_count_unknown' if group.member_count is None else 'below_member_minimum'
            elif not can_message:
                status = 'messaging_disabled'
            elif candidate and not group.member and not permission_ok:
                status = 'promotion_unverified'
            elif group.member:
                status = 'eligible' if postable else 'joined_unselected'
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
                            and group.member and can_message)
                state['groups'][gid]['member'] = True
                postable = (not denied and relevant and group.is_group and can_message)
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
            # Prefer an unused video, then allow confirmed videos to repeat.
            # Unresolved sends retain their duplicate protection indefinitely.
            short = None
            for candidate in config['shorts']:
                vid = video_id(candidate['url'])
                history = [p for key, p in ledger.items()
                           if p['group_id'] == group.id and
                           (p.get('video_id') == vid or key == f'{group.id}:{vid}')]
                if any(p['status'] != 'confirmed' for p in history):
                    continue
                if short is None or not history:
                    short = candidate
                if not history:
                    break
            if short is None:
                return

            # Validate identity, membership and posting rights before sending.
            group = await self.tg.inspect(group.username or group.id)
            if (group.id != int(gid)
                    or group.id in state.get('denied_groups', [])
                    or not group.is_group or not group.member
                    or not group.posting_allowed):
                return
            if not await self.tg.can_post(group):
                state['groups'][gid]['status'] = 'restricted'
                self.store.save(state)
                return

            vid = video_id(short['url'])
            entry = {'group_id': group.id, 'video_id': vid, 'at': self.now(),
                     'status': 'pending', 'random_id': secrets.randbits(63)}
            # Keep every attempt so reposts cannot erase rolling-limit history.
            key = f"{group.id}:{vid}:{entry['random_id']}"
            while key in ledger:
                entry['random_id'] = secrets.randbits(63)
                key = f"{group.id}:{vid}:{entry['random_id']}"
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
            await examine(candidate.username or candidate.id, 'already_joined')

        if mode == "send":
            # Inspect saved memberships beyond the current dialog window too.
            # No discovery or joining can occur in this mode.
            for gid, saved in state['groups'].items():
                if int(gid) in seen or int(gid) in state.get('denied_groups', []):
                    continue
                if saved.get('username') and saved.get('member') is True:
                    await examine(saved['username'], 'send_joined')
            return

        # Candidate links supplied by ChatGPT are reviewed before Telegram search.
        # The candidate file is independent of state.json and is never truncated.
        if mode != 'send' and hasattr(self.store, 'load_candidates'):
            candidate_doc = self.store.load_candidates()
            changed = False
            checked = 0
            usernames = set()
            for entry in candidate_doc['candidates']:
                if checked >= 20:
                    break  # Bound API lookups without starving normal discovery.
                url = entry['url']
                try:
                    username = public_group_username(url)
                except ValueError:
                    if entry.get('status') != 'invalid_url':
                        entry['status'] = 'invalid_url'
                        changed = True
                    continue
                key = username.casefold()
                if key in usernames:
                    if entry.get('status') != 'duplicate':
                        entry['status'] = 'duplicate'
                        changed = True
                    continue
                usernames.add(key)
                candidate_usernames.add(key)
                # Recheck rejections weekly; never rejoin known members.
                if entry.get('status') in ('joined', 'duplicate', 'invalid_url'):
                    continue
                last = entry.get('last_checked_at', 0)
                if (type(last) in (float, int) and last > self.now() - 7 * 86400
                        and entry.get('status') not in (None, 'pending', 'lookup_failed')):
                    continue
                already = next((int(gid) for gid, saved in state['groups'].items()
                                if saved.get('username', '').casefold() == key
                                and saved.get('member') is True), None)
                if already is not None:
                    entry.update(status='joined', group_id=already,
                                 last_checked_at=self.now())
                    changed = True
                    continue
                checked += 1
                try:
                    group = await self.tg.inspect(username)
                except ValueError:
                    entry.update(status='lookup_failed', last_checked_at=self.now())
                    changed = True
                    continue
                await examine(group, 'candidate_file', candidate=True)
                saved = state['groups'].get(str(group.id))
                if saved:
                    entry.update(group_id=group.id, last_checked_at=self.now(),
                                 status='joined' if saved['member'] else saved['status'])
                    changed = True
            if changed:
                self.store.save_candidates(candidate_doc)

        # Pass 2: inspect up to the configured number of distinct, unjoined
        # public groups. Joined groups and duplicate search hits do not consume
        # the new-group discovery target.
        target = limits['discovery_results']
        discovered = await self.tg.discover(config['keywords'], target,
                                            exclude_ids=joined_ids)
        candidates = [g.username for g in discovered
                      if g.id not in joined_ids and not g.member
                      and g.username.casefold() not in candidate_usernames]
        # Revisit known unjoined groups if Telegram returns fewer fresh results.
        # Each group still counts at most once and must pass current checks.
        candidates += [
            item['username'] for gid, item in state['groups'].items()
            if item.get('username') and item.get('member') is not True
            and item['username'].casefold() not in candidate_usernames
            and int(gid) not in joined_ids and item.get('status') != 'irrelevant'
        ]
        for reference in dict.fromkeys(candidates):
            if len(new_examined) >= target or joins >= limits['joins_per_run']:
                break
            await examine(reference, 'discovery')

        # If the first batch yielded no joins, inspect a bounded second batch.
        # Avoid endless retries, repeated Telegram searches and flood limits.
        if mode == 'join' and mutate and joins == 0 and target > 0:
            extra = await self.tg.discover(
                config['keywords'], min(target * 3, 60),
                exclude_ids=joined_ids | seen,
            )
            for candidate in extra:
                if joins or len(new_examined) >= min(target * 4, 80):
                    break
                if (candidate.id in seen or candidate.id in joined_ids
                        or candidate.username.casefold() in candidate_usernames):
                    continue
                await examine(candidate.username or candidate.id, 'discovery_extended')
