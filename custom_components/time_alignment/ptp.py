"""PTP (IEEE 1588) management client — ask every PTP clock on a segment how it is doing.

No Home Assistant import here, so the wire format is testable on its own.

How it works
------------
A PTP management GET sent to the PTP multicast group with a *wildcard* target port
identity is answered by every PTP clock that hears it (``ptp4l`` and most hardware
clocks). So one request discovers every clock on the segment; nothing has to be
configured per host. This is the same message ``pmc -4 'GET CURRENT_DATA_SET'`` sends.

Wire facts, as linuxptp implements them (``msg.h``, ``tlv.h``, ``ds.h``,
``pmc_common.c``, ``port.c``):

- Everything is big-endian. The common header is 34 bytes; a management message adds a
  10-byte target port identity, two boundary-hop bytes, the action byte, and a reserved
  byte (48 total), then one management TLV: type 0x0001, length, management id, data.
- ``ptp4l`` sends its RESPONSE to the **multicast group** (port.c builds the reply
  without the UNICAST flag), so a client must bind UDP 320 and join 224.0.1.129 to hear
  it — a plain unicast socket hears nothing.
- A GET carries a zero-filled data set of the right size (pmc's default; ``-z`` sends
  length 2 instead, which ptp4l also accepts).
- TimeInterval fields are nanoseconds scaled by 2**16.
- ptp4l ignores a message whose domain number is not its own, so the domain must match.
"""

from __future__ import annotations

import asyncio
import os
import socket
import struct
from dataclasses import dataclass, field

PTP_MCAST_GROUP = "224.0.1.129"
PTP_GENERAL_PORT = 320

MSG_MANAGEMENT = 0x0D
PTP_VERSION = 0x12  # minor 1, major 2 — what linuxptp sends
TLV_MANAGEMENT = 0x0001
TLV_MANAGEMENT_ERROR_STATUS = 0x0002

ACTION_GET = 0
ACTION_RESPONSE = 2

MID_DEFAULT_DATA_SET = 0x2000
MID_CURRENT_DATA_SET = 0x2001
MID_PARENT_DATA_SET = 0x2002
MID_PORT_DATA_SET = 0x2004
MID_TIME_STATUS_NP = 0xC000  # linuxptp extension; other clocks answer with an error TLV

# Size of each data set on the wire, which is also the zero-fill a GET carries.
DATASET_LEN = {
    MID_DEFAULT_DATA_SET: 20,
    MID_CURRENT_DATA_SET: 18,
    MID_PARENT_DATA_SET: 32,
    MID_PORT_DATA_SET: 26,
    MID_TIME_STATUS_NP: 50,
}

QUERY_IDS = (
    MID_DEFAULT_DATA_SET,
    MID_CURRENT_DATA_SET,
    MID_PARENT_DATA_SET,
    MID_PORT_DATA_SET,
    MID_TIME_STATUS_NP,
)

WILDCARD_PORT_IDENTITY = b"\xff" * 10

PORT_STATES = {
    1: "initializing",
    2: "faulty",
    3: "disabled",
    4: "listening",
    5: "pre_master",
    6: "master",
    7: "passive",
    8: "uncalibrated",
    9: "slave",
}

_HEADER = struct.Struct(">BBHBB2sqI10sHBb")  # 34 bytes
_MGMT = struct.Struct(">10sBBBB")  # 14 bytes after the header
_TLV = struct.Struct(">HHH")


class PtpError(Exception):
    """A malformed or unusable PTP message."""


def clock_identity_text(raw: bytes) -> str:
    """Format a clock identity the way pmc prints it: ``001122.fffe.334455``."""
    h = raw.hex()
    return f"{h[0:6]}.{h[6:10]}.{h[10:16]}"


def scaled_ns(value: int) -> float:
    """TimeInterval (ns * 2**16) to nanoseconds."""
    return value / 65536.0


