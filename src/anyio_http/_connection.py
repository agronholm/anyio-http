from __future__ import annotations

from abc import abstractmethod
from collections.abc import Sequence

from anyio.abc import AsyncResource, ByteReceiveStream, ByteStream, ObjectStream
from multidict import CIMultiDict
from wsproto.extensions import Extension
from yarl import URL


class HTTPConnection(AsyncResource):
    """Wrapper for a single transport connection to an HTTP server."""

    # max_slots: int = 1
    #
    # @property
    # @abstractmethod
    # def used_slots(self) -> int:
    #     """
    #     The number of concurrent requests that this connection is currently
    #     processing.
    #     """

    @property
    @abstractmethod
    def available_slots(self) -> int:
        """
        The number of concurrent requests that this connection can still accommodate.
        """

    @property
    @abstractmethod
    def closed(self) -> bool:
        """
        ``True`` if the connection is closed and thus cannot be used for further
        requests.
        """

    @abstractmethod
    async def request(
        self,
        method: str,
        target: URL,
        headers: CIMultiDict,
        body: ByteReceiveStream | None = None,
    ) -> ByteReceiveStream:
        """
        Send an HTTP request and return the response.

        If the response includes a body, it must be read before the next request.

        :param method: the HTTP method to use
        :param target: the path to request, including any query string
        :param headers: the headers to pass with the request
        :param body:
        :return: an HTTP response stream
        """

    @abstractmethod
    async def connect(self, target: URL, *, headers: CIMultiDict) -> ByteStream:
        """
        Establish a tunnel to a remote host via the HTTP server using the CONNECT
        method.

        :param target: the host:port pair to connect to
        :param headers: the headers to pass with the request
        :return: a bytestream connected to the remote host
        """

    @abstractmethod
    async def connect_ws(
        self,
        target: URL,
        headers: CIMultiDict,
        subprotocols: Sequence[str],
        proposed_extensions: Sequence[Extension],
    ) -> ObjectStream[bytes | str]:
        """
        Establish a WebSocket connection to the given endpoint.

        :param target: the HTTP endpoint path to connect to
        :param headers: the headers to pass with the request
        :param subprotocols: the WebSocket subprotocols to allow
        :param proposed_extensions: the WebSocket extensions to use
        :return: a WebSocket connection
        """
