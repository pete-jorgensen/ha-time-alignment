"""Time Alignment: how far apart are the clocks on this network?"""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .coordinator import TimeAlignmentConfigEntry, TimeAlignmentCoordinator
from .entity import network_device_id, ntp_device_id, ptp_device_id

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: TimeAlignmentConfigEntry) -> bool:
    coordinator = TimeAlignmentCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    # Registered before any entity, so host and clock devices can name it as parent.
    coordinator.network_device_registry_id = (
        dr.async_get(hass)
        .async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, network_device_id(entry.entry_id))},
            name="Network time",
            manufacturer="Time Alignment",
            model="Network",
        )
        .id
    )
    _remove_dropped_hosts(hass, entry, coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TimeAlignmentConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


def _remove_dropped_hosts(
    hass: HomeAssistant, entry: TimeAlignmentConfigEntry, coordinator: TimeAlignmentCoordinator
) -> None:
    """A host taken out of the options goes, with its entities, and so does every PTP
    clock once PTP is turned off. With PTP on, clocks are left for the user to delete
    (below): one that stopped answering is a problem to see, not to tidy away."""
    keep_ptp = coordinator.ptp_enabled
    wanted = {ntp_device_id(entry.entry_id, h) for h in coordinator.hosts}
    wanted.add(network_device_id(entry.entry_id))
    registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        ids = {i for d, i in device.identifiers if d == DOMAIN}
        if ids & wanted:
            continue
        if keep_ptp and any(i.startswith(ptp_device_id(entry.entry_id, "")) for i in ids):
            continue
        registry.async_remove_device(device.id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: TimeAlignmentConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Let the user delete a PTP clock that has gone away. Anything still configured or
    still answering would only come back on the next poll, so it stays."""
    coordinator = entry.runtime_data
    current = {network_device_id(entry.entry_id)}
    current |= {ntp_device_id(entry.entry_id, h) for h in coordinator.hosts}
    current |= {ptp_device_id(entry.entry_id, c) for c in coordinator.data.ptp_clocks}
    return not any(d == DOMAIN and i in current for d, i in device.identifiers)
