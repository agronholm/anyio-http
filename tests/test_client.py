from __future__ import annotations

import json
import pathlib
from collections.abc import AsyncGenerator, Awaitable, Callable
from functools import partial
from ssl import SSLContext
from typing import Any, Protocol

import anyio
import cbor2
import pytest
import sniffio
from _pytest.fixtures import SubRequest
from anyio import EndOfStream, Event, create_task_group
from anyio.abc import ByteReceiveStream, SocketAttribute
from anyio.lowlevel import checkpoint
from hypercorn import Config
from sse_starlette import EventSourceResponse
from starlette.applications import Starlette
from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket
from yarl import URL

from anyio_http import (
    CBOR,
    JSON,
    ContentWrapper,
    Form,
    FormField,
    HTTPClient,
    HTTPStatusError,
    ServerEvent,
)

pytestmark = pytest.mark.anyio


async def get_endpoint(request: Request) -> Response:
    fields = {
        "item": request.path_params["item"],
        "query": dict(request.query_params.items()),
    }
    return JSONResponse(fields)


async def post_endpoint(request: Request) -> Response:
    fields: dict[str, Any]
    match request.headers["content-type"]:
        case "application/json":
            fields = await request.json()
        case "application/cbor":
            fields = cbor2.loads(await request.body())
        case _:
            assert request.headers["content-type"].split(";", 1)[0] in (
                "application/x-www-form-urlencoded",
                "multipart/form-data",
            )
            form = await request.form()
            fields = dict(form.items())

    for key, value in fields.items():
        if isinstance(value, UploadFile):
            content = await value.read()
            fields[key] = {
                "content_type": value.content_type,
                "filename": value.filename,
                "size": value.size,
                "content": content.decode("utf-8", errors="backslashreplace"),
            }

    return JSONResponse(fields)


async def patch_endpoint(request: Request) -> Response:
    body = await request.body()
    return PlainTextResponse(content=body)


async def ws_endpoint(websocket: WebSocket) -> None:
    match websocket.query_params.get("reject"):
        case "denial":
            await websocket.send_denial_response(
                PlainTextResponse(content="Rejected", status_code=403)
            )
            return
        case "close":
            await websocket.close(1008, "Policy violation")
            return

    await websocket.accept()
    while True:
        message_dict = await websocket.receive()
        if text := message_dict.get("text"):
            await websocket.send_text(text[::-1])
        elif bytes_ := message_dict.get("bytes"):
            await websocket.send_bytes(bytes_[::-1])
        else:
            break


async def sse_endpoint(request: Request) -> EventSourceResponse:
    async def generate_events() -> AsyncGenerator[dict[str, str]]:
        yield {"event": "first", "data": repr(dict(request.query_params))}
        await checkpoint()
        yield {"data": "Second event", "id": "2"}
        await checkpoint()
        yield {"event": "third", "id": "3"}
        await checkpoint()

    return EventSourceResponse(generate_events())


app = Starlette(
    routes=[
        Route("/get/{item}", get_endpoint, methods=["GET"]),
        Route("/form", post_endpoint, methods=["POST"]),
        Route("/patch", patch_endpoint, methods=["PATCH"]),
        Route("/events", sse_endpoint),
        WebSocketRoute("/ws", ws_endpoint),
    ]
)


@pytest.fixture(scope="module", params=["http2", "http11", "http11+tls", "asgi"])
def transport(request: SubRequest) -> str:
    return request.param


@pytest.fixture(scope="module")
def use_tls(transport: str) -> bool:
    return transport in ("http2", "http11+tls")


class ASGIServer(Protocol):
    def __call__(
        self,
        app: Callable[[Any, Any, Any], Awaitable[Any]],
        config: Config,
        *,
        shutdown_trigger: Callable[..., Awaitable[None]] | None = ...,
    ) -> Any: ...


