"""Feed errors and the bind rules both feed servers share."""

from __future__ import annotations

import errno

FEED_HOST = "127.0.0.1"

# EADDRINUSE / EACCES, plus their Winsock numbers (WSAEADDRINUSE 10048, WSAEACCES 10013):
# on Windows a socket bind error carries the WSA code in ``errno``, and a port inside an
# excluded range (Hyper-V, WSL) fails with WSAEACCES rather than "in use".
_PORT_UNAVAILABLE = frozenset({errno.EADDRINUSE, errno.EACCES, 10048, 10013})


class FeedPortInUseError(OSError):
    """A feed port could not be bound; the caller disables the feed and names ``port``."""

    def __init__(self, port: int) -> None:
        super().__init__(f"text feed port {port} is already in use")
        self.port = port


def is_port_unavailable(err: OSError) -> bool:
    return err.errno in _PORT_UNAVAILABLE
