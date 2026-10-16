"""Problem sensors: ON when a clock (or the network as a whole) is not aligned.

Each carries its reasons as an attribute, so an automation or a card can say *why*.
A host problem sensor stays available while the host is unreachable — being unreachable
is the problem it reports.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ptp
from .alignment import host_problems
from .coordinator import TimeAlignmentConfigEntry, TimeAlignmentCoordinator
from .const import DOMAIN
from .entity import NetworkEntity, NtpHostEntity, PtpClockEntity, ptp_device_id

PARALLEL_UPDATES = 0


class _ProblemMixin(BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def _reasons(self) -> list[str]:
        raise NotImplementedError

    @property
    def is_on(self) -> bool:
        return bool(self._reasons())

    @property
    def extra_state_attributes(self) -> dict:
        return {"reasons": self._reasons()}


def _network_reasons(coordinator: TimeAlignmentCoordinator) -> list[str]:
    snap = coordinator.data
    reasons = [
        f"{h}: {r}" for h in coordinator.hosts for r in host_problems(h, snap.alignment)
    ]
    if coordinator.ptp_enabled:
        if snap.ptp_error:
            reasons.append(f"ptp: {snap.ptp_error}")
        for cid, clock in snap.ptp_clocks.items():
            reasons += [f"{cid}: {r}" for r in ptp.problems(clock, coordinator.ptp_tolerance_ns)]
        reasons += [f"ptp: {r}" for r in ptp.network_problems(snap.ptp_clocks)]
    return reasons


class NetworkProblem(NetworkEntity, _ProblemMixin):
    def __init__(self, coordinator: TimeAlignmentCoordinator) -> None:
        super().__init__(coordinator, "network_problem")

    def _reasons(self) -> list[str]:
        return _network_reasons(self.coordinator)


class NtpHostProblem(NtpHostEntity, _ProblemMixin):
    def __init__(self, coordinator: TimeAlignmentCoordinator, host: str) -> None:
        super().__init__(coordinator, "host_problem", host)

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    def _reasons(self) -> list[str]:
        return host_problems(self.host, self.coordinator.data.alignment)

    @property
    def extra_state_attributes(self) -> dict:
        # Why it did not answer (timeout, DNS, a kiss-o'-death code), when it did not.
        sample = self.coordinator.data.ntp_samples.get(self.host)
        return {
            "reasons": self._reasons(),
            "error": self.coordinator.data.ntp_errors.get(self.host),
            "address": sample.address if sample else None,
        }


class PtpClockProblem(PtpClockEntity, _ProblemMixin):
    def __init__(self, coordinator: TimeAlignmentCoordinator, clock_id: str) -> None:
        super().__init__(coordinator, "clock_problem", clock_id)

    @property
    def available(self) -> bool:
        # A clock that stopped answering is a problem, not an unknown.
        return self.coordinator.last_update_success

    def _reasons(self) -> list[str]:
        clock = self.coordinator.data.ptp_clocks.get(self.clock_id)
        if clock is None:
            return ["not_answering"]
        return ptp.problems(clock, self.coordinator.ptp_tolerance_ns)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TimeAlignmentConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [NetworkProblem(coordinator)]
        + [NtpHostProblem(coordinator, h) for h in coordinator.hosts]
    )
    if not coordinator.ptp_enabled:
        return
    # Clocks seen on an earlier run keep their problem sensor even while silent, so one
    # that stopped answering reads "not_answering" rather than vanishing on a reload.
    prefix = ptp_device_id(entry.entry_id, "")
    remembered = {
        ident[len(prefix):]
        for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
        for domain, ident in device.identifiers
        if domain == DOMAIN and ident.startswith(prefix)
    }
    known: set[str] = set(remembered)
    async_add_entities(PtpClockProblem(coordinator, cid) for cid in sorted(remembered))

    @callback
    def _add_new_clocks() -> None:
        new = [cid for cid in coordinator.data.ptp_clocks if cid not in known]
        if not new:
            return
        known.update(new)
        async_add_entities(PtpClockProblem(coordinator, cid) for cid in new)

    _add_new_clocks()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_clocks))
