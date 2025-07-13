from __future__ import annotations

import sys
from collections.abc import (
    Awaitable,
    Callable,
    Iterable,
    Mapping,
    MutableMapping,
    Sequence,
)
from functools import cached_property
from typing import Any, NoReturn, TypeAlias, cast

from anyio import (
    BrokenResourceError,
    CancelScope,
    EndOfStream,
    Event,
    create_memory_object_stream,
    create_task_group,
)
from anyio.abc import (
    ByteReceiveStream,
    ObjectStream,
    TaskGroup,
    TaskStatus,
)
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from multidict import CIMultiDict, CIMultiDictProxy
from wsproto.extensions import Extension
from wsproto.frame_protocol import CloseReason
from yarl import URL

from ._connection import HTTPConnection
from ._exceptions import HTTPError, HTTPStatusError
from ._response import HTTPResponseAttribute
from ._websocket import (
    WebSocketAttribute,
    WebSocketConnectionClosed,
)

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

ASGIEvent: TypeAlias = MutableMapping[str, Any]
ASGIApp: TypeAlias = Callable[
    [
        MutableMapping[str, Any],
        Callable[[], Awaitable[ASGIEvent]],
        Callable[[ASGIEvent], Awaitable[None]],
    ],
    Awaitable[None],
]


class ASGIError(HTTPError):
    """Raised when connecting directly to an ASGI app and that app misbehaves."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message, status_code)


def parse_headers(raw_headers: Iterable[tuple[bytes, bytes]]) -> CIMultiDictProxy:
    parsed_headers = CIMultiDict(
        [(key.decode("ascii"), value.decode("utf-8")) for key, value in raw_headers]
    )
    return CIMultiDictProxy(parsed_headers)


class ASGIConnection(HTTPConnection):
    _task_group: TaskGroup

    def __init__(self, app: ASGIApp, max_connections: int = 100) -> None:
        self.app = app
        self.max_connections = max_connections
        self._active_requests = set[ASGIRequest]()
        self._stop_event = Event()
        self._closed = False

    @property
    def available_slots(self) -> int:
        if self._closed:
            return 0

        return self.max_connections - len(self._active_requests)

    async def aclose(self) -> None:
        self._closed = True
        self._stop_event.set()

    @property
    def closed(self) -> bool:
        return self._closed

    async def manage(self, *, task_status: TaskStatus[None]) -> None:
        # Runs the ASGI requests as tasks
        async with create_task_group() as self._task_group:
            task_status.started()
            await self._stop_event.wait()

    async def _create_asgi_request(
        self,
        type_: str,
        target: URL,
        headers: CIMultiDict[str],
        extra: Mapping[str, Any],
    ) -> ASGIRequest:
        encoded_headers: list[Any] = [
            [key.encode("ascii"), value.encode("utf-8")]
            for key, value in headers.items()
        ]
        scope: ASGIEvent = {
            "type": type_,
            "asgi": {"version": "3.0", "spec_version": "2.5"},
            "path": target.path,
            "query_string": target.raw_query_string.encode("ascii"),
            "headers": encoded_headers,
        }
        if extra:
            scope.update(extra)

        request = ASGIRequest(self.app, scope)
        self._active_requests.add(request)
        await request.start_app_in_task(self._task_group)
        return request

    def release_request(self, request: ASGIRequest) -> None:
        # Called from ASGIRequest.aclose()
        self._active_requests.remove(request)

    async def request(
        self,
        method: str,
        target: URL,
        headers: CIMultiDict,
        body: ByteReceiveStream | bytes | memoryview | None = None,
    ) -> ByteReceiveStream:
        request = await self._create_asgi_request(
            "http", target, headers, {"method": method}
        )
        try:
            match body:
                case ByteReceiveStream():
                    async for chunk in body:
                        await request.send_event_to_app(
                            type="http.request", body=chunk, more_body=True
                        )

                    await request.send_event_to_app(
                        type="http.request", more_body=False
                    )
                case bytes() | memoryview():
                    await request.send_event_to_app(
                        type="http.request", body=bytes(body)
                    )

            response_event = await request.receive_event_from_app()
            if response_event["type"] != "http.response.start":
                raise HTTPError(
                    f"unexpected ASGI event type: {response_event['type']!r}"
                )

            return ASGIResponseStream(request, response_event)
        except BaseException:
            await request.aclose()
            raise

    async def connect(self, target: URL, *, headers: CIMultiDict) -> NoReturn:
        raise NotImplementedError("ASGI does not support HTTP CONNECT")

    async def connect_ws(
        self,
        target: URL,
        headers: CIMultiDict,
        subprotocols: Sequence[str],
        proposed_extensions: Sequence[Extension],
    ) -> ObjectStream[bytes | str]:
        request = await self._create_asgi_request(
            "websocket",
            target,
            headers,
            {
                "subprotocols": list(subprotocols),
                "extensions": {"websocket.http.response": {}},
            },
        )
        try:
            try:
                await request.send_event_to_app(type="websocket.connect")
            except BrokenResourceError:
                # The app sent a response before accepting the connection
                pass

            response_event = await request.receive_event_from_app()
            match response_event.get("type"):
                case "websocket.accept":
                    return ASGIWebSocketStream(request, response_event)
                case "websocket.close":
                    raise HTTPStatusError(403)
                case "websocket.http.response.start":
                    body: bytes | None = None
                    while True:
                        try:
                            body_event = await request.receive_event_from_app()
                        except EndOfStream:
                            break

                        if body is None:
                            body = cast(bytes, body_event["body"])
                        else:
                            body += cast(bytes, body_event["body"])

                        if not body_event.get("more_body", False):
                            break

                    parsed_headers = parse_headers(
                        cast(Iterable[tuple[bytes, bytes]], response_event["headers"])
                    )
                    raise HTTPStatusError(
                        cast(int, response_event["status"]),
                        parsed_headers,
                        body,
                    )
                case type_:
                    raise HTTPError(
                        f"unexpected ASGI event type on WebSocket handshake: {type_!r}"
                    )
        except BaseException:
            await request.aclose()
            raise


class ASGIRequest:
    _client_receive: MemoryObjectReceiveStream[ASGIEvent]
    _client_send: MemoryObjectSendStream[ASGIEvent]

    def __init__(self, app: ASGIApp, scope: ASGIEvent) -> None:
        self.app = app
        self.scope = scope
        self._app_cancel_scope: CancelScope | None = None
        self._app_finished_event = Event()

    async def _run_app(self, *, task_status: TaskStatus[CancelScope]) -> None:
        with CancelScope() as scope:
            self._client_send, app_receive = create_memory_object_stream[ASGIEvent](1)
            app_send, self._client_receive = create_memory_object_stream[ASGIEvent](1)
            try:
                with app_receive, app_send:
                    task_status.started(scope)
                    await self.app(self.scope, app_receive.receive, app_send.send)
            finally:
                self._app_finished_event.set()

    async def start_app_in_task(self, task_group: TaskGroup) -> None:
        self._app_cancel_scope = await task_group.start(
            self._run_app,
            name=f"ASGI request (type={self.scope['type']!r} path={self.scope['path']!r})",
        )

    async def send_event_to_app(self, **kwargs: Any) -> None:
        await self._client_send.send(kwargs)

    async def receive_event_from_app(self) -> ASGIEvent:
        return await self._client_receive.receive()

    async def aclose(self) -> None:
        if self._app_cancel_scope is not None:
            self._app_cancel_scope.cancel()
            with CancelScope(shield=True):
                await self._app_finished_event.wait()

            self._client_send.close()
            self._client_receive.close()

    async def send_eof(self) -> None:
        raise NotImplementedError("half-closing is not supported for ASGI requests")


class BaseASGIStream:
    def __init__(self, request: ASGIRequest, response_event: ASGIEvent) -> None:
        self._request = request
        self._raw_headers = cast(
            Iterable[tuple[bytes, bytes]], response_event["headers"]
        )

    async def aclose(self) -> None:
        await self._request.aclose()

    @cached_property
    def headers(self) -> CIMultiDictProxy:
        parsed_headers = parse_headers(self._raw_headers)
        del self._raw_headers
        return parsed_headers

    @property
    def extra_attributes(self) -> Mapping[Any, Callable[[], Any]]:
        return {
            HTTPResponseAttribute.http_version: lambda: self._request.scope[
                "http_version"
            ],
            HTTPResponseAttribute.headers: lambda: self.headers,
        }


class ASGIResponseStream(BaseASGIStream, ByteReceiveStream):
    def __init__(self, request: ASGIRequest, response_event: ASGIEvent) -> None:
        super().__init__(request, response_event)
        self._status_code = cast(int, response_event["status"])

    @override
    async def receive(self, max_bytes: int = 65536) -> bytes:
        event = await self._request.receive_event_from_app()
        if event["type"] == "http.response.body":
            return cast(bytes, event["body"])
        else:
            raise BrokenResourceError(f"unexpected ASGI event type: {event['type']!r}")

    @override
    async def aclose(self) -> None:
        await self._request.aclose()

    @property
    def extra_attributes(self) -> Mapping[Any, Callable[[], Any]]:
        return {
            **super().extra_attributes,
            HTTPResponseAttribute.status_code: lambda: self._status_code,
        }


class ASGIWebSocketStream(BaseASGIStream, ObjectStream[bytes | str]):
    def __init__(self, request: ASGIRequest, response_event: ASGIEvent) -> None:
        super().__init__(request, response_event)
        self._subprotocol = cast(str | None, response_event.get("subprotocol"))

    @override
    async def receive(self) -> bytes | str:
        event = await self._request.receive_event_from_app()
        match event.get("type"):
            case "websocket.send":
                text = cast(str | None, event.get("text"))
                if text is not None:
                    return text

                bytes_ = cast(bytes | None, event.get("bytes"))
                if bytes_ is not None:
                    return bytes_

                raise BrokenResourceError(
                    f"got malformed websocket.send event: {event}"
                )
            case "websocket.close":
                reason = cast(str | None, event.get("reason"))
                if (code := event.get("code")) == 1000:
                    raise WebSocketConnectionClosed(reason)
                else:
                    raise BrokenResourceError(
                        f"server closed the connection (code: {code!r}, "
                        f"reason: {reason})"
                    )
            case type_:
                raise BrokenResourceError(f"unexpected ASGI event type: {type_!r}")

    @override
    async def send(self, item: bytes | str) -> None:
        if isinstance(item, str):
            await self._request.send_event_to_app(type="websocket.receive", text=item)
        else:
            await self._request.send_event_to_app(type="websocket.receive", bytes=item)

    @override
    async def send_eof(self) -> None:
        raise NotImplementedError("websockets don't support half-closing")

    @override
    async def aclose(
        self, code: CloseReason = CloseReason.NORMAL_CLOSURE, reason: str | None = None
    ) -> None:
        try:
            await self._request.send_event_to_app(
                type="websocket.disconnect", code=int(code), reason=reason
            )
        finally:
            await self._request.aclose()

    @property
    def extra_attributes(self) -> Mapping[Any, Callable[[], Any]]:
        return {
            **super().extra_attributes,
            WebSocketAttribute.subprotocol: lambda: self._subprotocol,
        }
