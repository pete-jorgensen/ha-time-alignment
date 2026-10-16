"""Config and options flow. One entry watches the whole network.

A single entry, not one per host, because the network-wide figures (offset against the
reference, spread) only mean something when every host is measured in the same poll.
"""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
)

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
    MIN_SCAN_INTERVAL,
)
from .coordinator import split_host

_SEPARATORS = re.compile(r"[\s,;]+")


def parse_hosts(text: str) -> list[str]:
    """Hosts typed one per line or comma-separated; duplicates dropped, order kept."""
    seen: dict[str, None] = {}
    for item in _SEPARATORS.split(text or ""):
        if item:
            seen.setdefault(item, None)
    return list(seen)


def _schema(defaults: dict[str, Any]) -> vol.Schema:
    num = lambda lo, hi, step: NumberSelector(  # noqa: E731
        NumberSelectorConfig(min=lo, max=hi, step=step, mode=NumberSelectorMode.BOX)
    )
    return vol.Schema(
        {
            vol.Optional(
                CONF_NTP_HOSTS, default="\n".join(defaults.get(CONF_NTP_HOSTS, []))
            ): TextSelector(TextSelectorConfig(multiline=True)),
            vol.Optional(
                CONF_REFERENCE, default=defaults.get(CONF_REFERENCE, "")
            ): TextSelector(),
            vol.Required(
                CONF_TOLERANCE_MS,
                default=defaults.get(CONF_TOLERANCE_MS, DEFAULT_TOLERANCE_MS),
            ): num(0.001, 10000, 0.001),
            vol.Required(
                CONF_SCAN_INTERVAL,
                default=defaults.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            ): num(MIN_SCAN_INTERVAL, 3600, 1),
            vol.Required(
                CONF_PTP_ENABLED, default=defaults.get(CONF_PTP_ENABLED, False)
            ): BooleanSelector(),
            vol.Required(
                CONF_PTP_DOMAIN,
                default=defaults.get(CONF_PTP_DOMAIN, DEFAULT_PTP_DOMAIN),
            ): num(0, 255, 1),
            vol.Optional(
                CONF_PTP_INTERFACE, default=defaults.get(CONF_PTP_INTERFACE, "")
            ): TextSelector(),
            vol.Required(
                CONF_PTP_TOLERANCE_US,
                default=defaults.get(CONF_PTP_TOLERANCE_US, DEFAULT_PTP_TOLERANCE_US),
            ): num(0.001, 1000000, 0.001),
        }
    )


def _validate(user_input: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Normalise the form. Hosts are not probed here: a host that is down today is
    still one to watch, and its being down is exactly what the sensors will show."""
    errors: dict[str, str] = {}
    hosts = parse_hosts(user_input.get(CONF_NTP_HOSTS, ""))
    for host in hosts:
        try:
            split_host(host)
        except ValueError:
            errors[CONF_NTP_HOSTS] = "bad_host"
    reference = (user_input.get(CONF_REFERENCE) or "").strip()
    if reference and reference not in hosts:
        errors[CONF_REFERENCE] = "reference_not_a_host"
    ptp_enabled = bool(user_input.get(CONF_PTP_ENABLED))
    if not hosts and not ptp_enabled:
        errors["base"] = "nothing_to_watch"
    options = {
        CONF_NTP_HOSTS: hosts,
        CONF_REFERENCE: reference,
        CONF_TOLERANCE_MS: float(user_input[CONF_TOLERANCE_MS]),
        CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
        CONF_PTP_ENABLED: ptp_enabled,
        CONF_PTP_DOMAIN: int(user_input[CONF_PTP_DOMAIN]),
        CONF_PTP_INTERFACE: (user_input.get(CONF_PTP_INTERFACE) or "").strip(),
        CONF_PTP_TOLERANCE_US: float(user_input[CONF_PTP_TOLERANCE_US]),
    }
    return options, errors


class TimeAlignmentConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        errors: dict[str, str] = {}
        defaults: dict[str, Any] = {}
        if user_input is not None:
            options, errors = _validate(user_input)
            if not errors:
                return self.async_create_entry(
                    title="Network time alignment", data={}, options=options
                )
            defaults = options
        return self.async_show_form(
            step_id="user", data_schema=_schema(defaults), errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> TimeAlignmentOptionsFlow:
        return TimeAlignmentOptionsFlow()


class TimeAlignmentOptionsFlow(OptionsFlowWithReload):
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        defaults: dict[str, Any] = dict(self.config_entry.options)
        if user_input is not None:
            options, errors = _validate(user_input)
            if not errors:
                return self.async_create_entry(data=options)
            defaults = options
        return self.async_show_form(
            step_id="init", data_schema=_schema(defaults), errors=errors
        )
