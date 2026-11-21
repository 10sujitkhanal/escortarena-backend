"""Private messages between visitors and advertisers, one conversation per (ad, visitor).

Privacy: e-mail addresses are never exposed – only display names. Notification
e-mails contain no message text and honour the recipient's discretion setting.
"""

from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from .models import (
    Ad,
    Conversation,
    Message,
    MessageAttachment,
    MessageReaction,
    User,
    UserReport,
)

MAX_LEN = 2000
PAGE = 200  # newest messages returned for a thread
MAX_FILES = 10  # photos/videos per message
MAX_VIDEO_BYTES = 50 * 1024 * 1024
VIDEO_TYPES = {"video/mp4": "mp4", "video/webm": "webm", "video/quicktime": "mov"}
EDIT_MINUTES = 15  # like Messenger: messages can be edited for a while after sending
REACTIONS = ["❤️", "😂", "😮", "😢", "😡", "👍"]


class MessageThrottle(SimpleRateThrottle):
    scope = "messages"

    def get_cache_key(self, request, view):
        if request.method != "POST":
            return None
        return self.cache_format % {"scope": self.scope, "ident": request.user.pk}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def display_name(user: User, conv: Conversation) -> str:
    if user.pk == conv.advertiser_id:
        return user.display_name or conv.ad_title or "Advertiser"
    return user.display_name or f"Visitor {user.pk}"


def read_field(conv: Conversation, user: User) -> str:
    return "visitor_read_at" if user.pk == conv.visitor_id else "advertiser_read_at"


def hidden_field(conv: Conversation, user: User) -> str:
    return "visitor_hidden" if user.pk == conv.visitor_id else "advertiser_hidden"


def for_user(user: User):
    return Conversation.objects.filter(
        Q(visitor=user, visitor_hidden=False) | Q(advertiser=user, advertiser_hidden=False)
    )


def unread_q(user: User) -> Q:
    """Messages from the other person newer than my read marker."""
    return ~Q(messages__sender=user) & Q(messages__deleted_at__isnull=True) & (
        Q(visitor=user) & (Q(visitor_read_at__isnull=True) | Q(messages__created_at__gt=F("visitor_read_at")))
        | Q(advertiser=user) & (Q(advertiser_read_at__isnull=True) | Q(messages__created_at__gt=F("advertiser_read_at")))
    )


def with_unread(qs, user: User):
    return qs.annotate(unread=Count("messages", filter=unread_q(user), distinct=True))


def hidden_flag(conv: Conversation, user: User) -> str:
    """Message field for "removed for me" on this user's side."""
    return "hidden_for_visitor" if user.pk == conv.visitor_id else "hidden_for_advertiser"


def visible_messages(conv: Conversation, user: User):
    return conv.messages.filter(**{hidden_flag(conv, user): False})


def preview(msg: Message | None) -> str:
    """Inbox line for a message: text, or "📷 Photo" / "🎥 Video" / "Message removed"."""
    if msg is None:
        return ""
    if msg.deleted_at:
        return "Message removed"
    if msg.body:
        return msg.body[:120]
    kinds = [a.kind for a in msg.attachments.all()]
    if not kinds:
        return ""
    if all(k == "image" for k in kinds):
        return "📷 Photo" if len(kinds) == 1 else f"📷 {len(kinds)} photos"
    if all(k == "video" for k in kinds):
        return "🎥 Video" if len(kinds) == 1 else f"🎥 {len(kinds)} videos"
    return f"📎 {len(kinds)} files"


def serialize_message(m: Message, user_id: int) -> dict:
    """One message as `user_id` sees it (works without a request, so the socket can use it too)."""
    deleted = bool(m.deleted_at)
    reply = m.reply_to
    return {
        "id": m.id,
        "body": "" if deleted else m.body,
        "created_at": m.created_at.isoformat(),
        "mine": m.sender_id == user_id,
        "edited": bool(m.edited_at) and not deleted,
        "deleted": deleted,
        "reply_to": {
            "id": reply.id,
            "mine": reply.sender_id == user_id,
            "text": preview(reply),
            "deleted": bool(reply.deleted_at),
        } if reply else None,
        "attachments": [] if deleted else [
            {"id": a.id, "kind": a.kind, "url": f"/api/messages/files/{a.id}", "width": a.width, "height": a.height, "size": a.size}
            for a in m.attachments.all()
        ],
        "reactions": [] if deleted else [{"emoji": r.emoji, "mine": r.user_id == user_id} for r in m.reactions.all()],
    }


def with_extras(qs):
    return qs.select_related("reply_to").prefetch_related("attachments", "reactions", "reply_to__attachments")


