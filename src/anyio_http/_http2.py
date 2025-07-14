from __future__ import annotations

import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from functools import cached_property
from typing import Any, cast
from warnings import warn

from anyio import (
    BrokenResourceError,
    CancelScope,
    EndOfStream,
    Event,
    Lock,
    create_memory_object_stream,
    create_task_group,
    move_on_after,
)
from anyio.abc import (
    ByteReceiveStream,
    ByteStream,
    ObjectStream,
    TaskStatus,
)
from anyio.lowlevel import checkpoint_if_cancelled
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from h2.config import H2Configuration
from h2.connection import ConnectionState, H2Connection
from h2.errors import ErrorCodes
from h2.events import (
    ConnectionTerminated,
    DataReceived,
    PingReceived,
    ResponseReceived,
    SettingsAcknowledged,
    StreamEnded,
    StreamReset,
    WindowUpdated,
)
from h2.events import Event as H2Event
from h2.stream import H2Stream, StreamState
from hpack import HeaderTuple
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


class HTTP2Connection(HTTPConnection):
    def __init__(self, transport_stream: ByteStream, scheme: str, host: str) -> None:
        self.transport_stream = transport_stream
        self.scheme = scheme
        self.host = host
        config = H2Configuration(client_side=True)
        self._connection = H2Connection(config)
        self._streams: dict[int, MemoryObjectSendStream[H2Event]] = {}
        self._outbound_window_events: dict[int, Event] = {}
        self._io_lock = Lock(fast_acquire=True)
        self._manage_cancel_scope: CancelScope | None = None
        self._manage_stopped_event = Event()

    async def aclose(self) -> None:
        if self._manage_cancel_scope is None:
            # Close the transport stream, if not already closed
            self._connection.close_connection()
            try:
                await self.transport_stream.aclose()
            except BrokenResourceError:
                pass
        else:
            # Cancel the manager task and wait for it to exit
            self._manage_cancel_scope.cancel()
            self._manage_cancel_scope = None
            await self._manage_stopped_event.wait()

    @property
    def available_slots(self) -> int:
        if self._connection.state_machine.state is ConnectionState.CLOSED:
            return 0

        return (
            self._connection.remote_settings.max_concurrent_streams
            - self._connection.open_outbound_streams
        )

    @override
    @property
    def closed(self) -> bool:
        return self._connection.state_machine.state is ConnectionState.CLOSED

    async def manage(self, *, task_status: TaskStatus[None]) -> None:
        async def close_transport_stream() -> None:
            try:
                self._connection.close_connection()
                await self._flush_outgoing_data()
            finally:
                try:
                    await self.transport_stream.aclose()
                except BrokenResourceError:
                    pass

        async def close_request_stream(
            stream_id: int,
            request_stream: MemoryObjectSendStream[H2Event],
        ) -> None:
            reset = StreamReset()
            reset.stream_id = stream_id
            reset.error_code = ErrorCodes.CANCEL
            reset.remote_reset = False
            try:
                await request_stream.send(reset)
            finally:
                request_stream.close()

        async def close_request_streams() -> None:
            # Send a StreamReset event to all internal streams, telling them that we
            # closed the stream rather than the remote host
            with move_on_after(10, shield=True):
                async with create_task_group() as tg:
                    for stream_id, request_stream in self._streams.items():
                        tg.start_soon(close_request_stream, stream_id, request_stream)

                    self._streams.clear()

        async with AsyncExitStack() as stack:
            # Notify listeners on exit
            stack.callback(self._manage_stopped_event.set)

            # Ensure that the transport stream is closed on exit
            stack.push_async_callback(close_transport_stream)

            # Send the connection preamble
            self._connection.initiate_connection()
            await self._flush_outgoing_data()

            # Ensure that all internal streams are closed on exit
            stack.push_async_callback(close_request_streams)

            # Read incoming data and parse events from it.
            # Send stream specific events to their respective memory object streams.
            task_status_sent = False
            self._manage_cancel_scope = stack.enter_context(CancelScope())
            while self._connection.state_machine.state is not ConnectionState.CLOSED:
                try:
                    data = await self.transport_stream.receive()
                except EndOfStream:
                    self._connection.close_connection()
                    break

                for event in self._connection.receive_data(data):
                    match event:
                        case DataReceived():
                            assert event.stream_id is not None

                            # Ignore empty data frames, as they'll always be followed by
                            # StreamEnded
                            if event.flow_controlled_length:
                                await self._streams[event.stream_id].send(event)
                                self._connection.acknowledge_received_data(
                                    event.flow_controlled_length,
                                    event.stream_id,
                                )
                        case ResponseReceived():
                            assert event.stream_id is not None
                            if event.stream_id > 0:
                                send_stream = self._streams[event.stream_id]
                                await send_stream.send(event)
                        case StreamEnded() | StreamReset():
                            assert event.stream_id is not None
                            if event.stream_id > 0:
                                send_stream = self._streams[event.stream_id]
                                try:
                                    await send_stream.send(event)
                                except BrokenResourceError:
                                    # already closed from the HTTPResponseStream end
                                    continue

                                send_stream.close()
                                del self._streams[event.stream_id]
                        case WindowUpdated():
                            assert event.stream_id is not None
                            if event.stream_id > 0:
                                await self._streams[event.stream_id].send(event)
                            else:
                                if self._connection.outbound_flow_control_window:
                                    self._can_send_event.set()
                        case PingReceived():
                            # Send the automatically generated ping ack
                            await self._flush_outgoing_data()
                        case SettingsAcknowledged():
                            if not task_status_sent:
                                task_status.started()
                                task_status_sent = True
                        case ConnectionTerminated():
                            if not task_status_sent:
                                raise BrokenResourceError
                            else:
                                break

    async def send_stream_data(self, data: bytes | memoryview, stream_id: int) -> None:
        with memoryview(data) as bytes_to_send:
            while bytes_to_send:
                if not self._connection.outbound_flow_control_window:
                    self._can_send_event = Event()
                    await self._can_send_event.wait()
                    continue

                chunk_size = min(
                    self._connection.local_flow_control_window(stream_id),
                    self._connection.max_outbound_frame_size,
                    len(bytes_to_send),
                )
                chunk, bytes_to_send = (
                    bytes_to_send[:chunk_size],
                    bytes_to_send[chunk_size:],
                )
                self._connection.send_data(stream_id, chunk)
                await self._flush_outgoing_data()

    async def send_stream_eof(self, stream_id: int) -> None:
        state = self._connection.streams[stream_id].state_machine.state
        if state in (StreamState.HALF_CLOSED_REMOTE, StreamState.OPEN):
            self._connection.end_stream(stream_id)
            await self._flush_outgoing_data()

    async def send_stream_reset(self, stream_id: int, error_code: int = 0) -> None:
        state = self._connection.streams[stream_id].state_machine.state
        if state in (StreamState.HALF_CLOSED_REMOTE, StreamState.OPEN):
            self._connection.reset_stream(stream_id, error_code)
            await self._flush_outgoing_data()

    async def close_stream(self, stream_id: int) -> None:
        state = self._connection.streams[stream_id].state_machine.state
        if state not in (StreamState.HALF_CLOSED_LOCAL, StreamState.CLOSED):
            send_stream = self._streams.pop(stream_id)
            try:
                self._connection.reset_stream(stream_id)
                await self._flush_outgoing_data()
            finally:
                send_stream.close()

    async def _flush_outgoing_data(self) -> None:
        async with self._io_lock:
            if data := self._connection.data_to_send():
                await self.transport_stream.send(data)

    @override
    async def request(
        self,
        method: str,
        target: URL,
        headers: CIMultiDict,
        body: ByteReceiveStream | None = None,
        protocol: str | None = None,
        end_stream: bool = True,
    ) -> HTTP2ResponseStream:
        # Add the pseudo-headers to the beginning of the headers
        headers_list = [
            (":method", method),
            (":path", target.raw_path_qs),
            (":authority", self.host),
            (":scheme", self.scheme),
        ]
        if protocol:
            headers_list += [(":protocol", protocol)]

        # Add user-supplied headers
        headers_list += list(headers.items())

        # Allocate a new stream ID and create a matching memory object stream
        stream_id = self._connection.get_next_available_stream_id()
        self._streams[stream_id], receive_stream = create_memory_object_stream[H2Event](
            1
        )

        # Send the headers
        self._connection.send_headers(
            stream_id, headers_list, end_stream=end_stream and body is None
        )

        # If a body was passed, send it
        if body is not None:
            if content_length := headers.get("content-length", None):
                bytes_to_read: int | None = int(content_length)
            else:
                bytes_to_read = None

            while bytes_to_read is None or bytes_to_read > 0:
                try:
                    chunk = await body.receive(min(bytes_to_read or 65536, 65536))
                except EndOfStream:
                    break

                await self.send_stream_data(chunk, stream_id)
                if bytes_to_read is not None:
                    bytes_to_read -= len(chunk)

            self._connection.end_stream(stream_id)

        await self._flush_outgoing_data()

        # Wait for the response to begin
        event = await receive_stream.receive()
        assert isinstance(event, ResponseReceived)

        # Create and return a pseudo-stream
        h2_stream = self._connection.streams[stream_id]
        return HTTP2ResponseStream(h2_stream, event.headers or [], receive_stream, self)

    async def connect(
        self,
        target: URL,
        *,
        headers: CIMultiDict,
        protocol: str | None = None,
    ) -> HTTP2ResponseStream:
        request_stream = await self.request(
            "CONNECT", target, headers=headers, protocol=protocol, end_stream=False
        )
        status_code = request_stream.extra(HTTPResponseAttribute.status_code)
        if status_code != 200:
            body = b"".join([chunk async for chunk in request_stream]) or None
            raise HTTPStatusError(
                status_code, request_stream.extra(HTTPResponseAttribute.headers), body
            )

        return request_stream

    async def connect_ws(
        self,
        target: URL,
        headers: CIMultiDict,
        subprotocols: Sequence[str],
        proposed_extensions: Sequence[Extension],
    ) -> ObjectStream[bytes | str]:
        headers["sec-websocket-version"] = "13"
        headers["origin"] = f"{self.scheme}://{self.host}{target}"

        if subprotocols:
            headers["sec-websocket-protocol"] = ", ".join(subprotocols)

        if proposed_extensions:
            headers["sec-websocket-extensions"] = ", ".join(
                ext.name for ext in proposed_extensions
            )

        request_stream = await self.connect(
            target, headers=headers, protocol="websocket"
        )
        subprotocol = request_stream.headers.get("sec-websocket-protocol")
        return WebSocketConnection(request_stream, proposed_extensions, subprotocol)


