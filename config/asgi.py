"""ASGI entry point: HTTP via Django, WebSockets (/ws/...) via Channels."""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django_asgi_app = get_asgi_application()  # must run before importing consumers

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import OriginValidator
from django.conf import settings
from django.urls import path

from core.realtime import MessagesConsumer, TicketAuthMiddleware

# Sockets may come from the API's own hosts or from the frontend sites (www.… on Vercel connects directly).
ALLOWED_ORIGINS = list(dict.fromkeys([*settings.ALLOWED_HOSTS, *settings.FRONTEND_ORIGINS, *settings.CSRF_TRUSTED_ORIGINS]))

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    # Session cookie or a short-lived ?ticket= authenticates the socket; Origin must be our site or API.
    "websocket": OriginValidator(
        AuthMiddlewareStack(TicketAuthMiddleware(URLRouter([path("ws/messages/", MessagesConsumer.as_asgi())]))),
        ALLOWED_ORIGINS,
    ),
})
