from __future__ import annotations

import sys
from collections.abc import Callable, Mapping, Sequence
from functools import cached_property
from random import randbytes
from typing import Any
from warnings import warn

import h11
from anyio import BrokenResourceError, ClosedResourceError, EndOfStream
from anyio.abc import ByteReceiveStream, ByteStream, ObjectStream
from anyio.lowlevel import checkpoint_if_cancelled
from multidict import CIMultiDict, CIMultiDictProxy
from wsproto.extensions import Extension
from yarl import URL

from ._connection import HTTPConnection
from ._exceptions import HTTPStatusError
from ._response import HTTPResponseAttribute
from ._websocket import WebSocketConnection

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override


def parse_headers(raw_headers: list[tuple[bytes, bytes]]) -> CIMultiDictProxy:
    parsed_headers = CIMultiDict(
        [
            (key.decode("ascii"), value.decode("iso-8859-1"))
            for key, value in raw_headers
        ]
    )
    return CIMultiDictProxy(parsed_headers)


class HTTP11Connection(HTTPConnection):
    def __init__(self, transport_stream: ByteStream, scheme: str, host: str) -> None:
        self.transport_stream = transport_stream
        self.scheme = scheme
        self.host = host
        self._connection = h11.Connection(our_role=h11.CLIENT)

    async def aclose(self) -> None:
        if self._connection.our_state is h11.SWITCHED_PROTOCOL:
            # The closure of the request or tunnel will handle this
            return

        self._connection.send(h11.ConnectionClosed())
        assert self._connection.our_state is h11.CLOSED
        try:
            await self.transport_stream.aclose()
        except BrokenResourceError:
            pass

    @property
    def available_slots(self) -> int:
        return int(self._connection.our_state is h11.IDLE)

    @override
    @property
    def closed(self) -> bool:
        return self._connection.our_state is h11.CLOSED

    async def send_event(self, event: h11.Event) -> None:
        await checkpoint_if_cancelled()
        if data := self._connection.send(event):
            await self.transport_stream.send(data)

    async def receive_event(self) -> h11.Event:
        await checkpoint_if_cancelled()
        while True:
            event = self._connection.next_event()
            if isinstance(event, h11.Event):
                return event

            assert event is h11.NEED_DATA
            data = await self.transport_stream.receive()
            self._connection.receive_data(data)

    async def _request(
        self,
        method: str,
        target: URL,
        headers: CIMultiDict,
        body: ByteReceiveStream | None = None,
    ) -> h11.Response | h11.InformationalResponse:
        if body is not None and "content-length" not in headers:
            headers["transfer-encoding"] = "chunked"

        headers.setdefault("host", self.host)
        h11_request = h11.Request(
            method=method, target=str(target), headers=list(headers.items())
        )
        await self.send_event(h11_request)

        # Send the request body, if any
        if body is not None:
            async for chunk in body:
                await self.send_event(h11.Data(data=chunk))

        # Finish the request
        await self.send_event(h11.EndOfMessage())

        # Read data from the server until we have received the response headers
        event = await self.receive_event()
        assert isinstance(event, h11.Response | h11.InformationalResponse)
        return event

    async def request(
        self,
        method: str,
        target: URL,
        headers: CIMultiDict,
        body: ByteReceiveStream | None = None,
    ) -> ByteReceiveStream:
        h11_response = await self._request(method, target, headers, body)
        return HTTP11ResponseStream(
            self.transport_stream, h11_response, self._connection
        )

    async def connect(self, target: URL, *, headers: CIMultiDict) -> HTTP11TunnelStream:
        h11_response = await self._request("CONNECT", target, headers)
        return HTTP11TunnelStream(self.transport_stream, h11_response)

    async def connect_ws(
        self,
        target: URL,
        headers: CIMultiDict,
        subprotocols: Sequence[str],
        proposed_extensions: Sequence[Extension],
    ) -> ObjectStream[bytes | str]:
        headers.update(
            {
                "upgrade": "websocket",
                "connection": "upgrade",
                "origin": f"{self.scheme}://{self.host}{target.raw_path_qs}",
                "sec-websocket-version": "13",
                "sec-websocket-key": randbytes(16).hex(),
            }
        )

        if subprotocols:
            headers["sec-websocket-protocol"] = ", ".join(subprotocols)

        if proposed_extensions:
            headers["sec-websocket-extensions"] = ", ".join(
                ext.name for ext in proposed_extensions
            )

        try:
            h11_response = await self._request("GET", target, headers)
            if (
                isinstance(h11_response, h11.InformationalResponse)
                and h11_response.status_code == 101
            ):
                tunnel_stream = HTTP11TunnelStream(self.transport_stream, h11_response)
                subprotocol = tunnel_stream.headers.get("sec-websocket-protocol")
                return WebSocketConnection(
                    tunnel_stream, proposed_extensions, subprotocol
                )

            body: bytes | None = None
            while True:
                try:
                    event = await self.receive_event()
                except EndOfStream:
                    break

                if isinstance(event, h11.EndOfMessage):
                    break

                assert isinstance(event, h11.Data)
                body = (body + event.data) if body else event.data
        except BaseException:
            await self.aclose()
            raise

        raise HTTPStatusError(
            h11_response.status_code,
            parse_headers(h11_response.headers.raw_items()),
            body,
        )


