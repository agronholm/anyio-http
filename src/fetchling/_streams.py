from __future__ import annotations

import sys
from collections.abc import AsyncGenerator

from anyio import EndOfStream
from anyio.abc import ObjectReceiveStream

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override


class StaticStream(ObjectReceiveStream[bytes]):
    def __init__(self, data: bytes | memoryview):
        self.data: bytes | memoryview | None = data

    @override
    async def receive(self) -> bytes:
        if self.data is None:
            raise EndOfStream

        data, self.data = self.data, None
        return bytes(data)

    @override
    async def aclose(self) -> None:
        pass


class AsyncGeneratorStream(ObjectReceiveStream[bytes]):
    def __init__(self, generator: AsyncGenerator[bytes]) -> None:
        self._generator = generator

    @override
    async def receive(self) -> bytes:
        try:
            return await anext(self._generator)
        except StopAsyncIteration:
            raise EndOfStream from None

    @override
    async def aclose(self) -> None:
        await self._generator.aclose()
