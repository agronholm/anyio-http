API reference
=============

.. py:currentmodule:: anyio_http

Client
------

.. autoclass:: HTTPClient

HTTP request and response
-------------------------

.. autoclass:: HTTPRequest
.. autoclass:: HTTPResponse
.. autoclass:: HTTPResponseAttribute

WebSocket
---------

.. autoclass:: WebSocketConnection
.. autoclass:: WebSocketAttribute

Server-Sent Events
------------------

.. autoclass:: ServerEvent
.. autoclass:: ServerEventStream

Content wrappers
----------------

.. autoclass:: JSON
.. autoclass:: CBOR
.. autoclass:: Form
.. autoclass:: FormField

Base classes
------------

.. autoclass:: ContentDecoder
.. autoclass:: ContentWrapper

Type aliases
------------

.. autodata:: ASGIApp
.. autodata:: ASGIEvent

Exceptions
----------

.. autoexception:: HTTPError
.. autoexception:: HTTPStatusError
.. autoexception:: ASGIError
.. autoexception:: WebSocketError
.. autoexception:: WebSocketConnectionBroken
.. autoexception:: WebSocketConnectionEnded
