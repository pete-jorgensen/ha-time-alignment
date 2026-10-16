"""NTP: ask one server what time it is, and how sure it is.

No Home Assistant import here. The packet work is ``ntplib``'s; this module only turns
its answer into the figures that say whether a clock is aligned:

- **offset** — that host's clock minus ours, from the four timestamps (RFC 5905 §8).
  Positive means the host is ahead.
- **delay** — the round trip. Half of it bounds how wrong the offset can be.
- **root distance** — ``root_delay / 2 + root_dispersion``: the host's own estimate of
  its maximum error against UTC, carried up from its reference.
- **leap indicator 3** — the host says it is not synchronised at all. Its time is a
  free-running guess, whatever the offset reads.
- **stratum 0** — a kiss-o'-death (RFC 5905 §7.4): the server refuses to serve us (``RATE``
  when we query too often, ``DENY``/``RSTR`` when it will not). Its timestamps are not a
  measurement, so it is raised as :class:`KissOfDeath`, never returned as a sample.

How hard a host is asked (:func:`measure`) depends on where it is. A LAN server gets a
burst of ``NTP_SAMPLES`` queries ``NTP_SAMPLE_GAP`` apart and the cleanest one is kept;
a public server gets **one** query per poll. Public pools ask clients for about one query
a minute at most (a settled chrony or ntpd sends one every 64 s or slower), and a burst
every poll from every Home Assistant that installs this would be several times that.

``ntplib`` is synchronous, so callers run :func:`measure` in an executor.
"""

from __future__ import annotations

import dataclasses
import ipaddress
import socket
import time
from dataclasses import dataclass

import ntplib

from .const import NTP_SAMPLE_GAP, NTP_SAMPLES, NTP_TIMEOUT

LEAP_NOT_SYNCHRONISED = 3


class KissOfDeath(ntplib.NTPException):
    """The server answered with stratum 0: a refusal, carrying a code such as ``RATE``."""

    def __init__(self, host: str, code: str) -> None:
        super().__init__(f"{host} refused service (kiss-o'-death {code or '?'})")
        self.code = code


@dataclass(frozen=True)
class NtpSample:
    """One answered query."""

    host: str
    offset_s: float
    delay_s: float
    stratum: int
    leap: int
    reference: str
    root_delay_s: float
    root_dispersion_s: float
    measured_at: float
    # The address actually queried: for a pool name, which server answered this poll.
    address: str = ""

    @property
    def root_distance_s(self) -> float:
        return self.root_delay_s / 2 + self.root_dispersion_s

    @property
    def synchronised(self) -> bool:
        # 16 is "unsynchronised" in RFC 5905; 0 never gets here (KissOfDeath).
        return self.leap != LEAP_NOT_SYNCHRONISED and 0 < self.stratum < 16


def reference_text(ref_id: int, stratum: int) -> str:
    """The reference identifier as people read it: a refclock code such as ``PPS`` or
    ``GPS`` for stratum 0-1, the upstream server's IPv4 address above that.

    Not ``ntplib.ref_id_to_text``: that turns codes into long phrases ("Global Position
    System"), calls any code missing from its table "Unidentified", and raises above
    stratum 254. The code itself is what chronyc and ntpq print."""
    raw = ref_id.to_bytes(4, "big")
    if stratum <= 1:
        return raw.decode("ascii", "replace").strip("\x00 ")
    return ".".join(str(b) for b in raw)


def from_stats(host: str, stats: ntplib.NTPStats, measured_at: float) -> NtpSample:
    """Turn ntplib's raw stats into a sample. Split from :func:`query` for testing."""
    return NtpSample(
        host=host,
        offset_s=stats.offset,
        delay_s=stats.delay,
        stratum=stats.stratum,
        leap=stats.leap,
        reference=reference_text(stats.ref_id, stats.stratum),
        root_delay_s=stats.root_delay,
        root_dispersion_s=stats.root_dispersion,
        measured_at=measured_at,
    )


def query(host: str, port: int = 123, timeout: float = 2.0) -> NtpSample:
    """One NTPv4 client request. Raises ``ntplib.NTPException`` (including
    :class:`KissOfDeath`) or ``OSError``."""
    stats = ntplib.NTPClient().request(host, version=4, port=port, timeout=timeout)
    if stats.stratum == 0:
        raise KissOfDeath(host, reference_text(stats.ref_id, 0))
    return from_stats(host, stats, time.time())


def resolve(host: str, port: int = 123) -> str:
    """The one address a poll will query. Resolved once so every query in a burst goes
    to the same server: ``pool.ntp.org`` hands out a different one per lookup."""
    return socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)[0][4][0]


def is_public(address: str) -> bool:
    """A globally routable address — someone else's server, so it is asked gently.
    Private, loopback, link-local and carrier-grade NAT ranges are all ours."""
    return ipaddress.ip_address(address.split("%")[0]).is_global


def measure(host: str, port: int = 123, timeout: float = NTP_TIMEOUT) -> NtpSample:
    """One poll's measurement of one host: a burst on the LAN, one query in public.

    Raises ``ntplib.NTPException`` (a :class:`KissOfDeath` included) or ``OSError``
    (DNS failure among them) when nothing usable came back. A kiss-o'-death ends the
    burst at once: the server has asked us to back off."""
    address = resolve(host, port)
    count = 1 if is_public(address) else NTP_SAMPLES
    samples: list[NtpSample] = []
    last_error: Exception | None = None
    for attempt in range(count):
        if attempt:
            time.sleep(NTP_SAMPLE_GAP)
        try:
            samples.append(query(address, port, timeout))
        except KissOfDeath as err:
            last_error = err
            break
        except (ntplib.NTPException, OSError) as err:
            last_error = err
    if not samples:
        raise last_error or OSError(f"{host}: no answer")
    return dataclasses.replace(best_of(samples), host=host, address=address)


def best_of(samples: list[NtpSample]) -> NtpSample:
    """The sample with the shortest round trip.

    A single NTP exchange can be skewed by a queueing delay on one leg; the offset error
    is bounded by half the delay, so the fastest exchange is the most trustworthy one.
    This is the same "minimum delay" filter ntpd and chrony apply to their samples."""
    if not samples:
        raise ValueError("no samples")
    return min(samples, key=lambda s: s.delay_s)
