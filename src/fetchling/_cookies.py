from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from yarl import URL


@dataclass(frozen=True, slots=True)
class Cookie:
    name: str
    value: str
    path: str | None = field(kw_only=True, default=None)
    domain: list[str] | None = field(kw_only=True, default=None)
    secure: bool = field(kw_only=True, default=False)
    expires: float | None = field(kw_only=True, default=None)

    def is_valid_for(self, url: URL) -> bool:
        if self.path and not url.path.startswith(self.path):
            return False

        if self.domain is not None and url.host is not None:
            domain = url.host.split(".")
            if not all(zip(domain, self.domain, strict=False)):
                return False

        if self.secure and url.scheme != "https":
            return False

        return True

    def has_expired(self, current_timestamp: float) -> bool:
        return self.expires is not None and current_timestamp > self.expires

    @staticmethod
    def parse(cookie: str, current_timestamp: float | None = None) -> Cookie:
        parts = cookie.split(";")
        name, value = parts[0].split("=", 1)
        path: str | None = None
        domain: list[str] | None = None
        secure: bool = False
        expires: float | None = None

        for part in parts[1:]:
            key, _, value = part.partition("=")
            value = value.strip()
            match key.strip().lower():
                case "path":
                    if value and value.startswith("/"):
                        path = value
                case "domain":
                    if value:
                        domain = value.lstrip(".").split(".")
                case "secure":
                    secure = True
                case "max-age" if current_timestamp is not None:
                    if value.isdigit():
                        expires = current_timestamp + int(value)
                case "expires" if current_timestamp is not None:
                    try:
                        expires_dt = datetime.strptime(
                            value, "%a, %d-%b-%Y %H:%M:%S GMT"
                        )
                    except ValueError:
                        pass
                    else:
                        expires_ts = expires_dt.replace(tzinfo=timezone.utc).timestamp()
                        expires = int(current_timestamp - expires_ts)

        return Cookie(
            name=name,
            value=value,
            path=path,
            domain=domain,
            secure=secure,
            expires=expires,
        )

    def __str__(self) -> str:
        return f"{self.name}={self.value}"