def build_get(
    mid: int,
    sequence_id: int,
    source_port_identity: bytes,
    domain: int = 0,
    target_port_identity: bytes = WILDCARD_PORT_IDENTITY,
    boundary_hops: int = 0,
) -> bytes:
    """Encode one management GET for ``mid``."""
    if len(source_port_identity) != 10 or len(target_port_identity) != 10:
        raise ValueError("port identities are 10 bytes")
    datalen = DATASET_LEN.get(mid, 0)
    datalen += datalen % 2
    length = _HEADER.size + _MGMT.size + _TLV.size + datalen
    header = _HEADER.pack(
        MSG_MANAGEMENT,
        PTP_VERSION,
        length,
        domain,
        0,
        b"\x00\x00",
        0,
        0,
        source_port_identity,
        sequence_id & 0xFFFF,
        0,
        0x7F,
    )
    mgmt = _MGMT.pack(target_port_identity, boundary_hops, boundary_hops, ACTION_GET, 0)
    tlv = _TLV.pack(TLV_MANAGEMENT, 2 + datalen, mid)
    return header + mgmt + tlv + bytes(datalen)


@dataclass(frozen=True)
class ManagementResponse:
    """One decoded RESPONSE (or management error) from one clock port."""

    clock_identity: str
    port_number: int
    sequence_id: int
    domain: int
    target_port_identity: bytes
    mid: int
    data: bytes
    error: int | None = None


def parse_message(packet: bytes) -> ManagementResponse | None:
    """Decode a management RESPONSE. Anything else (our own looped-back GET, a Sync,
    an Announce) returns None rather than raising: the socket hears all of it."""
    if len(packet) < _HEADER.size + _MGMT.size + _TLV.size:
        return None
    (tsmt, ver, length, domain, _r1, _flags, _corr, _r2, src, seq, _ctl, _lmi) = (
        _HEADER.unpack_from(packet, 0)
    )
    if tsmt & 0x0F != MSG_MANAGEMENT or ver & 0x0F != 2:
        return None
    if length > len(packet):
        raise PtpError(f"truncated: header says {length} bytes, got {len(packet)}")
    # A declared length shorter than the fixed parts would make the unpacks below fail.
    if length < _HEADER.size + _MGMT.size + _TLV.size:
        raise PtpError(f"declared length {length} is shorter than a management message")
    # Ethernet padding can follow the message; only the declared length is ours.
    packet = packet[:length]
    target, _start, _hops, action, _res = _MGMT.unpack_from(packet, _HEADER.size)
    if action & 0x0F != ACTION_RESPONSE:
        return None
    off = _HEADER.size + _MGMT.size
    tlv_type, tlv_len, first = _TLV.unpack_from(packet, off)
    clock = clock_identity_text(src[:8])
    port = int.from_bytes(src[8:10], "big")
    if tlv_type == TLV_MANAGEMENT_ERROR_STATUS:
        # error TLV: type, length, errorId, managementId, reserved[4]
        if off + 8 > length:
            raise PtpError("short management error TLV")
        (mid,) = struct.unpack_from(">H", packet, off + 6)
        return ManagementResponse(clock, port, seq, domain, target, mid, b"", error=first)
    if tlv_type != TLV_MANAGEMENT:
        return None
    data = packet[off + _TLV.size : off + 4 + tlv_len]
    if len(data) != tlv_len - 2:
        raise PtpError("management TLV runs past the end of the message")
    return ManagementResponse(clock, port, seq, domain, target, first, data)


# --- data sets ----------------------------------------------------------------------


def decode_default_ds(data: bytes) -> dict:
    flags, _r, ports, prio1, cclass, cacc, cvar, prio2, cid, dom = struct.unpack_from(
        ">BBHBBBHB8sB", data
    )
    return {
        "two_step": bool(flags & 0x01),
        "slave_only": bool(flags & 0x02),
        "number_ports": ports,
        "priority1": prio1,
        "clock_class": cclass,
        "clock_accuracy": cacc,
        "priority2": prio2,
        "clock_identity": clock_identity_text(cid),
        "domain": dom,
    }


def decode_current_ds(data: bytes) -> dict:
    steps, offset, delay = struct.unpack_from(">Hqq", data)
    return {
        "steps_removed": steps,
        "offset_from_master_ns": scaled_ns(offset),
        "mean_path_delay_ns": scaled_ns(delay),
    }


