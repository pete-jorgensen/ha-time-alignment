"""The config flow, setup, and the entities a poll produces — inside a real HA core."""

from __future__ import annotations

from unittest.mock import patch

import ntplib
import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.time_alignment import ntp, ptp
from custom_components.time_alignment.const import DOMAIN


# Every fake host resolves to its own LAN (TEST-NET-1) address, so it gets the burst.
ADDRESSES: dict[str, str] = {}
NAMES: dict[str, str] = {}


def fake_resolve(host, port=123):
    if host not in ADDRESSES:
        ADDRESSES[host] = f"192.0.2.{len(ADDRESSES) + 1}"
        NAMES[ADDRESSES[host]] = host
    return ADDRESSES[host]


@pytest.fixture(autouse=True)
def fake_network():
    """No DNS, and no 2 s spacing between queries: both are for real servers."""
    with (
        patch("custom_components.time_alignment.ntp.resolve", side_effect=fake_resolve),
        patch("custom_components.time_alignment.ntp.NTP_SAMPLE_GAP", 0),
    ):
        yield


OFFSETS = {"ref.example": 0.040, "good.example": 0.041, "bad.example": 0.090}


def fake_query(address, port=123, timeout=2.0):
    host = NAMES[address]
    if host == "down.example":
        raise ntplib.NTPException("No response received from down.example.")
    return ntp.NtpSample(address, OFFSETS[host], 0.001, 1 if host == "ref.example" else 2,
                         0, "PPS", 0.0, 0.0005, 0.0)


FORM = {
    "ntp_hosts": "ref.example\ngood.example, bad.example\ndown.example",
    "reference": "ref.example",
    "tolerance_ms": 10,
    "scan_interval": 60,
    "ptp_enabled": False,
    "ptp_domain": 0,
    "ptp_interface": "",
    "ptp_tolerance_us": 1,
}


async def test_flow_rejects_a_reference_that_is_not_a_host(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**FORM, "reference": "elsewhere.example"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"reference": "reference_not_a_host"}


async def test_flow_needs_something_to_watch(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**FORM, "ntp_hosts": "", "reference": ""}
    )
    assert result["errors"] == {"base": "nothing_to_watch"}


async def test_setup_and_states(hass: HomeAssistant):
    with patch("custom_components.time_alignment.ntp.query", side_effect=fake_query):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], FORM)
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["options"]["ntp_hosts"] == [
            "ref.example", "good.example", "bad.example", "down.example"
        ]
        await hass.async_block_till_done()

    st = hass.states.get
    assert float(st("sensor.good_example_offset_from_reference").state) == 1.0
    assert float(st("sensor.bad_example_offset_from_reference").state) == 50.0
    assert float(st("sensor.good_example_offset").state) == 41.0
    assert st("sensor.ref_example_upstream_reference").state == "PPS"
    assert st("sensor.down_example_offset").state == "unavailable"

    assert st("binary_sensor.good_example_time_problem").state == "off"
    bad = st("binary_sensor.bad_example_time_problem")
    assert bad.state == "on" and bad.attributes["reasons"] == ["out_of_tolerance"]
    down = st("binary_sensor.down_example_time_problem")
    assert down.state == "on" and down.attributes["reasons"] == ["unreachable"]
    assert "No response" in down.attributes["error"]
    good = st("binary_sensor.good_example_time_problem")
    assert good.attributes["address"] == ADDRESSES["good.example"]

    assert float(st("sensor.network_time_offset_spread").state) == 50.0
    worst = st("sensor.network_time_worst_offset")
    assert float(worst.state) == 50.0 and worst.attributes["host"] == "bad.example"
    assert st("sensor.network_time_hosts_with_problems").state == "2"
    assert float(st("sensor.network_time_home_assistant_clock_offset").state) == -40.0
    assert st("binary_sensor.network_time_time_problem").state == "on"
    assert st("sensor.network_time_ptp_clocks_seen") is None  # PTP off


async def test_single_instance(hass: HomeAssistant):
    MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, options={}).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT


async def _setup(hass: HomeAssistant, options: dict) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


OPTIONS = {
    "ntp_hosts": ["down.example"],
    "reference": "",
    "tolerance_ms": 10.0,
    "scan_interval": 60,
    "ptp_enabled": False,
    "ptp_domain": 0,
    "ptp_interface": "",
    "ptp_tolerance_us": 1.0,
}


async def test_every_host_down_turns_problems_on(hass: HomeAssistant):
    """No host answering is a result, not a failed poll: the problem sensors stay
    available and turn ON, which is exactly when they are needed."""
    with patch("custom_components.time_alignment.ntp.query", side_effect=fake_query):
        await _setup(hass, OPTIONS)
    host = hass.states.get("binary_sensor.down_example_time_problem")
    assert host.state == "on" and host.attributes["reasons"] == ["unreachable"]
    assert hass.states.get("binary_sensor.network_time_time_problem").state == "on"
    assert hass.states.get("sensor.down_example_offset").state == "unavailable"


