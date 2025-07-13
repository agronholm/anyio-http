from __future__ import annotations

from pathlib import Path
from ssl import Purpose, SSLContext, create_default_context

import pytest
import trustme
from _pytest.fixtures import SubRequest
from anyio import get_all_backends
from pytest import TempPathFactory


@pytest.fixture(scope="module", params=get_all_backends())
async def anyio_backend(request: SubRequest) -> str:
    return request.param


@pytest.fixture(scope="session")
def ca() -> trustme.CA:
    return trustme.CA()


@pytest.fixture(scope="session")
def ca_path(ca: trustme.CA, tmp_path_factory: TempPathFactory) -> Path:
    ca_path = tmp_path_factory.mktemp("ca") / "ca.pem"
    ca.cert_pem.write_to_path(ca_path)
    return ca_path


@pytest.fixture(scope="session")
def server_cert(ca: trustme.CA) -> trustme.LeafCert:
    return ca.issue_cert("localhost")


@pytest.fixture(scope="session")
def server_cert_path(
    server_cert: trustme.LeafCert, tmp_path_factory: TempPathFactory
) -> Path:
    cert_path = tmp_path_factory.mktemp("server") / "server.pem"
    server_cert.private_key_and_cert_chain_pem.write_to_path(cert_path)
    return cert_path


@pytest.fixture(scope="session")
def client_ssl_context(ca: trustme.CA) -> SSLContext:
    # Set the SSLKEYLOGFILE environment variable to point to a file to use Wireshark to
    # debug the TLS encrypted streams
    ssl_context = create_default_context(Purpose.SERVER_AUTH)
    ca.configure_trust(ssl_context)
    return ssl_context


@pytest.fixture(scope="session")
def server_ssl_context(ca: trustme.CA) -> SSLContext:
    ssl_context = create_default_context(Purpose.CLIENT_AUTH)
    ca.configure_trust(ssl_context)
    return ssl_context
