"""Read-only discovery plus gated Telegram mutations. No automatic RPC retries."""
from telethon import TelegramClient, functions, types
from telethon.sessions import StringSession
from telethon.errors import UserNotParticipantError, InviteRequestSentError
from .core import Group


class Telegram:
    def __init__(self, session, api_id, api_hash):
        self.client = TelegramClient(StringSession(session), int(api_id), api_hash,
                                    flood_sleep_threshold=0, request_retries=0,
                                    connection_retries=1, raise_last_call_error=True)

    async def connect(self):
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise ValueError('Telegram session is not authorized')
        me = await self.client.get_me()
        if not me or me.bot:
            raise ValueError('A personal Telegram account is required')

    async def discover(self, keywords, limit):
        result = []
        for keyword in keywords:
            if len(result) >= limit:
                break
            found = await self.client(functions.contacts.SearchRequest(q=keyword, limit=min(limit, 20)))
            for entity in found.chats:
                if isinstance(entity, types.Channel) and entity.megagroup and entity.username and not entity.scam and not entity.fake and not entity.restricted:
                    result.append(Group(entity.id, entity.username, entity.title, '', '', True, not entity.left))
        return result[:limit]

    async def inspect(self, username):
        entity = await self.client.get_entity(username)
        if not isinstance(entity, types.Channel) or not entity.megagroup or not entity.username or entity.scam or entity.fake:
            return Group(entity.id, username, getattr(entity, 'title', ''), '', '', False, False)
        full = await self.client(functions.channels.GetFullChannelRequest(entity))
        pinned = ''
        complete = True
        # Inspect all available pinned messages, not only the latest one.
        messages = await self.client.get_messages(entity, limit=21, filter=types.InputMessagesFilterPinned())
        if len(messages) > 20:
            complete = False
        texts = []
        for message in messages:
            text = getattr(message, 'message', '')
            if not text:
                complete = False  # image/media-only rules require manual external review
            texts.append(text)
        pinned = '\n'.join(texts)
        if getattr(full.full_chat, 'pinned_msg_id', None) and not messages:
            complete = False
        current = next((c for c in full.chats if c.id == entity.id), entity)
        rights = getattr(current, 'default_banned_rights', None)
        posting_allowed = not current.restricted and not (rights and any(
            getattr(rights, field, False) for field in ('view_messages', 'send_messages', 'send_plain', 'embed_links')))
        return Group(entity.id, current.username or username, current.title,
                     full.full_chat.about or '', pinned, True, not current.left, complete, bool(posting_allowed))

    async def join(self, group):
        try:
            await self.client(functions.channels.JoinChannelRequest(group.username))
        except InviteRequestSentError:
            return  # approval requested; do not post until actually a member

    async def can_post(self, group):
        entity = await self.client.get_entity(group.username)
        if not isinstance(entity, types.Channel) or entity.id != group.id or entity.left:
            return False
        try:
            result = await self.client(functions.channels.GetParticipantRequest(entity, 'me'))
        except UserNotParticipantError:
            return False
        participant = result.participant
        if isinstance(participant, (types.ChannelParticipantBanned, types.ChannelParticipantLeft)):
            return False
        restrictions = [getattr(entity, 'default_banned_rights', None), getattr(participant, 'banned_rights', None)]
        for rights in restrictions:
            if rights and any(getattr(rights, field, False) for field in ('view_messages', 'send_messages', 'send_plain', 'embed_links')):
                return False
        return True

    async def send(self, group, text, random_id):
        peer = await self.client.get_input_entity(group.username)
        if getattr(peer, 'channel_id', None) != group.id:
            raise ValueError('Group identity changed')
        result = await self.client(functions.messages.SendMessageRequest(
            peer=peer, message=text, random_id=random_id, no_webpage=True))
        if isinstance(result, types.UpdateShortSentMessage):
            return result.id
        for update in getattr(result, 'updates', []):
            if isinstance(update, types.UpdateMessageID) and update.random_id == random_id:
                return update.id
        # Success may omit an ID; still record the delivery and never repeat it.
        return None
