"""ASGI entry point: HTTP via Django, WebSockets (/ws/...) via Channels."""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django_asgi_app = get_asgi_application()  # must run before importing consumers

from channels.auth import AuthMiddlewareStack  # noqa: E402
from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import AllowedHostsOriginValidator  # noqa: E402
from django.urls import path  # noqa: E402

from core.realtime import MessagesConsumer  # noqa: E402

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    # Session cookie authenticates the socket; Origin must be an allowed host.
    "websocket": AllowedHostsOriginValidator(
        AuthMiddlewareStack(URLRouter([path("ws/messages/", MessagesConsumer.as_asgi())]))
    ),
})
