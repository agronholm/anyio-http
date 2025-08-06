from __future__ import annotations

import sys
from collections import deque
from collections.abc import AsyncGenerator, Generator, Mapping, Sequence
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version
from ssl import SSLContext, create_default_context
from typing import Any, TypeAlias
from urllib.parse import unquote

from anyio import (
    AsyncContextManagerMixin,
    as_connectable,
    create_task_group,
    current_time,
)
from anyio.abc import (
    ByteReceiveStream,
    ByteStream,
    ByteStreamConnectable,
    ObjectStream,
    TaskGroup,
)
from anyio.streams.buffered import BufferedByteReceiveStream
from anyio.streams.tls import TLSAttribute
from multidict import CIMultiDict
from wsproto.extensions import Extension
from yarl import URL

from ._asgi import ASGIApp, ASGIConnection
from ._auth import Authenticator, BasicAuth
from ._connection import HTTPConnection
from ._cookies import Cookie
from ._decoders import ContentDecoder
from ._http2 import HTTP2Connection
from ._http11 import HTTP11Connection
from ._response import HTTPResponse
from ._sse import ServerEventStream
from ._streams import StaticStream
from ._wrappers import ContentWrapper

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

if sys.version_info >= (3, 11):
    from typing import Self
else:
    from typing_extensions import Self

PoolKey: TypeAlias = tuple[str, str, int | None]

_default_user_agent = "fetchling"
try:
    _default_user_agent += f"/{version('fetchling')}"
except PackageNotFoundError:
    pass


def url_to_connectable(
    scheme: str, host: str, port: int | None, ssl_context: SSLContext
) -> ByteStreamConnectable:
    remote: str | tuple[str, int]
    scheme, _, extra = scheme.partition("+")
    if extra == "unix":
        remote = unquote(host)
    else:
        if port:
            port = port
        elif scheme == "https":
            port = 443
        elif scheme == "http":
            port = 80
        else:
            raise ValueError(f"unsupported scheme: {scheme}")

        remote = (host, port)

    return as_connectable(
        remote,
        tls=scheme == "https",
        ssl_context=ssl_context,
        tls_hostname=host,
    )


class ConnectionPool:
    def __init__(
        self,
        app_or_connectable: ByteStreamConnectable | ASGIApp,
        scheme: str,
        server_hostname: str | None,
        task_group: TaskGroup,
    ) -> None:
        self.app_or_connectable = app_or_connectable
        self._scheme = scheme
        self._server_hostname = server_hostname
        self._task_group = task_group
        self._connections = deque[HTTPConnection]()

    @asynccontextmanager
    async def acquire(self) -> AsyncGenerator[HTTPConnection]:
        for conn in self._connections:
            if conn.available_slots:
                break
        else:
            if isinstance(self.app_or_connectable, ByteStreamConnectable):
                assert self._server_hostname is not None
                stream = await self.app_or_connectable.connect()
                if stream.extra(TLSAttribute.alpn_protocol, "") == "h2":
                    conn = HTTP2Connection(stream, self._scheme, self._server_hostname)
                    await self._task_group.start(conn.manage)
                else:
                    conn = HTTP11Connection(stream, self._scheme, self._server_hostname)
            else:
                conn = ASGIConnection(self.app_or_connectable)
                await self._task_group.start(conn.manage)

            assert conn.available_slots
            self._connections.appendleft(conn)

        try:
            yield conn
        finally:
            if conn.closed:
                self._connections.remove(conn)

    async def aclose(self) -> None:
        async with create_task_group() as tg:
            for connection in self._connections:
                tg.start_soon(connection.aclose)

            self._connections.clear()

    def __len__(self) -> int:
        return len(self._connections)


class HTTPRequest(AsyncContextManagerMixin):
    def __init__(
        self,
        pool: ConnectionPool,
        method: str,
        url: URL,
        headers: CIMultiDict[str],
        body: ByteReceiveStream | None = None,
    ) -> None:
        self._pool = pool
        self._method = method
        self._url = url
        self._headers = headers
        self._body = body

    @asynccontextmanager
    async def __asynccontextmanager__(self) -> AsyncGenerator[HTTPResponse]:
        # Acquire a connection from the pool, send the request and wait for the response
        # headers
        async with self._pool.acquire() as conn:
            stream = await conn.request(
                self._method,
                self._url,
                self._headers,
                self._body,
            )

            async with HTTPResponse(stream) as response:
                # TODO: store any cookies from the headers
                yield response

    async def _execute(self) -> HTTPResponse:
        async with self as response:
            await response.read_response_body()
            return response

    def __await__(self) -> Generator[Any, Any, HTTPResponse]:
        return (yield from self._execute().__await__())