def read_media(conv: Conversation, uploads) -> list[dict]:
    """Validate and prepare uploaded photos/videos before anything is saved."""
    from django.core.files.base import ContentFile

    from .images import InvalidImage, process_upload

    if len(uploads) > MAX_FILES:
        raise ValidationError({"files": f"You can send at most {MAX_FILES} photos or videos at a time."})
    prepared = []
    for up in uploads:
        ctype = (up.content_type or "").split(";")[0].lower()
        if ctype.startswith("image/"):
            try:
                content, w, h = process_upload(up)  # re-encoded to WebP; location data removed
            except InvalidImage as exc:
                raise ValidationError({"files": f"{up.name}: {exc}"}) from exc
            prepared.append({"kind": "image", "content": content, "name": "photo.webp", "ctype": "image/webp", "w": w, "h": h})
        elif ctype in VIDEO_TYPES:
            if up.size > MAX_VIDEO_BYTES:
                raise ValidationError({"files": f"{up.name}: videos can be at most 50 MB."})
            head = up.read(16)
            up.seek(0)
            ok = head[4:8] == b"ftyp" if ctype != "video/webm" else head[:4] == b"\x1a\x45\xdf\xa3"
            if not ok:
                raise ValidationError({"files": f"{up.name}: this is not a valid video file."})
            prepared.append({"kind": "video", "content": ContentFile(up.read()), "name": f"video.{VIDEO_TYPES[ctype]}", "ctype": ctype, "w": None, "h": None})
        else:
            raise ValidationError({"files": f"{up.name}: only photos and videos (MP4, WebM, MOV) can be sent."})
    return prepared


def unread_total(user: User) -> int:
    return sum(c.unread for c in with_unread(for_user(user), user))


def other_read_at(conv: Conversation, user: User):
    """When the other person last read this conversation (for "Seen")."""
    return conv.advertiser_read_at if user.pk == conv.visitor_id else conv.visitor_read_at


def mark_read(conv: Conversation, user: User) -> None:
    """Move my read marker to now; tell the other person and my other tabs if anything was unread."""
    field = read_field(conv, user)
    read_at = getattr(conv, field)
    had_unread = conv.messages.exclude(sender=user).filter(**({"created_at__gt": read_at} if read_at else {})).exists()
    setattr(conv, field, timezone.now())
    Conversation.objects.filter(pk=conv.pk).update(**{field: getattr(conv, field)})
    if had_unread:
        from .realtime import push_read
        transaction.on_commit(lambda: push_read(conv, user))


def notify(conv: Conversation, sender: User, previous_last: object) -> None:
    """E-mail the recipient once per unread streak (only if they had read everything before).

    No e-mail while the recipient is online – they see the message live. The e-mail is sent
    in the background so sending a chat message stays instant.
    """
    from .realtime import is_online
    recipient = conv.other(sender)
    if not recipient.notify_messages or not recipient.is_active or is_online(recipient.pk):
        return
    read_at = getattr(conv, read_field(conv, recipient))
    had_unread = previous_last is not None and (read_at is None or read_at < previous_last)
    if had_unread:
        return
    from . import emails
    emails.send(to=recipient.email, user=recipient, subject="you have a new message", heading="You have a new message",
                lines=["Someone sent you a new message. Open your inbox to read it and reply."],
                button=("Read the message", f"/messages/{conv.pk}"),
                note="You can turn these e-mails off in Settings → Notifications.")


def _in_background(fn) -> None:
    """Run after the transaction commits, off the request thread (inline in tests: locmem backend)."""
    import threading

    def run():
        try:
            fn()
        except Exception:
            pass

    if "locmem" in settings.EMAIL_BACKEND:
        run()
    else:
        transaction.on_commit(lambda: threading.Thread(target=run, daemon=True).start())