class HTTP11TunnelStream(ByteStream):
    def __init__(
        self,
        transport_stream: ByteStream,
        response: h11.Response | h11.InformationalResponse,
    ) -> None:
        self._transport_stream = transport_stream
        self._status_code = response.status_code
        self._http_version = response.http_version.decode("ascii")
        self._raw_headers = response.headers.raw_items()
        self._closed = False

    @cached_property
    def headers(self) -> CIMultiDictProxy:
        parsed_headers = CIMultiDict(
            [
                (key.decode("ascii"), value.decode("iso-8859-1"))
                for key, value in self._raw_headers
            ]
        )
        del self._raw_headers
        return CIMultiDictProxy(parsed_headers)

    @override
    @property
    def extra_attributes(self) -> Mapping[Any, Callable[[], Any]]:
        return {
            **self._transport_stream.extra_attributes,
            HTTPResponseAttribute.http_version: lambda: self._http_version,
            HTTPResponseAttribute.status_code: lambda: self._status_code,
            HTTPResponseAttribute.headers: lambda: self.headers,
        }

    @override
    async def send_eof(self) -> None:
        await self._transport_stream.send_eof()

    @override
    async def receive(self, max_bytes: int = 65536) -> bytes:
        return await self._transport_stream.receive(max_bytes)

    @override
    async def send(self, item: bytes) -> None:
        await self._transport_stream.send(item)

    @override
    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                await self._transport_stream.aclose()
            except BrokenResourceError:
                pass

    def __del__(self) -> None:
        if not self._closed:
            warn(f"unclosed {self!r}", ResourceWarning, stacklevel=1, source=self)


class HTTP11ResponseStream(HTTP11TunnelStream):
    """Low-level HTTP/1.1 response stream."""

    def __init__(
        self,
        transport_stream: ByteStream,
        response: h11.Response | h11.InformationalResponse,
        connection: h11.Connection,
    ) -> None:
        super().__init__(transport_stream, response)
        self._connection = connection
        self._eof_sent = connection.our_state is h11.DONE
        self._eof_received = connection.their_state is h11.DONE
        self._closed = False

    async def _send_event(self, event: h11.Event) -> None:
        if self._eof_sent:
            raise ClosedResourceError

        if data := self._connection.send(event):
            await self._transport_stream.send(data)

    @override
    async def receive(self, max_bytes: int = 65535) -> bytes:
        if self._closed:
            raise ClosedResourceError
        elif self._eof_received:
            raise EndOfStream

        while (event := self._connection.next_event()) is h11.NEED_DATA:
            data = await self._transport_stream.receive(max_bytes)
            self._connection.receive_data(data)

        match event:
            case h11.Data():
                return event.data
            case h11.ConnectionClosed():
                raise BrokenResourceError("server closed connection prematurely")
            case h11.EndOfMessage():
                self._eof_received = True
                raise EndOfStream
            case _:
                raise RuntimeError(f"unexpected event: {event}")

    @override
    async def send(self, item: bytes) -> None:
        await self._send_event(h11.Data(data=item))

    @override
    async def send_eof(self) -> None:
        if not self._eof_sent:
            self._eof_sent = True
            await self._send_event(h11.EndOfMessage())

    @override
    async def aclose(self) -> None:
        if self._closed:
            return

        self._closed = True
        if self._connection.their_state is h11.DONE:
            # Reset state to begin a new request reusing this connection
            self._connection.start_next_cycle()
        else:
            # Close the underlying stream
            self._connection.send(h11.ConnectionClosed())
            try:
                await self._transport_stream.aclose()
            except BrokenResourceError:
                pass

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} state={self._connection.our_state}>"
