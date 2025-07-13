from __future__ import annotations

from dataclasses import dataclass

from anyio import EndOfStream, IncompleteRead
from anyio.abc import AnyByteReceiveStream, ObjectReceiveStream
from anyio.streams.buffered import BufferedByteReceiveStream


@dataclass
class ServerEvent:
    id: str | None = None
    event: str | None = None
    data: str | None = None


class ServerEventStream(ObjectReceiveStream[ServerEvent]):
    def __init__(
        self, stream: AnyByteReceiveStream, max_event_size: int = 65536
    ) -> None:
        self._stream = BufferedByteReceiveStream(stream)
        self._max_event_size = max_event_size
        self._retry: int | None = None

    @property
    def retry(self) -> int | None:
        return self._retry

    async def receive(self) -> ServerEvent:
        try:
            text = await self._stream.receive_until(b"\r\n\r\n", self._max_event_size)
        except IncompleteRead:
            raise EndOfStream from None

        event = ServerEvent()
        for line in text.decode("utf-8", errors="replace").split("\r\n"):
            key, value = line.split(":", 1)
            value = value.lstrip()
            match key:
                case "data":
                    if event.data is None:
                        event.data = value
                    else:
                        event.data += value
                case "id":
                    event.id = value
                case "event":
                    event.event = value
                case "retry" if value.isdigit():
                    retry = int(value)
                    if retry >= 0:
                        self._retry = retry

        return event

    async def aclose(self) -> None:
        await self._stream.aclose()