def decode_parent_ds(data: bytes) -> dict:
    (ppid, _stats, _r, _var, _rate, gp1, gclass, gacc, _gvar, gp2, gmid) = (
        struct.unpack_from(">10sBBHiBBBHB8s", data)
    )
    return {
        "parent_clock_identity": clock_identity_text(ppid[:8]),
        "parent_port": int.from_bytes(ppid[8:], "big"),
        "grandmaster_identity": clock_identity_text(gmid),
        "grandmaster_priority1": gp1,
        "grandmaster_priority2": gp2,
        "grandmaster_clock_class": gclass,
        "grandmaster_clock_accuracy": gacc,
    }


def decode_port_ds(data: bytes) -> dict:
    pid, state, _mdri, peer_delay = struct.unpack_from(">10sBbq", data)
    return {
        "port_number": int.from_bytes(pid[8:], "big"),
        "port_state": PORT_STATES.get(state, f"unknown_{state}"),
        "peer_mean_path_delay_ns": scaled_ns(peer_delay),
    }


def decode_time_status_np(data: bytes) -> dict:
    master_offset, _ingress, _csro, _slgpc, _gtbi = struct.unpack_from(">qqiiH", data)
    gm_present, gmid = struct.unpack_from(">i8s", data, 8 + 8 + 4 + 4 + 2 + 12)
    return {
        "master_offset_ns": float(master_offset),
        "gm_present": bool(gm_present),
        "grandmaster_identity": clock_identity_text(gmid),
    }


_DECODERS = {
    MID_DEFAULT_DATA_SET: decode_default_ds,
    MID_CURRENT_DATA_SET: decode_current_ds,
    MID_PARENT_DATA_SET: decode_parent_ds,
    MID_PORT_DATA_SET: decode_port_ds,
    MID_TIME_STATUS_NP: decode_time_status_np,
}


@dataclass
class PtpClock:
    """Everything one PTP clock answered, merged across the data sets."""

    clock_identity: str
    source_address: str | None = None
    default: dict = field(default_factory=dict)
    current: dict = field(default_factory=dict)
    parent: dict = field(default_factory=dict)
    time_status: dict = field(default_factory=dict)
    ports: dict[int, dict] = field(default_factory=dict)

    @property
    def port_state(self) -> str | None:
        """The clock's role: slave if any port is a slave, else master if any is, else
        the first port's state. A boundary clock has both; slave is the one that says
        whether it is being disciplined."""
        states = [p["port_state"] for p in self.ports.values()]
        for wanted in ("slave", "uncalibrated", "master"):
            if wanted in states:
                return wanted
        return states[0] if states else None

    @property
    def is_grandmaster(self) -> bool:
        gm = self.parent.get("grandmaster_identity")
        return gm is not None and gm == self.clock_identity

    @property
    def offset_ns(self) -> float | None:
        """Offset from master. TIME_STATUS_NP's is the last measured value; CURRENT
        DATA SET's is the same thing in the standard's units, so prefer neither
        silently — use the standard one, fall back to the linuxptp one."""
        if "offset_from_master_ns" in self.current:
            return self.current["offset_from_master_ns"]
        return self.time_status.get("master_offset_ns")


def merge_responses(
    responses: list[tuple[ManagementResponse, str | None]],
) -> dict[str, PtpClock]:
    """Fold individual responses into one record per clock identity."""
    clocks: dict[str, PtpClock] = {}
    for resp, addr in responses:
        clock = clocks.setdefault(resp.clock_identity, PtpClock(resp.clock_identity))
        if addr and clock.source_address is None:
            clock.source_address = addr
        if resp.error is not None:
            continue
        decoder = _DECODERS.get(resp.mid)
        if decoder is None:
            continue
        try:
            decoded = decoder(resp.data)
        except struct.error:
            continue
        if resp.mid == MID_DEFAULT_DATA_SET:
            clock.default = decoded
        elif resp.mid == MID_CURRENT_DATA_SET:
            clock.current = decoded
        elif resp.mid == MID_PARENT_DATA_SET:
            clock.parent = decoded
        elif resp.mid == MID_TIME_STATUS_NP:
            clock.time_status = decoded
        elif resp.mid == MID_PORT_DATA_SET:
            clock.ports[decoded["port_number"]] = decoded
    return clocks


# --- the network side ---------------------------------------------------------------


