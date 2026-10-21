"""Telegram discovery, membership inspection, and gated messaging."""
from telethon import TelegramClient, functions, types
from telethon.sessions import StringSession
from telethon.errors import UserNotParticipantError, InviteRequestSentError
from .core import Group


class Telegram:
    def __init__(self, session, api_id, api_hash):
        self.client = TelegramClient(StringSession(session), int(api_id), api_hash,
                                    flood_sleep_threshold=0, request_retries=0,
                                    connection_retries=1, raise_last_call_error=True)
        self._joined_entities = {}

    async def connect(self):
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise ValueError('Telegram session is not authorized')
        me = await self.client.get_me()
        if not me or me.bot:
            raise ValueError('A personal Telegram account is required')

    async def list_joined(self, limit):
        """Enumerate existing group dialogs (not channels or private chats)."""
        groups = []
        if limit == 0:
            return groups
        # Limit total dialog reads; not all chats will be visible in very large accounts.
        async for dialog in self.client.iter_dialogs(limit=500):
            entity = dialog.entity
            if (isinstance(entity, types.Channel) and entity.megagroup
                    and not entity.left and not entity.scam and not entity.fake
                    and not entity.restricted):
                pass
            elif isinstance(entity, types.Chat) and not entity.left and not entity.deactivated:
                pass
            else:
                continue
            self._joined_entities[entity.id] = entity
            groups.append(Group(
                entity.id, getattr(entity, 'username', '') or '',
                entity.title, '', '', True, True,
                member_count=getattr(entity, 'participants_count', None)))
            if len(groups) >= limit:
                break
        return groups

    async def discover(self, keywords, limit, exclude_ids=None):
        """Find up to limit DISTINCT public groups not already joined.

        One Telegram search may return repeated/already-joined groups. Check
        multiple keywords with a 20-result request each; do not count those
        groups toward the configured discovery target.
        """
        seen_ids = set(exclude_ids or ())
        result = []
        for keyword in keywords:
            if len(result) >= limit:
                break
            found = await self.client(functions.contacts.SearchRequest(q=keyword, limit=20))
            for entity in found.chats:
                if (isinstance(entity, types.Channel) and entity.megagroup
                        and entity.username and not entity.scam and
                        not entity.fake and not entity.restricted
                        and entity.id not in seen_ids):
                    seen_ids.add(entity.id)
                    # Telegram's Channel.left indicates we already belong to
                    # this group. Exclude it even if outside our dialog scan.
                    if not entity.left:
                        continue
                    result.append(Group(entity.id, entity.username, entity.title,
                                        '', '', True, False,
                                        member_count=getattr(entity, 'participants_count', None)))
                    if len(result) >= limit:
                        return result
        return result

    async def _resolve(self, reference):
        cached = getattr(self, '_joined_entities', {}).get(reference) if isinstance(reference, int) else None
        return cached if cached is not None else await self.client.get_entity(reference)

    async def inspect(self, reference):
        entity = await self._resolve(reference)
        if isinstance(entity, types.Channel):
            if not entity.megagroup or entity.scam or entity.fake or entity.restricted:
                return Group(entity.id, getattr(entity, 'username', '') or '',
                             entity.title, '', '', False, False)
            full = await self.client(functions.channels.GetFullChannelRequest(entity))
            current = next((c for c in full.chats if c.id == entity.id), entity)
            about = full.full_chat.about or ''
            count = getattr(full.full_chat, 'participants_count', None)
            if type(count) is not int:
                count = getattr(current, 'participants_count', None)
            member = not current.left
        elif isinstance(entity, types.Chat):
            if entity.left or entity.deactivated:
                return Group(entity.id, '', entity.title, '', '', False, False)
            full = await self.client(functions.messages.GetFullChatRequest(entity.id))
            current = next((c for c in full.chats if c.id == entity.id), entity)
            about = getattr(full.full_chat, 'about', '') or ''
            participant_list = getattr(getattr(full.full_chat, 'participants', None), 'participants', None)
            count = len(participant_list) if participant_list is not None else None
            member = not current.left
        else:
            return Group(getattr(entity, 'id', 0), '', getattr(entity, 'title', ''),
                         '', '', False, False)

        messages = await self.client.get_messages(entity, limit=21,
                                                  filter=types.InputMessagesFilterPinned())
        complete = len(messages) <= 20
        texts = []
        for message in messages:
            value = getattr(message, 'message', '')
            if not value:
                complete = False
            texts.append(value)
        if getattr(full.full_chat, 'pinned_msg_id', None) and not messages:
            complete = False
        rights = getattr(current, 'default_banned_rights', None)
        posting_allowed = not any(getattr(rights, field, False)
                                  for field in ('view_messages', 'send_messages',
                                                'send_plain', 'embed_links')) if rights else True
        return Group(entity.id, getattr(current, 'username', '') or '',
                     current.title, about, '\n'.join(texts), True,
                     member, complete, bool(posting_allowed),
                     count if type(count) is int else None)

    async def join(self, group):
        if not group.username or group.member:
            return
        try:
            await self.client(functions.channels.JoinChannelRequest(group.username))
        except InviteRequestSentError:
            return

    async def can_post(self, group):
        entity = await self._resolve(group.username or group.id)
        if entity.id != group.id:
            return False
        if isinstance(entity, types.Channel):
            if entity.left or not entity.megagroup:
                return False
            try:
                result = await self.client(functions.channels.GetParticipantRequest(entity, 'me'))
            except UserNotParticipantError:
                return False
            participant = result.participant
            if isinstance(participant, (types.ChannelParticipantBanned, types.ChannelParticipantLeft)):
                return False
            rights = [getattr(entity, 'default_banned_rights', None),
                      getattr(participant, 'banned_rights', None)]
        elif isinstance(entity, types.Chat):
            if entity.left or entity.deactivated:
                return False
            rights = [getattr(entity, 'default_banned_rights', None)]
        else:
            return False
        return not any(getattr(r, field, False)
                       for r in rights if r for field in
                       ('view_messages', 'send_messages', 'send_plain', 'embed_links'))

    async def send(self, group, text, random_id):
        peer = await self.client.get_input_entity(group.username or group.id)
        if (getattr(peer, 'channel_id', getattr(peer, 'chat_id', None)) != group.id):
            raise ValueError('Group identity changed')
        result = await self.client(functions.messages.SendMessageRequest(
            peer=peer, message=text, random_id=random_id, no_webpage=True))
        if isinstance(result, types.UpdateShortSentMessage):
            return result.id
        for update in getattr(result, 'updates', []):
            if isinstance(update, types.UpdateMessageID) and update.random_id == random_id:
                return update.id
        return None
