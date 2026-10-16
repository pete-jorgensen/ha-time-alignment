"""PTP management wire format.

The expected bytes here are written out field by field from linuxptp's structs
(msg.h ``ptp_header`` / ``management_msg``, tlv.h ``management_tlv``, ds.h) — not
produced by the encoder under test — so a wrong struct format cannot pass by agreeing
with itself.
"""

from __future__ import annotations

import asyncio
import socket
import struct

import pytest

from custom_components.time_alignment import ptp

SRC = bytes.fromhex("0011223344556677") + b"\x00\x01"
GM = bytes.fromhex("aabbccfffe000001")
SLAVE = bytes.fromhex("001122fffe334455")


def header(msg_len: int, seq: int, src: bytes, domain: int = 0) -> bytes:
    return (
        bytes([0x0D, 0x12])  # tsmt: management; ver: 2.1
        + msg_len.to_bytes(2, "big")
        + bytes([domain, 0])  # domainNumber, reserved1
        + b"\x00\x00"  # flagField
        + b"\x00" * 8  # correction
        + b"\x00" * 4  # reserved2
        + src  # sourcePortIdentity
        + seq.to_bytes(2, "big")
        + bytes([0x00, 0x7F])  # control, logMessageInterval
    )


def test_get_current_data_set_matches_pmc_layout():
    got = ptp.build_get(ptp.MID_CURRENT_DATA_SET, 0x1234, SRC, domain=0)
    want = (
        header(72, 0x1234, SRC)
        + b"\xff" * 10  # targetPortIdentity: wildcard
        + bytes([0, 0])  # startingBoundaryHops, boundaryHops
        + bytes([0x00, 0x00])  # action GET, reserved
        + bytes.fromhex("0001")  # TLV_MANAGEMENT
        + (2 + 18).to_bytes(2, "big")
        + bytes.fromhex("2001")
        + b"\x00" * 18  # zero-filled currentDS, as pmc sends
    )
    assert got == want
    assert len(got) == 72


def test_get_pads_odd_dataset_to_even():
    # Every queried dataset is even-sized; the padding rule is pmc's (len + len % 2).
    msg = ptp.build_get(ptp.MID_TIME_STATUS_NP, 1, SRC, domain=24)
    assert msg[4] == 24
    assert struct.unpack_from(">H", msg, 50)[0] == 2 + 50


def response(mid: int, data: bytes, src: bytes, seq: int, target: bytes = SRC) -> bytes:
    tlv = bytes.fromhex("0001") + (2 + len(data)).to_bytes(2, "big") + mid.to_bytes(2, "big") + data
    body = target + bytes([0, 0, 0x02, 0])  # action RESPONSE
    return header(34 + len(body) + len(tlv), seq, src) + body + tlv


def current_ds(steps: int, offset_ns: float, delay_ns: float) -> bytes:
    return struct.pack(">Hqq", steps, int(offset_ns * 65536), int(delay_ns * 65536))


def parent_ds(parent: bytes, gm: bytes, clock_class: int = 6) -> bytes:
    return (
        parent  # parentPortIdentity (10)
        + bytes([0, 0])  # parentStats, reserved
        + b"\xff\xff"  # observedParentOffsetScaledLogVariance
        + b"\x7f\xff\xff\xff"  # observedParentClockPhaseChangeRate
        + bytes([128])  # gm priority1
        + bytes([clock_class, 0x21]) + b"\x4e\x5d"  # gm clockQuality
        + bytes([128])  # gm priority2
        + gm
    )


def port_ds(port_identity: bytes, state: int) -> bytes:
    return port_identity + bytes([state, 0]) + b"\x00" * 8 + bytes([1, 3, 0, 1, 0, 2])


def time_status(master_offset: int, gm_present: bool, gm: bytes) -> bytes:
    return (
        struct.pack(">qqiiH", master_offset, 0, 0, 0, 0)
        + b"\x00" * 12  # lastGmPhaseChange
        + struct.pack(">i", int(gm_present))
        + gm
    )


