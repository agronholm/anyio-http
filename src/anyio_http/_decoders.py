from __future__ import annotations

import sys
from typing import ClassVar

from anyio import EndOfStream
from anyio.abc import ByteReceiveStream

registered_decoders: dict[str, type[ContentDecoder]] = {}


def get_decoder(name: str) -> type[ContentDecoder]:
    return registered_decoders[name]


class ContentDecoder(ByteReceiveStream):
    name: ClassVar[str]

    def __init_subclass__(cls, *, decoder: str):
        cls.name = decoder
        registered_decoders[decoder] = cls

    def __init__(self, wrapped_stream: ByteReceiveStream, /):
        self.wrapped_stream = wrapped_stream

    async def aclose(self) -> None:
        await self.wrapped_stream.aclose()


class GzipDecompressor(ContentDecoder, decoder="gzip"):
    def __init__(self, wrapped_stream: ByteReceiveStream, /):
        import gzip

        super().__init__(wrapped_stream)
        self._decompress = gzip.decompress

    async def receive(self, max_bytes: int = 65536) -> bytes:
        compressed = await self.wrapped_stream.receive(max_bytes)
        return self._decompress(compressed)


class ZstdDecompressor(ContentDecoder, decoder="zstd"):
    def __init__(self, wrapped_stream: ByteReceiveStream, /):
        super().__init__(wrapped_stream)
        if sys.version_info >= (3, 14):
            from compression.zstd import ZstdDecompressor
        else:
            try:
                from zstandard import ZstdDecompressor
            except ModuleNotFoundError as exc:
                raise ImportError(
                    "zstandard module not available; "
                    "fix with 'pip install anyio-http[zstd]'"
                ) from exc

            self._decompressor = ZstdDecompressor()

    async def receive(self, max_bytes: int = 65536) -> bytes:
        compressed = await self.wrapped_stream.receive(max_bytes)
        return self._decompressor.decompress(compressed)


class DeflateDecompressor(ContentDecoder, decoder="deflate"):
    def __init__(self, wrapped_stream: ByteReceiveStream, /):
        import zlib

        super().__init__(wrapped_stream)

        # https://stackoverflow.com/questions/1838699
        self._decompressor = zlib.decompressobj(zlib.MAX_WBITS | 32)

    async def receive(self, max_bytes: int = 65536) -> bytes:
        try:
            compressed = await self.wrapped_stream.receive(max_bytes)
        except EndOfStream:
            return self._decompressor.flush()

        return self._decompressor.decompress(compressed)


class BrotliDecompressor(ContentDecoder, decoder="brotli"):
    def __init__(self, wrapped_stream: ByteReceiveStream, /):
        super().__init__(wrapped_stream)
        try:
            import brotlicffi as brotli
        except ImportError:
            try:
                import brotli
            except ModuleNotFoundError as exc:
                raise ImportError(
                    "brotli module not available; "
                    "fix with 'pip install anyio-http[brotli]'"
                ) from exc

        self._decompress = brotli.decompress

    async def receive(self, max_bytes: int = 65536) -> bytes:
        compressed = await self.wrapped_stream.receive(max_bytes)
        return self._decompress(compressed)