class HTTP2ResponseStream(ByteStream):
    """Low-level HTTP/2 response stream."""

    def __init__(
        self,
        h2_stream: H2Stream,
        headers: list[HeaderTuple],
        receive_stream: MemoryObjectReceiveStream[H2Event],
        conn: HTTP2Connection,
    ) -> None:
        self.h2_stream = h2_stream
        self._stream_id = h2_stream.stream_id
        self._raw_headers = headers
        self._receive_stream = receive_stream
        self._conn = conn
        self._buffer = b""
        self._error_code: int | None = None
        self._closed = False

        # The HTTP/2 spec says that pseudo-headers must come first, and :status is
        # mandatory in responses
        key, value = headers.pop(0)
        assert key == b":status"
        self._status_code = int(value)

    @cached_property
    def headers(self) -> CIMultiDictProxy:
        parsed_headers = CIMultiDict(
            [
                (key.decode("ascii"), value.decode("ascii"))
                for key, value in self._raw_headers
            ]
        )
        del self._raw_headers
        return CIMultiDictProxy(parsed_headers)

    def _check_error_code(self) -> None:
        if self._error_code is not None:
            raise BrokenResourceError(
                f"server closed the stream (error code: {self._error_code})"
            )

    @override
    async def receive(self, max_bytes: int = 65536) -> bytes:
        self._check_error_code()

        if self._buffer:
            await checkpoint_if_cancelled()
            data, self._buffer = self._buffer[:max_bytes], self._buffer[max_bytes:]
            return data

        while True:
            event = await self._receive_stream.receive()
            match event:
                case DataReceived():
                    # HTTP2Connection will never send us an empty data frame
                    data = cast(bytes, event.data)
                    if len(data) > max_bytes:
                        self._buffer = data[max_bytes:]
                        return data[:max_bytes]
                    else:
                        return data
                case StreamReset():
                    self._closed = True
                    self._error_code = event.error_code
                    self._receive_stream.close()
                    raise BrokenResourceError(
                        f"server closed the stream (error code: {self._error_code})"
                    )
                case StreamEnded():
                    self._closed = True
                    self._receive_stream.close()
                    raise EndOfStream
                case _:  # pragma: no cover
                    raise RuntimeError(f"unexpected event: {event}")

    @override
    async def send(self, item: bytes) -> None:
        self._check_error_code()
        await self._conn.send_stream_data(item, self._stream_id)

    @override
    async def send_eof(self) -> None:
        await self._conn.send_stream_eof(self._stream_id)

    @override
    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            self._receive_stream.close()
            await self._conn.close_stream(self._stream_id)

    @override
    @property
    def extra_attributes(self) -> Mapping[Any, Callable[[], Any]]:
        return {
            **self._conn.transport_stream.extra_attributes,
            HTTPResponseAttribute.http_version: lambda: "2",
            HTTPResponseAttribute.status_code: lambda: self._status_code,
            HTTPResponseAttribute.headers: lambda: self.headers,
        }

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} stream_id={self._stream_id} "
            f"state={self.h2_stream.state_machine.state}>"
        )

    def __del__(self) -> None:
        if not self._closed:
            warn(f"unclosed {self!r}", ResourceWarning, stacklevel=1, source=self)