class HTTPClient(AsyncContextManagerMixin):
    _task_group: TaskGroup
    _cookies: list[Cookie]

    def __init__(
        self,
        *,
        base_url: URL | str | None = None,
        app_or_connectable: ASGIApp | ByteStreamConnectable | None = None,
        auth: Authenticator | None = None,
        timeout: float | None = None,
        headers: Mapping[str, str] | None = None,
        cookies: Mapping[str, str] | Sequence[Cookie] | None = None,
        ssl_context: SSLContext | None = None,
        follow_redirects: bool = True,
        use_http2: bool = True,
        max_redirects: int = 20,
        content_decoders: Sequence[ContentDecoder] = (),
    ):
        self.base_url = URL(base_url) if base_url else None
        self.app_or_connectable = app_or_connectable
        self.auth: Authenticator | None = auth
        self.timeout = timeout
        self.ssl_context: SSLContext = ssl_context or create_default_context()
        self.follow_redirects = follow_redirects
        self.max_redirects = max_redirects
        self.headers: CIMultiDict[str] = CIMultiDict(headers if headers else ())
        self.headers.setdefault("user-agent", _default_user_agent)
        self._is_proxy = False

        # Unless use_http2 is False, add "h2" to the start of ALPN protocols to enable
        # HTTP/2
        alpn_protocols = ["http/1.1"]
        if use_http2:
            alpn_protocols.insert(0, "h2")

        self.ssl_context.set_alpn_protocols(alpn_protocols)

        # If the base URL contains a username or password, extract that information
        # into a BasicAuth authenticator and clear these fields in the base URL
        if self.base_url is not None:
            if self.base_url.user is not None or self.base_url.password is not None:
                self.auth = BasicAuth(
                    self.base_url.raw_user or "", self.base_url.raw_password or ""
                )
                self.base_url = self.base_url.with_user(None).with_password(None)

        # Convert any passed cookies mapping or sequence into a list of Cookie instances
        if isinstance(cookies, Mapping):
            self._cookies = [Cookie(name, value) for name, value in cookies.items()]
        elif isinstance(cookies, Sequence):
            self._cookies = list(cookies)
        else:
            self._cookies = []

        # If the base headers contain cookies, parse them into Cookie instances
        for cookie_header in self.headers.popall("cookie", ()):
            self._cookies.append(Cookie.parse(cookie_header))

        # self._content_decoders = {
        #     decoder.name: decoder
        #     for decoder in chain(registered_decoders.values(), content_decoders)
        # }
        # if decoder_names := ", ".join(self._content_decoders):
        #     self.headers.add("accept-encoding", decoder_names)

        self._connection_pools: dict[PoolKey | None, ConnectionPool] = {}

    @override
    @asynccontextmanager
    async def __asynccontextmanager__(self) -> AsyncGenerator[Self]:
        async with create_task_group() as self._task_group:
            try:
                yield self
            finally:
                for pool in self._connection_pools.values():
                    self._task_group.start_soon(pool.aclose)

                self._connection_pools.clear()

    def _prepare_request(
        self,
        url: URL | str,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[URL, CIMultiDict, ConnectionPool]:
        urlobj = URL(url)

        # If a base URL was provided, concatenate the provided one to the base
        if self.base_url is not None:
            urlobj = self.base_url.join(urlobj)

        # Add any query parameters
        if params is not None:
            urlobj = urlobj.extend_query(params)

        pool_key: PoolKey | None = None
        connectable: ByteStreamConnectable | ASGIApp | None = self.app_or_connectable
        if connectable is None:
            if not urlobj.host:
                raise ValueError("the URL must have the host component in it")
            elif not urlobj.scheme:
                raise ValueError("the URL must have the scheme component in it")

            # Get the connection pool based on the host and port in the URL
            pool_key = urlobj.scheme, urlobj.host, urlobj.port
            connectable = url_to_connectable(*pool_key, self.ssl_context)

        if not (pool := self._connection_pools.get(pool_key)):
            pool = ConnectionPool(
                connectable, urlobj.scheme, urlobj.host, self._task_group
            )
            self._connection_pools[pool_key] = pool

        # Lose the scheme and host parts of the URL
        if not self._is_proxy and urlobj.is_absolute():
            urlobj = urlobj.relative()

        # Join the base headers with any request-specific headers
        joined_headers = (
            CIMultiDict(self.headers, **headers) if headers else self.headers
        )

        # Add any applicable cookies
        now = current_time()
        cookies = [
            str(cookie)
            for cookie in self._cookies
            if cookie.is_valid_for(urlobj) and not cookie.has_expired(now)
        ]
        if cookie_header := joined_headers.get("cookie"):
            cookies.insert(0, cookie_header)

        if cookies:
            joined_headers["cookie"] = "; ".join(cookies)

        return urlobj, joined_headers, pool

    def request(
        self,
        method: str,
        url: URL | str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
        body: str
        | bytes
        | memoryview
        | ByteReceiveStream
        | ContentWrapper
        | None = None,
    ) -> HTTPRequest:
        urlobj, ci_headers, pool = self._prepare_request(url, params, headers)
        body_stream: ByteReceiveStream | None
        match body:
            case None:
                body_stream = None
            case bytes() | memoryview():
                body_stream = BufferedByteReceiveStream(StaticStream(body))
                ci_headers.setdefault("content-type", "application/octet-stream")
                ci_headers["content-length"] = str(len(body))
            case str():
                body_bytes = body.encode("utf-8")
                body_stream = BufferedByteReceiveStream(StaticStream(body_bytes))
                ci_headers.setdefault("content-type", "text/plain; charset=utf-8")
                ci_headers["content-length"] = str(len(body_bytes))
            case ByteReceiveStream():
                body_stream = body
            case ContentWrapper():
                ci_headers.setdefault("content-type", body.content_type)
                body = body.to_body()
                if isinstance(body, bytes):
                    ci_headers["content-length"] = str(len(body))
                    body_stream = BufferedByteReceiveStream(StaticStream(body))
                else:
                    body_stream = body
            case _:
                raise TypeError(f"unsupported body type: {body.__class__.__qualname__}")

        del body
        return HTTPRequest(pool, method, urlobj, ci_headers, body_stream)

    def head(
        self,
        url: URL | str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
    ) -> HTTPRequest:
        """
        Send a HEAD request.

        :param url: the target URL
        :param headers: HTTP headers to add to the request
        :param params: Query parameters (``?x=y&abc=123``) to add to the URL
        :return: the HTTP response

        """
        return self.request("HEAD", url, headers=headers, params=params)

    def get(
        self,
        url: URL | str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
    ) -> HTTPRequest:
        """
        Send a GET request.

        :param url: the target URL
        :param headers: HTTP headers to add to the request
        :param params: Query parameters (``?x=y&abc=123``) to add to the URL
        :return: the HTTP response

        """
        return self.request("GET", url, headers=headers, params=params)

    def post(
        self,
        url: URL | str,
        body: str
        | bytes
        | memoryview
        | ByteReceiveStream
        | ContentWrapper
        | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
    ) -> HTTPRequest:
        """
        Send a POST request.

        :param url: the target URL
        :param body: the request body to send
        :param headers: HTTP headers to add to the request
        :param params: Query parameters (``?x=y&abc=123``) to add to the URL
        :return: the HTTP response

        """
        return self.request(
            "POST",
            url,
            headers=headers,
            params=params,
            body=body,
        )

    def put(
        self,
        url: URL | str,
        body: str | bytes | memoryview | ByteReceiveStream | ContentWrapper,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
    ) -> HTTPRequest:
        """
        Send a PUT request.

        :param url: the target URL
        :param body: the request body to send
        :param headers: HTTP headers to add to the request
        :param params: Query parameters (``?x=y&abc=123``) to add to the URL
        :return: the HTTP response

        """
        return self.request("PUT", url, headers=headers, params=params, body=body)

    def patch(
        self,
        url: URL | str,
        body: str | bytes | memoryview | ByteReceiveStream | ContentWrapper,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
    ) -> HTTPRequest:
        """
        Send a PATCH request.

        :param url: the target URL
        :param body: the request body to send
        :param headers: HTTP headers to add to the request
        :param params: Query parameters (``?x=y&abc=123``) to add to the URL
        :return: the HTTP response

        """
        return self.request("PATCH", url, headers=headers, params=params, body=body)

    def delete(
        self,
        url: URL | str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
    ) -> HTTPRequest:
        """
        Send a DELETE request.

        :param url: the target URL
        :param headers: HTTP headers to add to the request
        :param params: Query parameters (``?x=y&abc=123``) to add to the URL
        :return: the HTTP response

        """
        return self.request("DELETE", url, headers=headers, params=params)

    @asynccontextmanager
    async def connect(
        self,
        url: URL | str,
        host: str,
        port: int,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> AsyncGenerator[ByteStream]:
        urlobj, headers_dict, pool = self._prepare_request(url, None, headers)

        # Acquire a connection from the pool, send the request and wait for a response
        async with pool.acquire() as conn:
            async with await conn.connect(
                URL.build(host=host, port=port), headers=headers_dict
            ) as stream:
                yield stream

    @asynccontextmanager
    async def connect_ws(
        self,
        url: URL | str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
        extensions: Sequence[Extension] = (),
        subprotocols: Sequence[str] = (),
    ) -> AsyncGenerator[ObjectStream[bytes | str]]:
        urlobj, headers, pool = self._prepare_request(url, params, headers)

        # Acquire a connection from the pool and do the WebSockets handshake
        async with pool.acquire() as conn:
            async with await conn.connect_ws(
                URL.build(path=urlobj.path, query=urlobj.query),
                headers,
                subprotocols,
                list(extensions),
            ) as ws_connection:
                yield ws_connection

    @asynccontextmanager
    async def connect_sse(
        self,
        url: URL | str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
    ) -> AsyncGenerator[ServerEventStream]:
        urlobj, headers, pool = self._prepare_request(url, params, headers)
        headers["accept"] = "text/event-stream"

        # Make a GET request to the provided URL and wrap the response stream,
        # parsing the incoming data as Server-Sent Events
        async with (
            HTTPRequest(pool, "GET", urlobj, headers) as response,
            ServerEventStream(response) as stream,
        ):
            yield stream
