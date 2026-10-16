"""One poll = every NTP host queried at once, alongside one PTP sweep.

All hosts are measured in the same poll so their offsets share one instant of the local
clock; that is what makes host-minus-reference cancel Home Assistant's own error.

A host that does not answer is a *result* — the thing the problem sensors exist to
report — not a failed poll. So a poll never fails for want of answers: if it did, Home
Assistant would mark every entity unavailable, problem sensors included, at exactly the
moment they should be ON.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from . import ntp, ptp
from .alignment import NetworkAlignment, evaluate
from .const import (
    CONF_NTP_HOSTS,
    CONF_PTP_DOMAIN,
    CONF_PTP_ENABLED,
    CONF_PTP_INTERFACE,
    CONF_PTP_TOLERANCE_US,
    CONF_REFERENCE,
    CONF_SCAN_INTERVAL,
    CONF_TOLERANCE_MS,
    DEFAULT_PTP_DOMAIN,
    DEFAULT_PTP_TOLERANCE_US,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TOLERANCE_MS,
    DOMAIN,
    PTP_LISTEN_WINDOW,
)

_LOGGER = logging.getLogger(__name__)


def split_host(entry: str) -> tuple[str, int]:
    """``host``, ``host:port`` or ``[v6]:port``. A bare IPv6 address has several colons
    and no port. Raises ``ValueError`` for anything else."""
    if entry.startswith("["):
        host, bracket, rest = entry[1:].partition("]")
        if not bracket or (rest and not rest.startswith(":")):
            raise ValueError(f"bad host: {entry}")
        port = int(rest[1:]) if rest else 123
    elif entry.count(":") == 1:
        host, text = entry.split(":")
        port = int(text)
    else:
        host, port = entry, 123
    if not host or not 0 < port < 65536:
        raise ValueError(f"bad host: {entry}")
    return host, port


@dataclasses.dataclass
class Snapshot:
    ntp_samples: dict[str, ntp.NtpSample] = dataclasses.field(default_factory=dict)
    ntp_errors: dict[str, str] = dataclasses.field(default_factory=dict)
    alignment: NetworkAlignment | None = None
    ptp_clocks: dict[str, ptp.PtpClock] = dataclasses.field(default_factory=dict)
    ptp_error: str | None = None


type TimeAlignmentConfigEntry = ConfigEntry[TimeAlignmentCoordinator]


class TimeAlignmentCoordinator(DataUpdateCoordinator[Snapshot]):
    """Polls every configured host and the PTP segment."""

    config_entry: TimeAlignmentConfigEntry

    def __init__(self, hass: HomeAssistant, entry: TimeAlignmentConfigEntry) -> None:
        opts = entry.options
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(
                seconds=opts.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ),
        )
        self.hosts: list[str] = list(opts.get(CONF_NTP_HOSTS, []))
        self.reference: str | None = opts.get(CONF_REFERENCE) or None
        self.tolerance_s: float = opts.get(CONF_TOLERANCE_MS, DEFAULT_TOLERANCE_MS) / 1000
        self.ptp_enabled: bool = opts.get(CONF_PTP_ENABLED, False)
        self.ptp_domain: int = opts.get(CONF_PTP_DOMAIN, DEFAULT_PTP_DOMAIN)
        self.ptp_interface: str | None = opts.get(CONF_PTP_INTERFACE) or None
        self.ptp_tolerance_ns: float = (
            opts.get(CONF_PTP_TOLERANCE_US, DEFAULT_PTP_TOLERANCE_US) * 1000
        )
        self._ptp_error_logged = False
        # Set by async_setup_entry once the network device is registered.
        self.network_device_registry_id: str | None = None

    async def _query_host(self, entry: str) -> ntp.NtpSample:
        host, port = split_host(entry)
        sample = await self.hass.async_add_executor_job(ntp.measure, host, port)
        # Keep the configured spelling as the key, not the bare host name.
        return dataclasses.replace(sample, host=entry)

    async def _ptp_sweep(self) -> tuple[dict[str, ptp.PtpClock], str | None]:
        try:
            clocks = await ptp.async_discover(
                domain=self.ptp_domain,
                interface_address=self.ptp_interface,
                window=PTP_LISTEN_WINDOW,
            )
        except OSError as err:
            # Most likely: UDP 320 is privileged and Home Assistant is not root, or
            # another process holds it without SO_REUSEADDR.
            msg = f"cannot listen for PTP on UDP {ptp.PTP_GENERAL_PORT}: {err}"
            if not self._ptp_error_logged:
                _LOGGER.warning(msg)
                self._ptp_error_logged = True
            return {}, msg
        self._ptp_error_logged = False
        return clocks, None

    async def _async_update_data(self) -> Snapshot:
        snap = Snapshot()

        async def ptp_part() -> None:
            if self.ptp_enabled:
                snap.ptp_clocks, snap.ptp_error = await self._ptp_sweep()

        *results, ptp_result = await asyncio.gather(
            *(self._query_host(h) for h in self.hosts),
            ptp_part(),
            return_exceptions=True,
        )
        for host, result in zip(self.hosts, results, strict=True):
            if isinstance(result, Exception):
                snap.ntp_errors[host] = str(result) or type(result).__name__
            elif isinstance(result, BaseException):
                raise result
            else:
                snap.ntp_samples[host] = result
        if isinstance(ptp_result, BaseException):
            raise ptp_result

        snap.alignment = evaluate(self.hosts, snap.ntp_samples, self.reference, self.tolerance_s)
        return snap
