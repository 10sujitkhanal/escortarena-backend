"""Live messaging over WebSockets (Django Channels).

Each logged-in browser tab joins the group "user-<id>". Events sent to the browser:

- message   a new message (+ the conversation row and the new unread total)
- read      the other person read the conversation (for "Seen")
- message_updated  a message was edited, unsent or reacted to
- message_hidden   I removed a message just for me (other tabs)
- unread    my unread total changed (e.g. I read a thread in another tab)
- typing    the other person is typing in a conversation
- presence  someone I talk with came online / went offline

The browser may send: ping, typing {conversation_id}.
Realtime is best-effort: the REST API stays the source of truth, and the
browser re-fetches after reconnecting.
"""

import json

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from channels.layers import get_channel_layer
from django.core import signing
from django.core.cache import cache
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import Q
from django.utils import timezone

ONLINE_TTL = 90  # seconds; the browser pings every 25 s


def group(user_id: int) -> str:
    return f"user-{user_id}"


# ---------------------------------------------------------------------------
# Presence (connection count per user in the cache)
# ---------------------------------------------------------------------------


def _key(uid):
    return f"online:{uid}"


def is_online(uid) -> bool:
    return (cache.get(_key(uid)) or 0) > 0


def last_seen(uid):
    return cache.get(f"seen:{uid}")


def _change_connections(uid, delta) -> tuple[int, int]:
    before = cache.get(_key(uid)) or 0
    after = max(0, before + delta)
    cache.set(_key(uid), after, ONLINE_TTL if after else 1)
    cache.set(f"seen:{uid}", timezone.now().isoformat(), 60 * 60 * 24 * 30)
    return before, after


def partner_ids(uid) -> list[int]:
    from .models import Conversation
    rows = Conversation.objects.filter(Q(visitor_id=uid) | Q(advertiser_id=uid)).values_list("visitor_id", "advertiser_id")
    return sorted({v if a == uid else a for v, a in rows})


def can_type_in(uid, conversation_id) -> int | None:
    """Return the other participant's id if `uid` may send typing events in this conversation."""
    from .models import Conversation
    conv = Conversation.objects.filter(Q(visitor_id=uid) | Q(advertiser_id=uid), pk=conversation_id, blocked_by__isnull=True).first()
    if not conv:
        return None
    return conv.advertiser_id if conv.visitor_id == uid else conv.visitor_id


# ---------------------------------------------------------------------------
# Tickets: the live site runs on Vercel, which can't pass WebSockets through. The browser fetches a
# short-lived ticket through /api (session cookie), then connects straight to the API host with it.
# ---------------------------------------------------------------------------

TICKET_SALT = "realtime.ticket"
TICKET_MAX_AGE = 30 * 60  # seconds; the browser reuses one ticket for reconnects within this time


def make_ticket(user_id: int) -> str:
    return signing.dumps(user_id, salt=TICKET_SALT)


def read_ticket(ticket: str) -> int | None:
    try:
        return int(signing.loads(ticket, salt=TICKET_SALT, max_age=TICKET_MAX_AGE))
    except (signing.BadSignature, TypeError, ValueError):
        return None


@database_sync_to_async
def _active_user(uid):
    from .models import User

    return User.objects.filter(pk=uid, is_active=True).first()


def _counts(user) -> dict:
    from .messaging import unread_total
    from .notifications import unread_count

    return {"unread_total": unread_total(user), "unread_notifications": unread_count(user)}