@pytest.fixture(scope="module")
async def base_url(
    free_tcp_port_factory: Callable[[], int],
    transport: str,
    use_tls: bool,
    ca_path: pathlib.Path,
    server_cert_path: pathlib.Path,
) -> AsyncGenerator[URL]:
    if transport == "asgi":
        yield URL.build(path="/")
        return

    serve: ASGIServer
    if sniffio.current_async_library() == "trio":
        from hypercorn.trio import serve
    else:
        from hypercorn.asyncio import serve

    free_tcp_port = free_tcp_port_factory()
    config = Config()
    config.bind = [f"localhost:{free_tcp_port}"]
    config.loglevel = "DEBUG"
    if use_tls:
        config.ca_certs = str(ca_path)
        config.certfile = str(server_cert_path)
        config.keyfile = str(server_cert_path)
        if transport != "http2":
            config.alpn_protocols.remove("h2")

    async with create_task_group() as tg:
        shutdown_event = Event()
        tg.start_soon(lambda: serve(app, config, shutdown_trigger=shutdown_event.wait))
        await checkpoint()
        yield URL.build(
            scheme="https" if use_tls else "http", host="localhost", port=free_tcp_port
        )
        shutdown_event.set()


# @pytest.fixture(scope="module")
# def proxy_url(
#     free_tcp_port_factory: Callable[[], int],
#     use_tls: bool,
#     ca_path: pathlib.Path,
#     server_cert_path: pathlib.Path,
# ) -> Generator[URL]:
#     free_tcp_port = free_tcp_port_factory()
#     extra_args = ["--certs", str(server_cert_path)] if use_tls else []
#     with Popen(
#         ["mitmproxy", "--listen-port", str(free_tcp_port), *extra_args]
#     ) as process:
#         # with proxy.Proxy(port=0) as p:
#         for _ in range(20):
#             try:
#                 with socket.create_connection(("localhost", free_tcp_port)):
#                     break
#             except ConnectionRefusedError:
#                 time.sleep(0.1)
#         else:
#             pytest.fail("mitmproxy did not start")
#
#         yield URL.build(scheme="http", host="localhost", port=free_tcp_port)
#         process.terminate()


@pytest.fixture
def http_client(
    base_url: URL, transport: str, client_ssl_context: SSLContext
) -> HTTPClient:
    return HTTPClient(
        base_url=base_url,
        app_or_connectable=app if transport == "asgi" else None,
        ssl_context=client_ssl_context,
    )


@pytest.mark.parametrize(
    "params",
    [
        pytest.param(None, id="none"),
        pytest.param({"hello": "åäö", "world": "ddd"}, id="params"),
    ],
)
async def test_get(
    params: dict[str, str] | None,
    http_client: HTTPClient,
) -> None:
    async with http_client:
        response = await http_client.get("/get/Hellö, world!", params=params)
        assert response.status_code == 200
        assert json.loads(response.text) == {
            "item": "Hellö, world!",
            "query": params or {},
        }


async def test_get_sequential(http_client: HTTPClient, transport: str) -> None:
    async with http_client:
        expected_local_port: int | None = None
        for i in range(10):
            response = await http_client.get(
                "/get/Hellö, world!", params={"index": str(i)}
            )
            assert response.status_code == 200
            assert json.loads(response.text) == {
                "item": "Hellö, world!",
                "query": {"index": str(i)},
            }

            # Check that the requests all used the same connection
            if transport != "asgi":
                local_port = response.extra(SocketAttribute.local_port)
                if expected_local_port is None:
                    expected_local_port = local_port
                else:
                    assert local_port == expected_local_port


@pytest.mark.parametrize(
    "wrapper",
    [
        pytest.param(JSON, id="json"),
        pytest.param(CBOR, id="cbor"),
        pytest.param(Form, id="form"),
        pytest.param(partial(Form, force_multipart=True), id="multipart"),
    ],
)
async def test_post_simple_form(
    wrapper: Callable[[Any], ContentWrapper],
    http_client: HTTPClient,
) -> None:
    async with http_client:
        response = await http_client.post("/form", wrapper({"teståäö": "valueåäö"}))
        assert response.status_code == 200
        assert response.text == '{"teståäö":"valueåäö"}'


