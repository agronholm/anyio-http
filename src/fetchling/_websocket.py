from __future__ import annotations

import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from anyio import BrokenResourceError, EndOfStream, TypedAttributeSet, typed_attribute
from anyio.abc import AnyByteStream, ObjectStream
from anyio.lowlevel import checkpoint_if_cancelled
from wsproto import ConnectionType
from wsproto.connection import Connection
from wsproto.events import BytesMessage, CloseConnection, Event, Ping, TextMessage
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
        self._close_code: int | None = None
        self._close_reason: str | None = None

    @override
    async def receive(self) -> bytes | str:
        def maybe_raise_close_error() -> None:
            if self._close_code is not None:
                if self._close_code == CloseReason.NORMAL_CLOSURE:
                    raise WebSocketConnectionEnded
                else:
                    raise WebSocketConnectionBroken(
                        self._close_code, self._close_reason
                    )

        while True:
            maybe_raise_close_error()
            await checkpoint_if_cancelled()
            if event := next(self._connection.events(), None):
                match event:
                    case BytesMessage() | TextMessage():
                        return event.data
                    case CloseConnection():
                        self._close_code = event.code
                        self._close_reason = event.reason or None
                        await self._request_stream.aclose()
                        maybe_raise_close_error()
                    case Ping():
                        await self._send_event(event.response())

            try:
                data = await self._request_stream.receive()
            except EndOfStream as exc:
                self._close_code = CloseReason.ABNORMAL_CLOSURE
                raise WebSocketConnectionBroken(
                    CloseReason.ABNORMAL_CLOSURE, "server closed connection prematurely"
                ) from exc

            self._connection.receive_data(data)

    async def _send_event(self, event: Event) -> None:
        await checkpoint_if_cancelled()
        data = self._connection.send(event)
        await self._request_stream.send(data)

    @override
    async def send(self, item: bytes | str) -> None:
        if self._close_code is not None:
            raise WebSocketConnectionBroken(self._close_code, self._close_reason)

        event = TextMessage(item) if isinstance(item, str) else BytesMessage(item)
        await self._send_event(event)

    @override
    async def send_eof(self) -> None:
        raise NotImplementedError("websockets don't support half-closing")

    @override
    async def aclose(
        self, code: int = CloseReason.NORMAL_CLOSURE, reason: str | None = None
    ) -> None:
        if self._close_code is not None:
            return

        try:
            await self._send_event(CloseConnection(code, reason))
        finally:
            await self._request_stream.aclose()

    @override
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


class WebSocketConnectionBroken(WebSocketError, BrokenResourceError):
    """
    Raised when the websocket connection is closed uncleanly.

    If the closure was due to a transport-level problem, the code 1006 will be used,
    and the underlying exception is stored in ``__cause__``.

    :ivar code: the close code
    :type code: int
    :ivar reason: the close reason
    :type reason: str | None
    """

    def __init__(self, code: int, reason: str | None):
        self.code = code
        self.reason = reason


class WebSocketConnectionEnded(WebSocketError, EndOfStream):
    """
    Raised when trying to receive from a websocket connection that the other end has
    closed cleanly (with a code of 1000).
    """
