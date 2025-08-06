from __future__ import annotations

from abc import ABCMeta, abstractmethod
from collections.abc import AsyncGenerator, Callable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from os import PathLike
from typing import Any, TypeAlias

from anyio import Path
from anyio.abc import ByteReceiveStream
from anyio.streams.buffered import BufferedByteReceiveStream

from ._streams import AsyncGeneratorStream

JSONType: TypeAlias = (
    dict[str, "JSONType"]
    | list["JSONType"]
    | tuple["JSONType", ...]
    | str
    | float
    | bool
    | None
)


class ContentWrapper(metaclass=ABCMeta):
    __slots__ = ("content_type",)

    def __init__(self, *, content_type: str):
        self.content_type: str = content_type

    @abstractmethod
    def to_body(self) -> bytes | ByteReceiveStream:
        pass


class JSON(ContentWrapper):
    __slots__ = ("value", "pretty", "default")

    def __init__(
        self,
        value: JSONType,
        /,
        *,
        content_type: str = "application/json",
        pretty: bool = False,
        default: Callable[[Any], Any] | None = None,
    ):
        super().__init__(content_type=content_type)
        self.value: JSONType = value
        self.pretty: bool = pretty
        self.default: Callable[[Any], Any] | None = default

    @staticmethod
    @lru_cache(1)
    def dumps_function() -> Callable[[JSONType, JSON], bytes]:
        try:
            from orjson import OPT_INDENT_2
            from orjson import dumps as orjson_dumps

            def dump(value: JSONType, wrapper: JSON) -> bytes:
                return orjson_dumps(
                    value,
                    option=OPT_INDENT_2 if wrapper.pretty else None,
                    default=wrapper.default,
                )
        except ImportError:
            from json import dumps as json_dumps

            def dump(value: JSONType, wrapper: JSON) -> bytes:
                return json_dumps(
                    value,
                    ensure_ascii=False,
                    indent=2 if wrapper.pretty else 0,
                    default=wrapper.default,
                ).encode("utf-8")

        return dump

    def to_body(self) -> bytes | ByteReceiveStream:
        return self.dumps_function()(self.value, self)


class CBOR(ContentWrapper):
    __slots__ = ("value", "kwargs")

    def __init__(
        self,
        value: JSONType,
        /,
        *,
        content_type: str = "application/cbor",
        kwargs: Mapping[str, Any] | None = None,
    ):
        super().__init__(content_type=content_type)
        self.value: JSONType = value
        self.kwargs: Mapping[str, Any] = kwargs or {}

    @staticmethod
    @lru_cache(1)
    def dumps_function() -> Callable[[Any, CBOR], bytes]:
        try:
            from cbor2 import dumps
        except ModuleNotFoundError as exc:
            raise ImportError(
                "cbor2 module not found - install with 'pip install fetchling[cbor]'"
            ) from exc

        def dump(value: Any, wrapper: CBOR) -> bytes:
            return dumps(value, **wrapper.kwargs)

        return dump

    def to_body(self) -> bytes | ByteReceiveStream:
        return self.dumps_function()(self.value, self)


@dataclass(eq=False, frozen=True, slots=True)
class FormField:
    value: str | bytes | Path | ByteReceiveStream
    filename: str | None = field(kw_only=True, default=None)
    content_type: str | None = field(kw_only=True, default=None)

    @staticmethod
    def convert(value: str | bytes | PathLike[str] | FormField) -> FormField:
        match value:
            case str():
                return FormField(value=value, content_type="text/plain; charset=utf-8")
            case bytes():
                return FormField(value=value, content_type="application/octet-stream")
            case PathLike():
                from mimetypes import guess_type

                path = Path(value)
                content_type = guess_type(path.name)[0] or "application/octet-stream"
                return FormField(
                    value=path, filename=path.name, content_type=content_type
                )
            case FormField():
                return value
            case _:
                raise TypeError(
                    f"cannot convert {value.__class__.__qualname__} to FormField"
                )


class Form(ContentWrapper):
    __slots__ = ("_content",)

    _content: bytes | MultiPartStream

    class MultiPartStream(BufferedByteReceiveStream):
        def __init__(self, form_fields: Mapping[str, FormField]) -> None:
            from random import randbytes

            generator_stream = AsyncGeneratorStream(self._generate_content())
            super().__init__(generator_stream)
            self._boundary = randbytes(16).hex()
            self._form_fields = form_fields

        async def _generate_content(self) -> AsyncGenerator[bytes]:
            for name, field_ in self._form_fields.items():
                data = (
                    f"\r\n--{self._boundary}\r\n"
                    f'content-disposition: form-data; name="{name}"'
                )
                if field_.filename:
                    data += f'; filename="{field_.filename}"'

                if field_.content_type:
                    data += f"\r\ncontent-type: {field_.content_type}"

                data += "\r\n\r\n"
                yield data.encode("utf-8")
                match field_.value:
                    case Path():
                        async with await field_.value.open("rb") as fp:
                            async for chunk in fp:
                                yield chunk
                    case ByteReceiveStream():
                        async for chunk in field_.value:
                            yield chunk
                    case str():
                        yield field_.value.encode("utf-8")
                    case bytes():
                        yield field_.value

            yield f"\r\n--{self._boundary}--\r\n".encode()

    def __init__(
        self,
        fields: Mapping[str, str | bytes | PathLike[str] | FormField],
        /,
        *,
        force_multipart: bool = False,
    ):
        if force_multipart or all(isinstance(value, str) for value in fields.values()):
            from urllib.parse import urlencode

            content_type = "application/x-www-form-urlencoded"
            self._content = urlencode(fields).encode("ascii")
        else:
            form_fields = {
                key: FormField.convert(value) for key, value in fields.items()
            }
            self._content = Form.MultiPartStream(form_fields)
            content_type = f"multipart/form-data; boundary={self._content._boundary}"

        super().__init__(content_type=content_type)

    def to_body(self) -> bytes | ByteReceiveStream:
        return self._content