async def test_post_complex_multipart_form(
    http_client: HTTPClient,
    tmp_path: pathlib.Path,
) -> None:
    class CustomStream(ByteReceiveStream):
        def __init__(self) -> None:
            self._buffer = b"static stream data"

        async def receive(self, max_bytes: int = 65536) -> bytes:
            data, self._buffer = self._buffer[:max_bytes], self._buffer[max_bytes:]
            if not data:
                raise EndOfStream

            return data

        async def aclose(self) -> None:
            pass

    path1 = tmp_path / "textfile.txt"
    path1.write_text("content1")
    path2 = anyio.Path(tmp_path / "binaryfile")
    await path2.write_bytes(b"\x00\xff\x00\xff")
    form = Form(
        {
            "textfield": "valueåäö",
            "binaryfield": b"\x06\x07\x08\x00",
            "text file": path1,
            "binary file": FormField(
                path2, filename="image.jpg", content_type="image/jpeg"
            ),
            "custom stream": FormField(CustomStream()),
        }
    )
    async with http_client:
        response = await http_client.post("/form", form, params={"hello": "world"})
        assert response.status_code == 200
        assert response.content_type == "application/json"
        response_dict = json.loads(response.text)
        assert response_dict == {
            "textfield": "valueåäö",
            "binaryfield": "\x06\x07\x08\x00",
            "text file": {
                "filename": "textfile.txt",
                "size": 8,
                "content_type": "text/plain",
                "content": "content1",
            },
            "binary file": {
                "filename": "image.jpg",
                "size": 4,
                "content_type": "image/jpeg",
                "content": "\x00\\xff\x00\\xff",
            },
            "custom stream": "static stream data",
        }


# async def test_connect(proxy_url: URL, client_ssl_context: SSLContext) -> None:
#     async with HTTPClient(base_url=proxy_url, ssl_context=client_ssl_context) as client:
#         with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_sock:
#             server_sock.settimeout(5)
#             server_sock.bind(("localhost", 0))
#             server_sock.listen()
#             host, port = server_sock.getsockname()
#             async with client.connect("", host, port) as tunnel:
#                 with server_sock.accept()[0] as sock:
#                     await tunnel.send(b"Hello, world!")
#                     assert sock.recv(1024) == b"Hello, world!"
#                     sock.sendall(b"!dlrow ,olleH")
#                     assert await tunnel.receive() == b"!dlrow ,olleH"


async def test_websocket(http_client: HTTPClient) -> None:
    async with http_client, http_client.connect_ws("/ws") as ws:
        await ws.send("Hello, world!")
        assert await ws.receive() == "!dlrow ,olleH"
        await ws.send(b"Hello, world!")
        assert await ws.receive() == b"!dlrow ,olleH"


@pytest.mark.parametrize("reject_method", ["close", "denial"])
async def test_websocket_rejected(http_client: HTTPClient, reject_method: str) -> None:
    async with http_client:
        with pytest.raises(HTTPStatusError) as exc_info:
            async with http_client.connect_ws("/ws", params={"reject": reject_method}):
                pass

    assert exc_info.value.status_code == 403
    if reject_method == "denial":
        assert exc_info.value.body == b"Rejected"
    else:
        assert not exc_info.value.body


async def test_sse(http_client: HTTPClient) -> None:
    events: list[ServerEvent] = []
    async with (
        http_client,
        http_client.connect_sse("/events", params={"hello": "world"}) as event_stream,
    ):
        async for event in event_stream:
            events.append(event)
            if len(events) > 3:
                pytest.fail("Received too many events")

    assert len(events) == 3
    assert events[0] == ServerEvent(event="first", data="{'hello': 'world'}")
    assert events[1] == ServerEvent(data="Second event", id="2")
    assert events[2] == ServerEvent(event="third", id="3")