def post_message(conv: Conversation, sender: User, body: str, files=(), reply_to=None) -> Message:
    body = (body or "").strip()
    if not body and not files:
        raise ValidationError({"body": "Write a message or add a photo or video."})
    if len(body) > MAX_LEN:
        raise ValidationError({"body": f"Messages can be at most {MAX_LEN} characters."})
    if conv.blocked_by_id:
        raise PermissionDenied("This conversation is blocked.")
    if sender.pk == conv.visitor_id:
        from .membership import require_vip
        require_vip(sender, "message")  # visitors need VIP to write; advertisers can always answer
    reply = None
    if reply_to:
        reply = conv.messages.filter(pk=reply_to).first()
        if reply is None:
            raise ValidationError({"reply_to": "That message is not in this conversation."})
    media = read_media(conv, list(files))
    previous_last = conv.last_message_at if conv.messages.exists() else None
    now = timezone.now()
    with transaction.atomic():
        msg = Message.objects.create(conversation=conv, sender=sender, body=body, reply_to=reply)
        for i, f in enumerate(media):
            att = MessageAttachment(message=msg, kind=f["kind"], content_type=f["ctype"], size=f["content"].size,
                                    width=f["w"], height=f["h"], position=i)
            att.file.save(f["name"], f["content"], save=True)
        setattr(conv, read_field(conv, sender), now)
        conv.last_message_at = now
        conv.visitor_hidden = conv.advertiser_hidden = False  # a new message brings the thread back
        conv.save()
    notify(conv, sender, previous_last)
    from .realtime import push_message

    transaction.on_commit(lambda: push_message(conv, msg))
    return msg


def get_own_message(conv: Conversation, user: User, mid: int) -> Message:
    msg = get_object_or_404(with_extras(conv.messages.filter(**{hidden_flag(conv, user): False})), pk=mid)
    return msg


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------


def conversation_summary(conv: Conversation, user: User) -> dict:
    from .realtime import is_online, last_seen
    other = conv.other(user)
    seen = other_read_at(conv, user)
    last = visible_messages(conv, user).prefetch_related("attachments").order_by("-created_at", "-id").first()
    cover = None
    if conv.ad_id:
        pic = conv.ad.pictures.first()
        cover = pic.image.url if pic and pic.image else None
    return {
        "id": conv.pk,
        "role": conv.role_of(user),
        "other_name": display_name(other, conv),
        "other_initials": "".join(w[0] for w in display_name(other, conv).split()[:2]).upper() or "?",
        "ad_id": str(conv.ad_id) if conv.ad_id else None,
        "ad_title": conv.ad_title,
        "ad_cover": cover,
        "last_message": preview(last),
        "last_message_mine": bool(last and last.sender_id == user.pk),
        "last_message_at": conv.last_message_at,
        "unread": getattr(conv, "unread", 0),
        "blocked": bool(conv.blocked_by_id),
        "blocked_by_me": conv.blocked_by_id == user.pk,
        "other_id": other.pk,
        "other_profile": bool(other.is_advertiser and other.is_active),
        "other_online": is_online(other.pk),
        "other_last_seen": last_seen(other.pk),
        "other_read_at": seen.isoformat() if seen else None,
    }


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


class StartConversationView(APIView):
    """POST /api/ads/<id>/message – first (or next) message to an ad's advertiser."""

    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [MessageThrottle]

    def post(self, request, pk):
        ad = get_object_or_404(Ad.objects.live().select_related("owner"), pk=pk)
        if ad.owner_id == request.user.pk:
            raise ValidationError({"detail": "You can't message your own ad."})
        try:
            conv, _ = Conversation.objects.get_or_create(
                ad=ad, visitor=request.user, defaults={"advertiser": ad.owner, "ad_title": ad.title}
            )
        except IntegrityError:
            conv = Conversation.objects.get(ad=ad, visitor=request.user)
        post_message(conv, request.user, request.data.get("body", ""))
        return Response({"conversation_id": conv.pk}, status=status.HTTP_201_CREATED)


class ConversationListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        qs = with_unread(for_user(request.user), request.user).select_related("ad", "visitor", "advertiser")
        return Response([conversation_summary(c, request.user) for c in qs[:100]])


