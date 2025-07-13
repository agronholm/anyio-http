from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, final

from anyio import TypedAttributeSet, typed_attribute
from anyio.abc import ByteReceiveStream
from multidict import CIMultiDictProxy

from anyio_http._exceptions import HTTPStatusError

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

if sys.version_info >= (3, 11):
    from typing import Self
else:
    from typing_extensions import Self


class HTTPResponseAttribute(TypedAttributeSet):
    """Contains attributes specific to HTTP responses."""

    #: HTTP status code
    status_code: int = typed_attribute()
    #: HTTP version ("1.1", "2", etc.)
    http_version: str = typed_attribute()
    #: HTTP response headers
    headers: CIMultiDictProxy = typed_attribute()


@dataclass(eq=False)
class HTTPResponse(ByteReceiveStream):
    _request_stream: ByteReceiveStream
    _body: bytes | None = field(init=False, default=None)
    _text: str | None = field(init=False, default=None)

    @property
    @final
    def http_version(self) -> str:
        return self.extra(HTTPResponseAttribute.http_version)

    @property
    @final
    def status_code(self) -> int:
        return self.extra(HTTPResponseAttribute.status_code)

    @property
    def content_type(self) -> str | None:
        """
        The value of the content-type header, without any parameters like ``charset``.
        """
        if value := self.headers.getone("content-type", None):
            return value.split(";")[0].strip()

        return None

    @property
    @final
    def headers(self) -> CIMultiDictProxy:
        return self.extra(HTTPResponseAttribute.headers)

    @final
    async def read_response_body(self) -> None:
        if self._body is None:
            body = b""
            async for chunk in self:
                body += chunk

            self._body = body

    @property
    @final
    def body(self) -> bytes:
        """
        The response body as a raw bytestring.

        Requires that the response body has been read already.

        :raises RuntimeError: if the response body has not been read yet

        """
        if self._body is None:
            raise RuntimeError("response body not read yet")

        return self._body

    @property
    @final
    def text(self) -> str:
        """
        The response body as a Unicode string.

        Requires that the response body has been read already.

        :raises RuntimeError: if the response body has not been read yet

        """
        if self._text is None:
            encoding = "utf-8"
            if content_type := self.headers.getone("content-type", None):
                parts = content_type.split(";")
                for part in parts:
                    if part.startswith("charset="):
                        encoding = part.split("=", 1)[1].strip(
                            '"',
                        )
                        break

            self._text = self.body.decode(encoding, errors="replace")

        return self._text

    @final
    def raise_for_status(self) -> Self:
        """
        If the response contains a status code lower than 400, raise an exception.

        :return: the response itself
        :raises HTTPStatusError: if the response contains a status code of 400 or higher

        """
        if self.status_code < 400:
            return self

        raise HTTPStatusError(self.status_code, self.headers, self._body)

    @override
    async def receive(self, max_bytes: int = 65536) -> bytes:
        return await self._request_stream.receive(max_bytes)

    @override
    async def aclose(self) -> None:
        await self._request_stream.aclose()

    @property
    def extra_attributes(self) -> Mapping[Any, Callable[[], Any]]:
        return self._request_stream.extra_attributes

    def __repr__(self) -> str:
        return (
            f"<HTTPResponse http_version={self.http_version!r} "
            f"status_code={self.status_code}>"
        )