def test_parse_current_data_set():
    pkt = response(
        ptp.MID_CURRENT_DATA_SET, current_ds(1, -1234.5, 5678.25), SLAVE + b"\x00\x01", 7
    )
    r = ptp.parse_message(pkt)
    assert r is not None
    assert (r.clock_identity, r.port_number, r.sequence_id) == ("001122.fffe.334455", 1, 7)
    assert ptp.decode_current_ds(r.data) == {
        "steps_removed": 1,
        "offset_from_master_ns": -1234.5,
        "mean_path_delay_ns": 5678.25,
    }


def test_parse_ignores_our_own_get_and_non_management():
    assert ptp.parse_message(ptp.build_get(ptp.MID_CURRENT_DATA_SET, 1, SRC)) is None
    sync = bytearray(header(44, 1, SLAVE + b"\x00\x01"))
    sync[0] = 0x00
    assert ptp.parse_message(bytes(sync) + b"\x00" * 10) is None
    assert ptp.parse_message(b"\x0d\x12") is None


def test_parse_error_status():
    tlv = (
        bytes.fromhex("0002")  # MANAGEMENT_ERROR_STATUS
        + (8).to_bytes(2, "big")
        + bytes.fromhex("0002")  # errorId
        + bytes.fromhex("c000")  # managementId
        + b"\x00" * 4
    )
    body = SRC + bytes([0, 0, 0x02, 0])
    pkt = header(34 + len(body) + len(tlv), 3, GM + b"\x00\x01") + body + tlv
    r = ptp.parse_message(pkt)
    assert r is not None and r.error == 2 and r.mid == ptp.MID_TIME_STATUS_NP


def test_truncated_is_an_error():
    pkt = response(ptp.MID_CURRENT_DATA_SET, current_ds(1, 0, 0), SLAVE + b"\x00\x01", 7)
    with pytest.raises(ptp.PtpError):
        ptp.parse_message(pkt[:-4])


def _segment():
    """A grandmaster and one slave, as ptp4l would answer the five GETs."""
    gm_pid, sl_pid = GM + b"\x00\x01", SLAVE + b"\x00\x01"
    return [
        (ptp.MID_CURRENT_DATA_SET, current_ds(0, 0, 0), gm_pid),
        (ptp.MID_PARENT_DATA_SET, parent_ds(gm_pid, GM), gm_pid),
        (ptp.MID_PORT_DATA_SET, port_ds(gm_pid, 6), gm_pid),
        (ptp.MID_CURRENT_DATA_SET, current_ds(1, 2500.0, 800.0), sl_pid),
        (ptp.MID_PARENT_DATA_SET, parent_ds(gm_pid, GM), sl_pid),
        (ptp.MID_PORT_DATA_SET, port_ds(sl_pid, 9), sl_pid),
        (ptp.MID_TIME_STATUS_NP, time_status(2500, True, GM), sl_pid),
    ]


def test_merge_and_problems():
    resps = [
        (ptp.parse_message(response(mid, d, src, 1)), "192.0.2.9") for mid, d, src in _segment()
    ]
    clocks = ptp.merge_responses(resps)
    gm = clocks["aabbcc.fffe.000001"]
    slave = clocks["001122.fffe.334455"]
    assert gm.is_grandmaster and gm.port_state == "master"
    assert slave.port_state == "slave" and slave.offset_ns == 2500.0
    assert slave.parent["grandmaster_identity"] == "aabbcc.fffe.000001"
    assert ptp.problems(gm, 1000) == []
    assert ptp.problems(slave, 1000) == ["offset_out_of_tolerance"]
    assert ptp.problems(slave, 5000) == []


def test_problem_states():
    c = ptp.PtpClock("x", ports={1: {"port_number": 1, "port_state": "listening"}})
    assert ptp.problems(c, 1000) == ["port_1_listening"]
    c = ptp.PtpClock("x", ports={1: {"port_number": 1, "port_state": "slave"}},
                     time_status={"gm_present": False})
    assert ptp.problems(c, 1000) == ["no_grandmaster"]
    assert ptp.problems(ptp.PtpClock("x"), 1000) == ["no_port_data"]


