"""Is Home Assistant's clock right? An SNTP check, no dependencies.

NO HOME ASSISTANT IMPORTS.

The day/night switch happens on the meter's clock, so a house computer whose clock has
drifted by minutes would book kWh in the wrong band around 23:00 and 08:00 GMT. Home
Assistant OS keeps time with NTP already; a container or a VM may not. So the
integration asks an NTP server a few times a day how far off the local clock is, uses
that offset for every band decision, and says so if it is more than a second.

It never sets the system clock: that is the operating system's job, not an
integration's.
"""

from __future__ import annotations

import asyncio
import socket
import struct
import time
from dataclasses import dataclass

NTP_EPOCH_DELTA = 2208988800  # seconds between 1900-01-01 and 1970-01-01
DEFAULT_SERVER = "pool.ntp.org"


@dataclass
class ClockCheck:
    server: str
    offset: float      # seconds to ADD to the local clock to get true time
    delay: float       # round trip, seconds
    checked_at: float  # local epoch


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.future: asyncio.Future[tuple[bytes, float]] = asyncio.get_running_loop().create_future()

    def datagram_received(self, data: bytes, addr) -> None:  # noqa: ANN001
        if not self.future.done():
            self.future.set_result((data, time.time()))

    def error_received(self, exc: Exception) -> None:
        if not self.future.done():
            self.future.set_exception(exc)


def _to_ntp(t: float) -> bytes:
    seconds = int(t) + NTP_EPOCH_DELTA
    fraction = int((t % 1) * 2**32)
    return struct.pack("!II", seconds, fraction)


def _from_ntp(data: bytes, at: int) -> float:
    seconds, fraction = struct.unpack("!II", data[at : at + 8])
    return seconds - NTP_EPOCH_DELTA + fraction / 2**32


async def check(server: str = DEFAULT_SERVER, timeout: float = 5.0) -> ClockCheck:
    """One SNTP request (RFC 4330). Raises OSError / TimeoutError on failure."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(server, 123, type=socket.SOCK_DGRAM)
    family, _, _, _, address = infos[0]
    transport, protocol = await loop.create_datagram_endpoint(_Protocol, family=family, remote_addr=address)
    try:
        packet = bytearray(48)
        packet[0] = 0x23  # LI 0, version 4, mode 3 (client)
        t1 = time.time()
        packet[40:48] = _to_ntp(t1)
        transport.sendto(bytes(packet))
        data, t4 = await asyncio.wait_for(protocol.future, timeout)
    finally:
        transport.close()
    if len(data) < 48:
        raise OSError("short NTP reply")
    t2 = _from_ntp(data, 32)  # server receive
    t3 = _from_ntp(data, 40)  # server transmit
    offset = ((t2 - t1) + (t3 - t4)) / 2
    delay = (t4 - t1) - (t3 - t2)
    return ClockCheck(server, offset, delay, t4)