class RealtimeTicketView(APIView):
    """A short-lived ticket plus the address for the chat WebSocket (see realtime.TicketAuthMiddleware)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .membership import can_go_live
        from .realtime import TICKET_MAX_AGE, make_ticket

        if not can_go_live(request.user):
            raise PermissionDenied({"detail": "Real-time messages are for VIP members.", "vip_required": True})
        return Response({"ticket": make_ticket(request.user.pk), "url": settings.REALTIME_WS_URL,
                         "expires_in": TICKET_MAX_AGE})


class UnreadCountView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response({"unread": unread_total(request.user)})


class ConversationDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [MessageThrottle]

    def get_conv(self, request, pk):
        conv = get_object_or_404(
            Conversation.objects.select_related("ad", "visitor", "advertiser"),
            Q(visitor=request.user) | Q(advertiser=request.user),
            pk=pk,
        )
        return conv

    def get(self, request, pk):
        conv = self.get_conv(request, pk)
        msgs = list(with_extras(visible_messages(conv, request.user)).order_by("-created_at", "-id")[:PAGE])[::-1]
        mark_read(conv, request.user)  # opening the thread marks it read
        data = conversation_summary(conv, request.user)
        data["unread"] = 0
        data["messages"] = [serialize_message(m, request.user.pk) for m in msgs]
        data["reactions"] = REACTIONS
        data["edit_minutes"] = EDIT_MINUTES
        return Response(data)

    def post(self, request, pk):
        """Send a message: text and/or photos/videos (multipart "files"), optionally replying to one."""
        conv = self.get_conv(request, pk)
        msg = post_message(conv, request.user, request.data.get("body", ""), request.FILES.getlist("files"),
                           request.data.get("reply_to") or None)
        msg = with_extras(Message.objects.filter(pk=msg.pk)).get()
        return Response(serialize_message(msg, request.user.pk), status=status.HTTP_201_CREATED)

    def delete(self, request, pk):
        """Delete the chat for me: hides the conversation and its messages so far.

        If the other person writes again, the chat comes back with only the new messages.
        """
        conv = self.get_conv(request, pk)
        conv.messages.update(**{hidden_flag(conv, request.user): True})
        setattr(conv, hidden_field(conv, request.user), True)
        conv.save(update_fields=[hidden_field(conv, request.user)])
        return Response(status=status.HTTP_204_NO_CONTENT)


class ReadConversationView(APIView):
    """POST /api/conversations/<id>/read – the open thread got a new message and the user saw it."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        conv = get_object_or_404(Conversation, Q(visitor=request.user) | Q(advertiser=request.user), pk=pk)
        mark_read(conv, request.user)
        return Response({"unread": unread_total(request.user)})


class BlockConversationView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        conv = get_object_or_404(Conversation, Q(visitor=request.user) | Q(advertiser=request.user), pk=pk)
        if conv.blocked_by_id and conv.blocked_by_id != request.user.pk:
            raise PermissionDenied("The other person has blocked this conversation.")
        conv.blocked_by = None if conv.blocked_by_id else request.user
        conv.save(update_fields=["blocked_by"])
        return Response({"blocked": bool(conv.blocked_by_id), "blocked_by_me": bool(conv.blocked_by_id)})


class ReportConversationView(APIView):
    """POST /api/conversations/<id>/report – report the other person in the chat (optionally block them too)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        return Response({"reasons": [{"value": v, "label": l} for v, l in UserReport.Reason.choices]})

    def post(self, request, pk):
        conv = get_object_or_404(Conversation, Q(visitor=request.user) | Q(advertiser=request.user), pk=pk)
        reason = request.data.get("reason")
        if reason not in UserReport.Reason.values:
            raise ValidationError({"reason": "Choose a reason."})
        message = str(request.data.get("message") or "").strip()[:2000]
        if reason == UserReport.Reason.OTHER and len(message) < 5:
            raise ValidationError({"message": "Tell us briefly what happened."})
        other = conv.other(request.user)
        recent = UserReport.objects.filter(reporter=request.user, reported=other,
                                           created_at__gte=timezone.now() - timedelta(hours=1))
        if not recent.exists():
            UserReport.objects.create(reporter=request.user, reported=other, conversation=conv,
                                      reason=reason, message=message)
        blocked = bool(request.data.get("block")) and not conv.blocked_by_id
        if blocked:
            conv.blocked_by = request.user
            conv.save(update_fields=["blocked_by"])
        return Response({"reported": True, "blocked": bool(conv.blocked_by_id),
                         "blocked_by_me": conv.blocked_by_id == request.user.pk}, status=status.HTTP_201_CREATED)


class MessageView(APIView):
    """PATCH: edit my message (text, within EDIT_MINUTES). DELETE ?for=everyone|me: unsend, or remove just for me."""

    permission_classes = [permissions.IsAuthenticated]

    def setup_conv(self, request, pk, mid):
        conv = get_object_or_404(Conversation, Q(visitor=request.user) | Q(advertiser=request.user), pk=pk)
        return conv, get_own_message(conv, request.user, mid)

    def patch(self, request, pk, mid):
        from .realtime import push_update
        conv, msg = self.setup_conv(request, pk, mid)
        if msg.sender_id != request.user.pk or msg.deleted_at:
            raise PermissionDenied("You can only edit your own messages.")
        if conv.blocked_by_id:
            raise PermissionDenied("This conversation is blocked.")
        if timezone.now() - msg.created_at > timedelta(minutes=EDIT_MINUTES):
            raise ValidationError({"body": f"Messages can only be edited for {EDIT_MINUTES} minutes."})
        body = (request.data.get("body") or "").strip()
        if not body and not msg.attachments.exists():
            raise ValidationError({"body": "Write a message."})
        if len(body) > MAX_LEN:
            raise ValidationError({"body": f"Messages can be at most {MAX_LEN} characters."})
        if body != msg.body:
            msg.body, msg.edited_at = body, timezone.now()
            msg.save(update_fields=["body", "edited_at"])
            transaction.on_commit(lambda: push_update(conv, msg.pk))
        return Response(serialize_message(msg, request.user.pk))

    def delete(self, request, pk, mid):
        from .realtime import push_hidden, push_update
        conv, msg = self.setup_conv(request, pk, mid)
        if request.query_params.get("for") == "everyone":
            if msg.sender_id != request.user.pk:
                raise PermissionDenied("You can only unsend your own messages.")
            if not msg.deleted_at:
                for a in msg.attachments.all():
                    a.file.delete(save=False)
                msg.attachments.all().delete()
                msg.reactions.all().delete()
                msg.body, msg.deleted_at = "", timezone.now()
                msg.save(update_fields=["body", "deleted_at"])
                transaction.on_commit(lambda: push_update(conv, msg.pk))
        else:
            setattr(msg, hidden_flag(conv, request.user), True)
            msg.save(update_fields=[hidden_flag(conv, request.user)])
            transaction.on_commit(lambda: push_hidden(conv, request.user, msg.pk))
        return Response(status=status.HTTP_204_NO_CONTENT)


class ReactView(APIView):
    """POST {emoji}: set my reaction; the same emoji again (or empty) removes it."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk, mid):
        from .realtime import push_update
        conv = get_object_or_404(Conversation, Q(visitor=request.user) | Q(advertiser=request.user), pk=pk)
        msg = get_own_message(conv, request.user, mid)
        if msg.deleted_at or conv.blocked_by_id:
            raise PermissionDenied("You can't react to this message.")
        emoji = request.data.get("emoji") or ""
        if emoji and emoji not in REACTIONS:
            raise ValidationError({"emoji": "Choose one of the reactions."})
        current = MessageReaction.objects.filter(message=msg, user=request.user).first()
        if current and (not emoji or current.emoji == emoji):
            current.delete()
        elif emoji:
            MessageReaction.objects.update_or_create(message=msg, user=request.user, defaults={"emoji": emoji})
        transaction.on_commit(lambda: push_update(conv, msg.pk))
        msg = with_extras(Message.objects.filter(pk=msg.pk)).get()
        return Response(serialize_message(msg, request.user.pk))


