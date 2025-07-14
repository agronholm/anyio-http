from __future__ import annotations

from ._asgi import ASGIApp as ASGIApp
from ._asgi import ASGIError as ASGIError
from ._asgi import ASGIEvent as ASGIEvent
from ._client import HTTPClient as HTTPClient
from ._client import HTTPRequest as HTTPRequest
from ._decoders import ContentDecoder as ContentDecoder
from ._exceptions import HTTPError as HTTPError
from ._exceptions import HTTPStatusError as HTTPStatusError
from ._response import HTTPResponse as HTTPResponse
from ._response import HTTPResponseAttribute as HTTPResponseAttribute
from ._sse import ServerEvent as ServerEvent
from ._sse import ServerEventStream as ServerEventStream
from ._websocket import WebSocketAttribute as WebSocketAttribute
from ._websocket import WebSocketConnection as WebSocketConnection
from ._websocket import WebSocketConnectionBroken as WebSocketConnectionBroken
from ._websocket import WebSocketConnectionEnded as WebSocketConnectionEnded
from ._websocket import WebSocketError as WebSocketError
from ._wrappers import CBOR as CBOR
from ._wrappers import JSON as JSON
from ._wrappers import ContentWrapper as ContentWrapper
from ._wrappers import Form as Form
from ._wrappers import FormField as FormField

for __value in list(locals().values()):
    if getattr(__value, "__module__", "").startswith("anyio_http."):
        __value.__module__ = __name__

locals().pop("__value", None)
