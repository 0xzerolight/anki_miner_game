"""A ``truststore.SSLContext`` stand-in that records itself and stops where the TLS handshake starts.

``install(monkeypatch)`` puts it in place of ``truststore.SSLContext`` and returns the list every
context made from then on lands in. Its ``wrap_socket`` notes the host name the connection asked
for and raises ``HandshakeStoppedError`` before a byte is sent, so a test can point the real HTTPS
transport at a loopback port and see which verifier the connection would have been checked by.
"""

import socket
import ssl

import pytest
import truststore


class HandshakeStoppedError(Exception):
    """Raised where the TLS handshake would start."""


class RecordingContext(truststore.SSLContext):
    server_hostname: str | None = None

    def wrap_socket(
        self,
        sock: socket.socket,
        server_side: bool = False,
        do_handshake_on_connect: bool = True,
        suppress_ragged_eofs: bool = True,
        server_hostname: str | None = None,
        session: ssl.SSLSession | None = None,
    ) -> ssl.SSLSocket:
        self.server_hostname = server_hostname
        raise HandshakeStoppedError


def install(monkeypatch: pytest.MonkeyPatch) -> list[RecordingContext]:
    made: list[RecordingContext] = []

    class Recording(RecordingContext):
        def __init__(self, protocol: int) -> None:
            super().__init__(protocol)
            made.append(self)

    monkeypatch.setattr(truststore, "SSLContext", Recording)
    return made
