"""Sensors: offsets, delays and error bounds for the network, each host, each PTP clock."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ptp
from .alignment import hosts_with_problems
from .coordinator import Snapshot, TimeAlignmentConfigEntry, TimeAlignmentCoordinator
from .entity import NetworkEntity, NtpHostEntity, PtpClockEntity
from .ntp import NtpSample

PARALLEL_UPDATES = 0


def _ms(seconds: float | None) -> float | None:
    return None if seconds is None else seconds * 1000


def _us(nanoseconds: float | None) -> float | None:
    return None if nanoseconds is None else nanoseconds / 1000


# --- network ------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class NetworkSensorDescription(SensorEntityDescription):
    value_fn: Callable[[Snapshot, TimeAlignmentCoordinator], Any]
    attrs_fn: Callable[[Snapshot, TimeAlignmentCoordinator], dict] | None = None
    ptp_only: bool = False


def _worst_ntp(snap: Snapshot, _c: TimeAlignmentCoordinator) -> float | None:
    a = snap.alignment
    if a is None or a.worst_host is None:
        return None
    return _ms(a.hosts[a.worst_host].offset_vs_reference_s)


def _worst_ptp(snap: Snapshot, _c: TimeAlignmentCoordinator) -> float | None:
    offsets = [
        c.offset_ns
        for c in snap.ptp_clocks.values()
        if c.port_state == "slave" and c.offset_ns is not None
    ]
    return _us(max(offsets, key=abs)) if offsets else None


NETWORK_SENSORS: tuple[NetworkSensorDescription, ...] = (
    NetworkSensorDescription(
        key="spread",
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        value_fn=lambda s, _c: _ms(s.alignment.spread_s) if s.alignment else None,
    ),
    NetworkSensorDescription(
        key="worst_offset",
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        value_fn=_worst_ntp,
        attrs_fn=lambda s, c: {
            "host": s.alignment.worst_host if s.alignment else None,
            "reference": c.reference,
            "basis": s.alignment.basis if s.alignment else None,
        },
    ),
    NetworkSensorDescription(
        # Every offset is measured against the clock Home Assistant runs on, so the
        # basis's offset (the reference's, or the median host's), negated, is that
        # clock's own error: often the only way to see it, since a host running a time
        # *client* answers no NTP queries.
        key="local_offset",
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        value_fn=lambda s, _c: (
            _ms(-s.alignment.basis_offset_s)
            if s.alignment and s.alignment.basis_offset_s is not None
            else None
        ),
        attrs_fn=lambda s, _c: {"basis": s.alignment.basis if s.alignment else None},
    ),
    NetworkSensorDescription(
        key="hosts_with_problems",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s, _c: len(hosts_with_problems(s.alignment)) if s.alignment else None,
        attrs_fn=lambda s, c: {
            "tolerance_ms": c.tolerance_s * 1000,
            "out_of_tolerance": s.alignment.out_of_tolerance if s.alignment else [],
            "unreachable": s.alignment.unreachable if s.alignment else [],
            "unsynchronised": s.alignment.unsynchronised if s.alignment else [],
        },
    ),
    NetworkSensorDescription(
        key="ptp_clocks",
        state_class=SensorStateClass.MEASUREMENT,
        ptp_only=True,
        value_fn=lambda s, _c: len(s.ptp_clocks),
        attrs_fn=lambda s, _c: {"error": s.ptp_error},
    ),
    NetworkSensorDescription(
        key="ptp_worst_offset",
        native_unit_of_measurement=UnitOfTime.MICROSECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        ptp_only=True,
        value_fn=_worst_ptp,
    ),
)


class NetworkSensor(NetworkEntity, SensorEntity):
    entity_description: NetworkSensorDescription

    def __init__(self, coordinator, description: NetworkSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data, self.coordinator)

    @property
    def extra_state_attributes(self) -> dict | None:
        fn = self.entity_description.attrs_fn
        return fn(self.coordinator.data, self.coordinator) if fn else None


# --- one NTP host -------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class NtpSensorDescription(SensorEntityDescription):
    value_fn: Callable[[NtpSample, Snapshot, str], Any]
    attrs_fn: Callable[[Snapshot, str], dict] | None = None


def _uncertainty_attrs(snap: Snapshot, host: str) -> dict:
    a = snap.alignment.hosts.get(host) if snap.alignment else None
    return {
        "uncertainty_ms": _ms(a.uncertainty_s) if a else None,
        "basis": snap.alignment.basis if snap.alignment else None,
    }


NTP_SENSORS: tuple[NtpSensorDescription, ...] = (
    NtpSensorDescription(
        key="offset",
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        value_fn=lambda s, _snap, _h: _ms(s.offset_s),
    ),
    NtpSensorDescription(
        key="offset_vs_reference",
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        value_fn=lambda _s, snap, h: (
            _ms(snap.alignment.hosts[h].offset_vs_reference_s)
            if snap.alignment and h in snap.alignment.hosts
            else None
        ),
        attrs_fn=_uncertainty_attrs,
    ),
    NtpSensorDescription(
        key="round_trip",
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        value_fn=lambda s, _snap, _h: _ms(s.delay_s),
    ),
    NtpSensorDescription(
        key="root_distance",
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        value_fn=lambda s, _snap, _h: _ms(s.root_distance_s),
    ),
    NtpSensorDescription(
        key="stratum",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s, _snap, _h: s.stratum,
    ),
    NtpSensorDescription(
        key="ntp_reference",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s, _snap, _h: s.reference or None,
    ),
)


class NtpSensor(NtpHostEntity, SensorEntity):
    entity_description: NtpSensorDescription

    def __init__(self, coordinator, description: NtpSensorDescription, host: str) -> None:
        super().__init__(coordinator, description.key, host)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        snap = self.coordinator.data
        sample = snap.ntp_samples.get(self.host)
        if sample is None:
            return None
        return self.entity_description.value_fn(sample, snap, self.host)

    @property
    def extra_state_attributes(self) -> dict | None:
        fn = self.entity_description.attrs_fn
        return fn(self.coordinator.data, self.host) if fn else None


# --- one PTP clock ------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class PtpSensorDescription(SensorEntityDescription):
    value_fn: Callable[[ptp.PtpClock], Any]


PTP_SENSORS: tuple[PtpSensorDescription, ...] = (
    PtpSensorDescription(
        key="offset_from_master",
        native_unit_of_measurement=UnitOfTime.MICROSECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        value_fn=lambda c: _us(c.offset_ns) if c.port_state == "slave" else None,
    ),
    PtpSensorDescription(
        key="mean_path_delay",
        native_unit_of_measurement=UnitOfTime.MICROSECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        value_fn=lambda c: _us(c.current.get("mean_path_delay_ns")),
    ),
    PtpSensorDescription(
        key="steps_removed",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.current.get("steps_removed"),
    ),
    PtpSensorDescription(
        key="port_state",
        device_class=SensorDeviceClass.ENUM,
        options=[*ptp.PORT_STATES.values()],
        value_fn=lambda c: c.port_state if c.port_state in ptp.PORT_STATES.values() else None,
    ),
    PtpSensorDescription(
        key="grandmaster",
        value_fn=lambda c: c.parent.get("grandmaster_identity")
        or c.time_status.get("grandmaster_identity"),
    ),
    PtpSensorDescription(
        key="grandmaster_clock_class",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.parent.get("grandmaster_clock_class"),
    ),
)


class PtpSensor(PtpClockEntity, SensorEntity):
    entity_description: PtpSensorDescription

    def __init__(self, coordinator, description: PtpSensorDescription, clock_id: str) -> None:
        super().__init__(coordinator, description.key, clock_id)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        clock = self.coordinator.data.ptp_clocks.get(self.clock_id)
        return None if clock is None else self.entity_description.value_fn(clock)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TimeAlignmentConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        NetworkSensor(coordinator, d)
        for d in NETWORK_SENSORS
        if coordinator.ptp_enabled or not d.ptp_only
    ]
    entities += [NtpSensor(coordinator, d, h) for h in coordinator.hosts for d in NTP_SENSORS]
    async_add_entities(entities)

    if not coordinator.ptp_enabled:
        return
    known: set[str] = set()

    @callback
    def _add_new_clocks() -> None:
        new = [cid for cid in coordinator.data.ptp_clocks if cid not in known]
        if not new:
            return
        known.update(new)
        async_add_entities(PtpSensor(coordinator, d, cid) for cid in new for d in PTP_SENSORS)

    _add_new_clocks()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_clocks))