class TicketAuthMiddleware:
    """?ticket=… on the socket URL logs the socket in (used when the session cookie isn't sent)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        from urllib.parse import parse_qs

        ticket = (parse_qs(scope.get("query_string", b"").decode()).get("ticket") or [""])[0]
        uid = read_ticket(ticket) if ticket else None
        if uid:
            user = await _active_user(uid)
            if user:
                scope = dict(scope, user=user)
        return await self.app(scope, receive, send)


class MessagesConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        user = self.scope.get("user")
        if not user or not user.is_authenticated:
            await self.close(code=4401)
            return
        from .membership import can_go_live

        if not can_go_live(user):
            await self.close(code=4403)  # free visitors: no real time (the site shows the VIP offer)
            return
        self.uid = user.pk
        self.group_name = group(user.pk)
        self.typing_ok: dict[int, int | None] = {}  # conversation id → other user id (cached checks)
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        # Both badge counts straight away, so the site needs no separate unread requests while connected.
        await self.send_json({"type": "counts", **await database_sync_to_async(_counts)(user)})
        before, _ = await database_sync_to_async(_change_connections)(self.uid, +1)
        if before == 0:
            await self._announce(True)

    async def disconnect(self, code):
        if not hasattr(self, "group_name"):
            return
        await self.channel_layer.group_discard(self.group_name, self.channel_name)
        _, after = await database_sync_to_async(_change_connections)(self.uid, -1)
        if after == 0:
            await self._announce(False)

    async def _announce(self, online: bool):
        payload = {"type": "presence", "user_id": self.uid, "online": online, "last_seen": timezone.now().isoformat()}
        for pid in await database_sync_to_async(partner_ids)(self.uid):
            await self.channel_layer.group_send(group(pid), {"type": "chat.message", "payload": payload})

    async def receive_json(self, content, **kwargs):
        kind = content.get("type")
        if kind == "ping":  # keep-alive; also refreshes the online marker
            await database_sync_to_async(_change_connections)(self.uid, 0)
            await self.send_json({"type": "pong"})
        elif kind == "typing":
            try:
                cid = int(content.get("conversation_id"))
            except (TypeError, ValueError):
                return
            if cid not in self.typing_ok:
                self.typing_ok[cid] = await database_sync_to_async(can_type_in)(self.uid, cid)
            other = self.typing_ok[cid]
            if other:
                await self.channel_layer.group_send(
                    group(other), {"type": "chat.message", "payload": {"type": "typing", "conversation_id": cid}}
                )

    async def chat_message(self, event):
        await self.send_json(event["payload"])


# ---------------------------------------------------------------------------
# Server → browser pushes (called from the REST views)
# ---------------------------------------------------------------------------


def _send(uid, payload) -> None:
    layer = get_channel_layer()
    if layer is None:
        return
    try:
        payload = json.loads(json.dumps(payload, cls=DjangoJSONEncoder))  # dates → ISO strings
        async_to_sync(layer.group_send)(group(uid), {"type": "chat.message", "payload": payload})
    except Exception:  # realtime is best-effort; the REST API still has everything
        pass


def _row(conv, user):
    from .messaging import conversation_summary, with_unread
    from .models import Conversation
    row = with_unread(Conversation.objects.filter(pk=conv.pk), user).select_related("ad", "visitor", "advertiser").first()
    return conversation_summary(row, user) if row else None


def push_message(conv, msg) -> None:
    """Send a new message to both people, each with their own view of the message and conversation row."""
    from .messaging import serialize_message, unread_total, with_extras
    from .models import Message, User

    msg = with_extras(Message.objects.filter(pk=msg.pk)).first()
    if msg is None:
        return
    for user in User.objects.filter(pk__in=[conv.visitor_id, conv.advertiser_id]):
        _send(user.pk, {
            "type": "message",
            "conversation_id": conv.pk,
            "message": serialize_message(msg, user.pk),
            "conversation": _row(conv, user),
            "unread_total": unread_total(user),
        })


def push_update(conv, message_id) -> None:
    """A message was edited, unsent or got a reaction: send the new version to both people."""
    from .messaging import hidden_flag, serialize_message, unread_total, with_extras
    from .models import Message, User

    msg = with_extras(Message.objects.filter(pk=message_id)).first()
    if msg is None:
        return
    for user in User.objects.filter(pk__in=[conv.visitor_id, conv.advertiser_id]):
        if getattr(msg, hidden_flag(conv, user)):
            continue
        _send(user.pk, {
            "type": "message_updated",
            "conversation_id": conv.pk,
            "message": serialize_message(msg, user.pk),
            "conversation": _row(conv, user),
            "unread_total": unread_total(user),
        })


def push_hidden(conv, user, message_id) -> None:
    """I removed a message for myself: update my other tabs."""
    _send(user.pk, {"type": "message_hidden", "conversation_id": conv.pk, "message_id": message_id, "conversation": _row(conv, user)})


def push_read(conv, reader) -> None:
    """`reader` has read the conversation: tell the other person ("Seen") and the reader's other tabs."""
    from .messaging import read_field, unread_total
    read_at = getattr(conv, read_field(conv, reader))
    other = conv.advertiser_id if reader.pk == conv.visitor_id else conv.visitor_id
    _send(other, {"type": "read", "conversation_id": conv.pk, "read_at": read_at.isoformat() if read_at else None})
    _send(reader.pk, {"type": "unread", "conversation_id": conv.pk, "unread_total": unread_total(reader)})
