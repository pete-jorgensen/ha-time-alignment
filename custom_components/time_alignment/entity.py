"""Three kinds of device: the network as a whole, each NTP host, each PTP clock."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, NETWORK_DEVICE_ID
from .coordinator import TimeAlignmentCoordinator


def network_device_id(entry_id: str) -> str:
    return f"{entry_id}_{NETWORK_DEVICE_ID}"


def ntp_device_id(entry_id: str, host: str) -> str:
    return f"{entry_id}_ntp_{host}"


def ptp_device_id(entry_id: str, clock_id: str) -> str:
    return f"{entry_id}_ptp_{clock_id}"


class TimeAlignmentEntity(CoordinatorEntity[TimeAlignmentCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: TimeAlignmentCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._entry_id = coordinator.config_entry.entry_id
        self._attr_translation_key = key

    def _network_identifier(self) -> tuple[str, str]:
        return (DOMAIN, network_device_id(self._entry_id))

    def _child_device_info(self, **info) -> DeviceInfo:
        """A host or clock device, linked under the network device. HA 2026.8+ takes
        the parent's registry id (``via_device_id``), not its identifier."""
        return DeviceInfo(
            manufacturer="Time Alignment",
            via_device_id=self.coordinator.network_device_registry_id,
            **info,
        )


class NetworkEntity(TimeAlignmentEntity):
    def __init__(self, coordinator: TimeAlignmentCoordinator, key: str) -> None:
        super().__init__(coordinator, key)
        self._attr_unique_id = f"{self._entry_id}_{NETWORK_DEVICE_ID}_{key}"
        # The device itself is registered (name, model) in async_setup_entry.
        self._attr_device_info = DeviceInfo(identifiers={self._network_identifier()})


class NtpHostEntity(TimeAlignmentEntity):
    def __init__(self, coordinator: TimeAlignmentCoordinator, key: str, host: str) -> None:
        super().__init__(coordinator, key)
        self.host = host
        self._attr_unique_id = f"{self._entry_id}_ntp_{host}_{key}"
        self._attr_device_info = self._child_device_info(
            identifiers={(DOMAIN, ntp_device_id(self._entry_id, host))},
            name=host,
            model="NTP server",
        )

    @property
    def available(self) -> bool:
        return super().available and self.host in self.coordinator.data.ntp_samples


class PtpClockEntity(TimeAlignmentEntity):
    def __init__(
        self, coordinator: TimeAlignmentCoordinator, key: str, clock_id: str
    ) -> None:
        super().__init__(coordinator, key)
        self.clock_id = clock_id
        self._attr_unique_id = f"{self._entry_id}_ptp_{clock_id}_{key}"
        identifiers = {(DOMAIN, ptp_device_id(self._entry_id, clock_id))}
        clock = coordinator.data.ptp_clocks.get(clock_id)
        if clock is None:
            # A clock remembered from an earlier run that is not answering now: attach
            # to its existing device without renaming it.
            self._attr_device_info = DeviceInfo(identifiers=identifiers)
            return
        self._attr_device_info = self._child_device_info(
            identifiers=identifiers,
            name=f"PTP clock {clock.source_address or clock_id}",
            model="PTP clock",
            serial_number=clock_id,
        )

    @property
    def available(self) -> bool:
        return super().available and self.clock_id in self.coordinator.data.ptp_clocks