def test_boundary_clock_with_a_faulty_port_is_a_problem():
    # port_state shows "master", but the faulty port must not hide behind it.
    c = ptp.PtpClock("x", ports={
        1: {"port_number": 1, "port_state": "faulty"},
        2: {"port_number": 2, "port_state": "master"},
    })
    assert c.port_state == "master"
    assert ptp.problems(c, 1000) == ["port_1_faulty"]


def test_two_grandmasters_is_a_split_segment():
    clocks = ptp.merge_responses(
        [(ptp.parse_message(response(mid, d, src, 1)), None) for mid, d, src in _segment()]
    )
    assert ptp.network_problems(clocks) == []
    clocks["ddeeff.fffe.000002"] = ptp.PtpClock(
        "ddeeff.fffe.000002", parent={"grandmaster_identity": "ddeeff.fffe.000002"}
    )
    assert ptp.network_problems(clocks) == [
        "multiple_grandmasters: aabbcc.fffe.000001, ddeeff.fffe.000002"
    ]


def test_trailing_padding_is_ignored():
    pkt = response(ptp.MID_CURRENT_DATA_SET, current_ds(1, 0, 0), SLAVE + b"\x00\x01", 7)
    assert ptp.parse_message(pkt + b"\x00" * 16) == ptp.parse_message(pkt)


def test_tlv_may_not_run_into_padding():
    # The TLV claims 4 more bytes than the message holds; Ethernet padding supplies them.
    pkt = bytearray(
        response(ptp.MID_CURRENT_DATA_SET, current_ds(1, 0, 0), SLAVE + b"\x00\x01", 7)
    )
    at = 34 + 14 + 2  # the TLV's length field
    pkt[at : at + 2] = (int.from_bytes(pkt[at : at + 2], "big") + 4).to_bytes(2, "big")
    with pytest.raises(ptp.PtpError):
        ptp.parse_message(bytes(pkt) + b"\x00" * 4)


def test_boundary_clock_reports_its_slave_side():
    c = ptp.PtpClock("x", ports={
        1: {"port_number": 1, "port_state": "master"},
        2: {"port_number": 2, "port_state": "slave"},
    })
    assert c.port_state == "slave"


@pytest.mark.usefixtures("real_sockets")
async def test_discover_against_a_responder(monkeypatch):
    """End to end over real sockets: a fake ptp4l that answers each GET the way
    port.c does — to the multicast group, not to the sender. Uses a high port so it runs
    unprivileged; skipped where the host has no multicast loopback."""
    port = 43200
    monkeypatch.setattr(ptp, "PTP_GENERAL_PORT", port)
    answers = {mid: (d, src) for mid, d, src in _segment() if src[:8] == SLAVE}

    try:
        responder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        responder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        responder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        responder.bind(("", port))
        responder.setsockopt(
            socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
            socket.inet_aton(ptp.PTP_MCAST_GROUP) + socket.inet_aton("127.0.0.1"),
        )
        responder.setsockopt(
            socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton("127.0.0.1")
        )
        responder.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        responder.setblocking(False)
    except OSError as err:
        pytest.skip(f"no multicast on loopback here: {err}")

    loop = asyncio.get_running_loop()

    class Fake(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.t = transport

        def datagram_received(self, data, addr):
            if data[0] & 0x0F != 0x0D or data[46] & 0x0F != 0:
                return  # only GETs
            src, seq = data[20:30], int.from_bytes(data[30:32], "big")
            mid = int.from_bytes(data[52:54], "big")
            if mid in answers:
                d, me = answers[mid]
                self.t.sendto(response(mid, d, me, seq, target=src), (ptp.PTP_MCAST_GROUP, port))

    transport, _ = await loop.create_datagram_endpoint(Fake, sock=responder)
    try:
        clocks = await ptp.async_discover(interface_address="127.0.0.1", window=0.5)
    finally:
        transport.close()
    if not clocks:
        pytest.skip("multicast loopback delivered nothing on this host")
    slave = clocks["001122.fffe.334455"]
    assert slave.port_state == "slave"
    assert slave.offset_ns == 2500.0
    assert slave.time_status["gm_present"] is True
