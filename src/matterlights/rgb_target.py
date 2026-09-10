"""Where the RGB extension's frames go: a unix socket path, or UDP.

``RGB_PUBLISH_SOCKET`` takes either. A plain path is a unix datagram socket --
Linux, where the extension's socket file is readable by one user alone.
``udp://HOST:PORT`` is UDP, which is what Windows needs: it has no unix
datagram sockets at all, since its ``AF_UNIX`` is ``SOCK_STREAM`` only. The
extension (ambience-rgb) prints the exact value to paste.

A module of its own so the settings loader can validate the value without
importing the publisher, which the sync loop only loads when the extension is
enabled.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

UDP_SCHEME = "udp://"
EXAMPLE = "udp://127.0.0.1:45731"


@dataclass(frozen=True, slots=True)
class UdpTarget:
    host: str
    port: int

    def __str__(self) -> str:
        return f"{UDP_SCHEME}{self.host}:{self.port}"


def is_udp(value: str) -> bool:
    return str(value).strip().lower().startswith(UDP_SCHEME)


def parse_rgb_target(value: str | Path | UdpTarget) -> Path | UdpTarget:
    """A ``udp://HOST:PORT`` URL as a :class:`UdpTarget`; anything else as a path.

    Raises ``ValueError`` for a malformed URL, naming what one should look like.
    """

    if isinstance(value, (Path, UdpTarget)):
        return value
    text = str(value).strip()
    if not is_udp(text):
        return Path(text)
    host, separator, port = text[len(UDP_SCHEME):].rpartition(":")
    if (
        not separator
        or not host
        or not (port.isascii() and port.isdigit())
        or not 1 <= int(port) <= 65535
    ):
        raise ValueError(
            f"RGB_PUBLISH_SOCKET={text!r} must be udp://HOST:PORT, for example {EXAMPLE}"
        )
    return UdpTarget(host, int(port))
