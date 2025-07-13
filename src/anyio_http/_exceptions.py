from __future__ import annotations

from multidict import CIMultiDict, CIMultiDictProxy


class HTTPError(Exception):
    """Base class for all HTTP-related errors."""


class HTTPStatusError(HTTPError):
    """
    Raised by :meth:`~anyio_http.HTTPRequest.raise_for_status` and a number of other
    ways when the server returns a non-successful status code.

    :ivar status_code: The HTTP status code
    :ivar headers: The HTTP response headers
    :ivar body: The HTTP response body (if any)
    """

    def __init__(
        self,
        status_code: int,
        headers: CIMultiDictProxy | None = None,
        body: bytes | None = None,
    ):
        super().__init__(status_code, body)
        self.status_code = status_code
        self.headers = (
            headers if headers is not None else CIMultiDictProxy(CIMultiDict())
        )
        self.body = body

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(status_code={self.status_code})"
