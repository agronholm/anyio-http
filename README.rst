.. image:: https://github.com/agronholm/anyio-http/actions/workflows/test.yml/badge.svg
  :target: https://github.com/agronholm/anyio-http/actions/workflows/test.yml
  :alt: Build Status
.. image:: https://coveralls.io/repos/github/agronholm/anyio-http/badge.svg?branch=master
  :target: https://coveralls.io/github/agronholm/anyio?branch=master
  :alt: Code Coverage
.. image:: https://readthedocs.org/projects/anyio-http/badge/?version=latest
  :target: https://anyio.readthedocs.io/en/latest/?badge=latest
  :alt: Documentation
.. image:: https://badges.gitter.im/gitterHQ/gitter.svg
  :target: https://gitter.im/python-trio/AnyIO
  :alt: Gitter chat

AnyIO-HTTP provides both an asynchronous and synchronous HTTP client, backed by the
AnyIO_ library. It works on top of asyncio, Trio_ and any future event loop
implementation supported by AnyIO.

Documentation
-------------

View full documentation at: https://anyio-http.readthedocs.io/

Features
--------

AnyIO-HTTP offers the following functionality:

* HTTP/1.1 and HTTP/2 support
* WebSocket client
* SSE (Server Sent Events) client
* HTTP tunneling using the ``CONNECT`` method
* Support for testing ASGI 3.0 applications directly (without sockets)
* Transparent decompression support (deflate, gzip, zstd, brotli)

.. _AnyIO: https://github.com/agronholm/anyio
.. _Trio: https://github.com/python-trio/trio
