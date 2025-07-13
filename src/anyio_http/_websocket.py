from __future__ import annotations

import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from anyio import EndOfStream, TypedAttributeSet, typed_attribute
from anyio.abc import AnyByteStream, ObjectStream
from anyio.lowlevel import checkpoint_if_cancelled
from wsproto import ConnectionType
from wsproto.connection import Connection
from wsproto.events import BytesMessage, CloseConnection, Event, Ping, Pong, TextMessage
from wsproto.extensions import Extension
from wsproto.frame_protocol import CloseReason
from wsproto.handshake import client_extensions_handshake

from ._exceptions import HTTPError
from ._response import HTTPResponseAttribute

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override


class WebSocketAttribute(TypedAttributeSet):
    """Contains attributes specific to websocket connections."""

    #: the WebSocket extensions negotiated between the client and the server
    extensions: Sequence[str] = typed_attribute()
    #: the selected subprotocol, if any
    subprotocol: str | None = typed_attribute()


class WebSocketConnection(ObjectStream[bytes | str]):
    def __init__(
        self,
        request_stream: AnyByteStream,
        proposed_extensions: Sequence[Extension],
        subprotocol: str | None,
    ) -> None:
        headers = request_stream.extra(HTTPResponseAttribute.headers)
        accepted_extensions = [
            ext.strip()
            for ext in headers.getone("sec-websocket-extensions", "").split(", ")
            if ext
        ]
        extensions = client_extensions_handshake(
            accepted_extensions,
            proposed_extensions,
        )
        self._connection = Connection(ConnectionType.CLIENT, extensions)
        self._request_stream = request_stream
        self._extensions = extensions
        self._subprotocol = subprotocol

    async def receive(self) -> bytes | str:
        while True:
            if event := next(self._connection.events(), None):
                if isinstance(event, BytesMessage | TextMessage):
                    return event.data
                elif isinstance(event, CloseConnection):
                    await self._request_stream.aclose()
                    raise EndOfStream
                elif isinstance(event, Ping):
                    data = self._connection.send(Pong(event.payload))
                    await self._request_stream.send(data)

            data = await self._request_stream.receive()
            self._connection.receive_data(data)

    async def _send_event(self, event: Event) -> None:
        await checkpoint_if_cancelled()
        data = self._connection.send(event)
        await self._request_stream.send(data)

    @override
    async def send(self, item: bytes | str) -> None:
        event = TextMessage(item) if isinstance(item, str) else BytesMessage(item)
        await self._send_event(event)

    @override
    async def send_eof(self) -> None:
        raise NotImplementedError("websockets don't support half-closing")

    @override
    async def aclose(self, code: int = 1000, reason: str | None = None) -> None:
        try:
            await self._send_event(CloseConnection(code, reason))
        finally:
            await self._request_stream.aclose()

    @property
    def extra_attributes(self) -> Mapping[Any, Callable[[], Any]]:
        return {
            **self._request_stream.extra_attributes,
            WebSocketAttribute.extensions: lambda: tuple(
                ext.name for ext in self._extensions
            ),
            WebSocketAttribute.subprotocol: lambda: self._subprotocol,
        }


class WebSocketError(HTTPError):
    """Base class for all websocket errors."""


class WebSocketConnectionClosed(WebSocketError, EndOfStream):
    """
    Raised when the server closes a websocket connection.
    """

    def __init__(self, reason: str | None):
        super().__init__(CloseReason.NORMAL_CLOSURE, reason)