async def test_kiss_of_death_stops_the_burst(hass: HomeAssistant):
    calls = []

    def refuse(address, port=123, timeout=2.0):
        calls.append(NAMES[address])
        raise ntp.KissOfDeath(address, "RATE")

    with patch("custom_components.time_alignment.ntp.query", side_effect=refuse):
        await _setup(hass, {**OPTIONS, "ntp_hosts": ["busy.example"]})
    assert calls == ["busy.example"]  # one query, not three
    st = hass.states.get("binary_sensor.busy_example_time_problem")
    assert st.state == "on" and "RATE" in st.attributes["error"]


async def test_options_change_removes_a_dropped_host(hass: HomeAssistant):
    with patch("custom_components.time_alignment.ntp.query", side_effect=fake_query):
        entry = await _setup(
            hass, {**OPTIONS, "ntp_hosts": ["ref.example", "good.example"]}
        )
        assert hass.states.get("sensor.good_example_offset") is not None

        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {**FORM, "ntp_hosts": "ref.example", "reference": "ref.example"}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()

    assert entry.options["ntp_hosts"] == ["ref.example"]
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert sorted(d.name for d in devices) == ["Network time", "ref.example"]
    assert hass.states.get("sensor.good_example_offset") is None


def _clocks() -> dict[str, ptp.PtpClock]:
    gm = ptp.PtpClock(
        "aabbcc.fffe.000001",
        "192.0.2.101",
        parent={"grandmaster_identity": "aabbcc.fffe.000001", "grandmaster_clock_class": 6},
        ports={1: {"port_number": 1, "port_state": "master"}},
    )
    slave = ptp.PtpClock(
        "001122.fffe.334455",
        "192.0.2.102",
        current={"offset_from_master_ns": 2500.0, "mean_path_delay_ns": 800.0, "steps_removed": 1},
        parent={"grandmaster_identity": "aabbcc.fffe.000001"},
        ports={1: {"port_number": 1, "port_state": "slave"}},
    )
    return {gm.clock_identity: gm, slave.clock_identity: slave}


async def test_ptp_clocks_become_devices(hass: HomeAssistant):
    async def discover(**_kw):
        return _clocks()

    with patch("custom_components.time_alignment.ptp.async_discover", side_effect=discover):
        await _setup(hass, {**OPTIONS, "ntp_hosts": [], "ptp_enabled": True})

    st = hass.states.get
    assert st("sensor.network_time_ptp_clocks_seen").state == "2"
    assert float(st("sensor.network_time_ptp_worst_offset").state) == 2.5
    assert float(st("sensor.ptp_clock_192_0_2_102_offset_from_master").state) == 2.5
    assert st("sensor.ptp_clock_192_0_2_101_port_state").state == "master"
    slave = st("binary_sensor.ptp_clock_192_0_2_102_time_problem")
    assert slave.state == "on" and slave.attributes["reasons"] == ["offset_out_of_tolerance"]
    assert st("binary_sensor.ptp_clock_192_0_2_101_time_problem").state == "off"


async def test_ptp_bind_failure_is_reported(hass: HomeAssistant):
    async def refuse(**_kw):
        raise PermissionError(13, "Permission denied")

    with patch("custom_components.time_alignment.ptp.async_discover", side_effect=refuse):
        await _setup(hass, {**OPTIONS, "ntp_hosts": [], "ptp_enabled": True})
    seen = hass.states.get("sensor.network_time_ptp_clocks_seen")
    assert seen.state == "0" and "Permission denied" in seen.attributes["error"]
    net = hass.states.get("binary_sensor.network_time_time_problem")
    assert net.state == "on"


async def test_a_silent_clock_stays_a_problem_across_a_reload(hass: HomeAssistant):
    clocks = _clocks()

    async def discover(**_kw):
        return dict(clocks)

    with patch("custom_components.time_alignment.ptp.async_discover", side_effect=discover):
        entry = await _setup(hass, {**OPTIONS, "ntp_hosts": [], "ptp_enabled": True})
        clocks.pop("001122.fffe.334455")
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

    st = hass.states.get("binary_sensor.ptp_clock_192_0_2_102_time_problem")
    assert st.state == "on" and st.attributes["reasons"] == ["not_answering"]
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, f"{entry.entry_id}_ptp_001122.fffe.334455"), entry.entry_id
    )
    assert device.name == "PTP clock 192.0.2.102"


async def test_form_defaults_to_five_minutes(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    defaults = {str(k): k.default() for k in result["data_schema"].schema if callable(k.default)}
    assert defaults["scan_interval"] == 300
