from __future__ import annotations

from abc import ABCMeta, abstractmethod
from base64 import b64encode
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._client import HTTPClient


class Authenticator(metaclass=ABCMeta):
    @abstractmethod
    async def get_authorization(self, client: HTTPClient) -> str:
        pass


class BasicAuth(Authenticator):
    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        secret = f"{self.username}:{self.password}".encode()
        self._header = "Basic " + b64encode(secret).decode("ascii")

    async def get_authorization(self, client: HTTPClient) -> str:
        return self._header


# class DigestAuth(Authenticator):
#     def __init__(self, username: str, password: str):
#         self.username = username
#         self.password = password
#
#     async def get_authorization(self, client: HTTPClient) -> str:
#         pass


# class OAuth2AuthorizationCodeAuthenticator(Authenticator):
#     def __init__(
#         self,
#         token_endpooint: URL | str,
#         client_id: str,
#         client_secret: str | None = None,
#     ):
#         self.token_endpooint = URL(token_endpooint)
#         self.client_id = client_id
#         self.client_secret = client_secret
#
#     async def get_authorization(self, client: HTTPClient) -> str:
#         headers = {"Content-Type": "application/x-www-form-urlencoded"}
#         body = {
#             "grant_type": "authorization_code",
#             "code": "123456",
#             "client_id": self.client_id,
#         }
#         if self.client_secret:
#             body["client_secret"] = self.client_secret
#
#         response = await client.post(self.token_endpooint, body)
#
#
# class OAuth2ClientCredentialsAuthenticator(Authenticator):
#     def __init__(
#         self,
#         token_endpooint: URL | str,
#         client_id: str,
#         client_secret: str | None = None,
#     ):
#         self.token_endpooint = URL(token_endpooint)
#         self.client_id = client_id
#         self.client_secret = client_secret
#
#
# class OAuth2PasswordAuthenticator(Authenticator):
#     def __init__(self, token_endpooint: URL | str, client_id: str, password: str):
#         self.token_endpooint = URL(token_endpooint)
#         self.client_id = client_id
#         self.password = password
#
#
# class OAuth2DeviceAuthenticator(Authenticator):
#     def __init__(self, token_endpooint: URL | str, client_id: str, password: str):
#         self.token_endpooint = URL(token_endpooint)
#         self.client_id = client_id
#         self.password = password