def _make_socket(interface_address: str | None) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        # SO_REUSEADDR lets this bind beside a ptp4l on the same host, which sets it too
        # (linuxptp udp.c). ptp4l does not set SO_REUSEPORT, so that one only helps
        # against another instance of this client.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.bind(("", PTP_GENERAL_PORT))
        iface = socket.inet_aton(interface_address or "0.0.0.0")
        mreq = socket.inet_aton(PTP_MCAST_GROUP) + iface
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        if interface_address:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, iface)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        sock.setblocking(False)
    except BaseException:
        sock.close()
        raise
    return sock


class _Collector(asyncio.DatagramProtocol):
    def __init__(self, sequence_ids: set[int], source: bytes) -> None:
        self.sequence_ids = sequence_ids
        self.source = source
        self.responses: list[tuple[ManagementResponse, str | None]] = []

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            resp = parse_message(data)
        except PtpError:
            return
        if resp is None or resp.sequence_id not in self.sequence_ids:
            return
        # A reply is addressed to the requester's port identity; another client's
        # replies share the multicast group with ours.
        if resp.target_port_identity not in (self.source, WILDCARD_PORT_IDENTITY):
            return
        self.responses.append((resp, addr[0]))


async def async_discover(
    domain: int = 0,
    interface_address: str | None = None,
    window: float = 1.0,
    destinations: tuple[str, ...] = (PTP_MCAST_GROUP,),
) -> dict[str, PtpClock]:
    """Ask every PTP clock that can hear us for its data sets.

    ``destinations`` defaults to the multicast group; unicast addresses can be added
    for a clock on another segment (its reply still comes back multicast, so it is only
    heard if the segment is shared — a routed clock needs a local relay)."""
    loop = asyncio.get_running_loop()
    sock = _make_socket(interface_address)
    # Our own port identity: a random clock identity, port 1. Random so two instances
    # (or a pmc on the same host) never claim each other's replies.
    source = os.urandom(8) + b"\x00\x01"
    first_seq = int.from_bytes(os.urandom(2), "big")
    seqs = {(first_seq + i) & 0xFFFF for i in range(len(QUERY_IDS))}
    try:
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: _Collector(seqs, source), sock=sock
        )
    except BaseException:
        sock.close()
        raise
    try:
        for i, mid in enumerate(QUERY_IDS):
            msg = build_get(mid, (first_seq + i) & 0xFFFF, source, domain)
            for dest in destinations:
                transport.sendto(msg, (dest, PTP_GENERAL_PORT))
        await asyncio.sleep(window)
    finally:
        transport.close()
    return merge_responses(protocol.responses)


# --- what counts as a problem --------------------------------------------------------

_BAD_STATES = {"initializing", "faulty", "disabled", "listening", "uncalibrated"}


def problems(clock: PtpClock, tolerance_ns: float) -> list[str]:
    """Why this clock is not well aligned; empty when it is.

    Every port's state is judged, not just the one :attr:`PtpClock.port_state` shows: a
    boundary clock with one faulty port and one master port is still faulty. A
    grandmaster (only ``master`` ports) has no offset to judge — it *is* the reference.
    A clock with a ``slave`` port is judged on its offset and on whether it can still see
    a grandmaster."""
    if not clock.ports:
        return ["no_port_data"]
    reasons = [
        f"port_{number}_{port['port_state']}"
        for number, port in sorted(clock.ports.items())
        if port["port_state"] in _BAD_STATES or port["port_state"].startswith("unknown_")
    ]
    if clock.port_state == "slave":
        offset = clock.offset_ns
        if offset is not None and abs(offset) > tolerance_ns:
            reasons.append("offset_out_of_tolerance")
        if clock.time_status and not clock.time_status.get("gm_present", True):
            reasons.append("no_grandmaster")
    return reasons


def network_problems(clocks: dict[str, PtpClock]) -> list[str]:
    """Problems no single clock can see. One domain on one segment should agree on one
    grandmaster; two means the segment has split (a clock lost its upstream and made
    itself grandmaster to the clocks behind it, or two grandmasters cannot hear each
    other), and the two halves drift apart."""
    grandmasters = sorted(
        {c.parent["grandmaster_identity"] for c in clocks.values() if c.parent}
    )
    if len(grandmasters) > 1:
        return [f"multiple_grandmasters: {', '.join(grandmasters)}"]
    return []
