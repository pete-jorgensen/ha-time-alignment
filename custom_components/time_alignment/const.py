"""Constants for Time Alignment."""

from __future__ import annotations

DOMAIN = "time_alignment"

CONF_NTP_HOSTS = "ntp_hosts"
CONF_REFERENCE = "reference"
CONF_TOLERANCE_MS = "tolerance_ms"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_PTP_ENABLED = "ptp_enabled"
CONF_PTP_DOMAIN = "ptp_domain"
CONF_PTP_INTERFACE = "ptp_interface"
CONF_PTP_TOLERANCE_US = "ptp_tolerance_us"

DEFAULT_TOLERANCE_MS = 10.0
DEFAULT_SCAN_INTERVAL = 300
DEFAULT_PTP_DOMAIN = 0
DEFAULT_PTP_TOLERANCE_US = 1.0

# Queries per LAN host per poll (a public server gets one: ntp.measure); the shortest
# round trip wins (ntp.best_of). They are spaced NTP_SAMPLE_GAP apart: ntpd's default
# rate limit ("restrict ... limited", minimum 2 s) answers closer queries with a
# kiss-o'-death, and chrony's own burst uses 2 s too.
NTP_SAMPLES = 3
NTP_SAMPLE_GAP = 2.0
NTP_TIMEOUT = 2.0
# Public NTP pools ask for no more than about one query a minute per client, and a public
# server gets one query per poll, so the poll may not be faster than a minute.
MIN_SCAN_INTERVAL = 60
PTP_LISTEN_WINDOW = 1.0

NETWORK_DEVICE_ID = "network"