class AttachmentFileView(APIView):
    """GET /api/messages/files/<id> – only for the two participants. Supports Range, so videos can seek."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, aid):
        return attachment_file(request, aid)


def attachment_file(request, aid):
    import mimetypes
    import os
    import re

    from django.http import FileResponse, Http404, HttpResponse, StreamingHttpResponse

    user = request.user
    if not user.is_authenticated:
        return HttpResponse(status=401)
    att = MessageAttachment.objects.select_related("message__conversation").filter(pk=aid).first()
    if not att or att.message.deleted_at or user.pk not in (att.message.conversation.visitor_id, att.message.conversation.advertiser_id):
        raise Http404
    path = att.file.path
    size = os.path.getsize(path)
    ctype = att.content_type or mimetypes.guess_type(path)[0] or "application/octet-stream"
    match = re.match(r"bytes=(\d*)-(\d*)", request.headers.get("Range", ""))
    if match and (match.group(1) or match.group(2)):
        start = int(match.group(1)) if match.group(1) else max(0, size - int(match.group(2)))
        end = int(match.group(2)) if match.group(1) and match.group(2) else size - 1
        end = min(end, size - 1)
        if start > end:
            resp = HttpResponse(status=416)
            resp["Content-Range"] = f"bytes */{size}"
            return resp

        def chunks(f=open(path, "rb"), left=end - start + 1):
            with f:
                f.seek(start)
                while left > 0:
                    data = f.read(min(64 * 1024, left))
                    if not data:
                        break
                    left -= len(data)
                    yield data

        resp = StreamingHttpResponse(chunks(), status=206, content_type=ctype)
        resp["Content-Range"] = f"bytes {start}-{end}/{size}"
        resp["Content-Length"] = str(end - start + 1)
    else:
        resp = FileResponse(open(path, "rb"), content_type=ctype)
        resp["Content-Length"] = str(size)
    resp["Accept-Ranges"] = "bytes"
    resp["Cache-Control"] = "private, max-age=86400"
    resp["X-Content-Type-Options"] = "nosniff"
    resp["Content-Disposition"] = "inline"
    return resp
