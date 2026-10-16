"""NTP sampling and the network arithmetic."""

from __future__ import annotations

import socket
import struct
import threading
import time

import ntplib
import pytest

from custom_components.time_alignment import alignment, ntp
from custom_components.time_alignment.coordinator import split_host

NTP_EPOCH_DELTA = 2208988800


def sample(host, offset, *, delay=0.001, stratum=2, leap=0):
    return ntp.NtpSample(host, offset, delay, stratum, leap, "x", 0.002, 0.001, 0.0)


def _ntp_ts(t: float) -> bytes:
    t += NTP_EPOCH_DELTA
    return struct.pack(">II", int(t), int((t % 1) * 2**32))


class FakeNtpServer:
    """Answers NTP requests with a clock ``skew`` seconds ahead of ours."""

    def __init__(self, skew: float, stratum: int = 1, ref: bytes = b"PPS\x00", leap: int = 0):
        self.skew, self.stratum, self.ref, self.leap = skew, stratum, ref, leap
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.sock.settimeout(0.2)
        self.stop = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stop:
            try:
                req, addr = self.sock.recvfrom(512)
            except TimeoutError:
                continue
            now = time.time() + self.skew
            resp = (
                bytes([(self.leap << 6) | (4 << 3) | 4, self.stratum, 4, 0xEC])
                + struct.pack(">ii", int(0.004 * 65536), int(0.0005 * 65536))
                + self.ref
                + _ntp_ts(now)  # reference
                + req[40:48]  # originate = client's transmit
                + _ntp_ts(now)  # receive
                + _ntp_ts(now)  # transmit
            )
            self.sock.sendto(resp, addr)

    def close(self):
        self.stop = True
        self.thread.join()
        self.sock.close()


@pytest.mark.usefixtures("real_sockets")
def test_query_against_a_server_ahead_by_250ms():
    srv = FakeNtpServer(0.250)
    try:
        s = ntp.query("127.0.0.1", srv.port, timeout=1)
    finally:
        srv.close()
    assert s.offset_s == pytest.approx(0.250, abs=0.01)
    assert s.stratum == 1 and s.reference == "PPS" and s.synchronised
    assert s.root_distance_s == pytest.approx(0.002 + 0.0005, abs=1e-4)


@pytest.mark.usefixtures("real_sockets")
def test_unsynchronised_server():
    srv = FakeNtpServer(0.0, stratum=16, leap=3)
    try:
        s = ntp.query("127.0.0.1", srv.port, timeout=1)
    finally:
        srv.close()
    assert not s.synchronised


def test_reference_text():
    assert ntp.reference_text(int.from_bytes(b"GPS\x00", "big"), 1) == "GPS"
    assert ntp.reference_text(int.from_bytes(bytes([192, 0, 2, 7]), "big"), 2) == "192.0.2.7"


def test_best_of_takes_the_shortest_round_trip():
    a, b = sample("h", 0.010, delay=0.020), sample("h", 0.001, delay=0.002)
    assert ntp.best_of([a, b]) is b


def test_reference_cancels_the_local_clock():
    # Home Assistant's own clock is 40 ms slow, so every offset reads +40 ms.
    samples = {
        "ref": sample("ref", 0.040, stratum=1),
        "a": sample("a", 0.041),
        "b": sample("b", 0.065),
    }
    r = alignment.evaluate(["ref", "a", "b"], samples, "ref", tolerance_s=0.010)
    assert r.reference_available
    assert r.hosts["a"].offset_vs_reference_s == pytest.approx(0.001)
    assert r.hosts["b"].offset_vs_reference_s == pytest.approx(0.025)
    assert r.out_of_tolerance == ["b"]
    assert r.worst_host == "b"
    assert r.spread_s == pytest.approx(0.025)


def test_median_stands_in_when_the_reference_is_down():
    samples = {"a": sample("a", 0.000), "b": sample("b", 0.001), "c": sample("c", 0.500)}
    r = alignment.evaluate(["ref", "a", "b", "c"], samples, "ref", tolerance_s=0.010)
    assert not r.reference_available
    assert r.unreachable == ["ref"]
    assert r.out_of_tolerance == ["c"]


def test_unsynchronised_host_is_reported_once():
    samples = {"ref": sample("ref", 0.0), "a": sample("a", 0.0, leap=3)}
    r = alignment.evaluate(["ref", "a", "gone"], samples, "ref", tolerance_s=0.010)
    assert alignment.host_problems("a", r) == ["unsynchronised"]
    assert alignment.host_problems("ref", r) == []
    assert alignment.host_problems("gone", r) == ["unreachable"]
    assert alignment.hosts_with_problems(r) == ["a", "gone"]


def test_unsynchronised_reference_gives_way_to_the_median():
    # The reference lost its source and free-runs 5 s off; the others agree.
    samples = {
        "ref": sample("ref", 5.0, stratum=16, leap=3),
        "a": sample("a", 0.000),
        "b": sample("b", 0.001),
    }
    r = alignment.evaluate(["ref", "a", "b"], samples, "ref", tolerance_s=0.010)
    assert r.basis == alignment.BASIS_MEDIAN and not r.reference_available
    assert r.out_of_tolerance == []
    assert r.unsynchronised == ["ref"]
    assert alignment.host_problems("ref", r) == ["unsynchronised"]


def test_unsynchronised_host_stays_out_of_spread_and_worst():
    samples = {
        "ref": sample("ref", 0.0, stratum=1),
        "a": sample("a", 0.002),
        "lost": sample("lost", 3600.0, stratum=16, leap=3),
    }
    r = alignment.evaluate(["ref", "a", "lost"], samples, "ref", tolerance_s=0.010)
    assert r.spread_s == pytest.approx(0.002)
    assert r.worst_host == "a"
    assert r.hosts["lost"].within_tolerance is False


def test_nothing_synchronised_means_no_basis():
    samples = {"a": sample("a", 1.0, leap=3)}
    r = alignment.evaluate(["a"], samples, None, 0.01)
    assert r.basis is None and r.basis_offset_s is None
    assert r.hosts["a"].offset_vs_reference_s is None


def test_tolerance_allows_for_path_uncertainty():
    # 30 ms round trip: the offset is only known to +-15 ms (plus the reference's 0.5).
    samples = {
        "ref": sample("ref", 0.0, delay=0.001, stratum=1),
        # 20 ms is past the 10 ms tolerance, but not past it by more than +-15.5 ms.
        "far": sample("far", 0.020, delay=0.030),
        "near_bad": sample("near_bad", 0.012, delay=0.001),
        "far_bad": sample("far_bad", 0.030, delay=0.030),
    }
    r = alignment.evaluate(list(samples), samples, "ref", tolerance_s=0.010)
    assert r.hosts["far"].uncertainty_s == pytest.approx(0.0155)
    assert r.hosts["ref"].uncertainty_s == 0.0
    assert r.out_of_tolerance == ["near_bad", "far_bad"]


def test_split_host():
    assert split_host("ntp.example") == ("ntp.example", 123)
    assert split_host("ntp.example:1123") == ("ntp.example", 1123)
    assert split_host("fe80::1") == ("fe80::1", 123)
    assert split_host("[fe80::1]:1123") == ("fe80::1", 1123)
    for bad in ("host:", "host:x", "host:70000", "[fe80::1", "[fe80::1]x", ":123"):
        with pytest.raises(ValueError):
            split_host(bad)


def test_public_or_lan():
    assert ntp.is_public("216.239.35.0")  # time.google.com
    assert ntp.is_public("2001:4860:4806::")
    # One address from each range that is ours, not someone else's server.
    for lan in ("192.168.1.10", "10.0.0.1", "172.16.0.1", "127.0.0.1", "169.254.1.1",
                "100.64.0.1", "192.0.2.10", "fd00::1", "fe80::1%eth0", "::1"):
        assert not ntp.is_public(lan), lan


def _measure(monkeypatch, address, answers):
    """Run ntp.measure against a fake query; return the addresses it asked."""
    asked = []

    def fake_query(addr, port=123, timeout=2.0):
        asked.append(addr)
        answer = answers[len(asked) - 1]
        if isinstance(answer, Exception):
            raise answer
        return sample(addr, 0.0, delay=answer)

    monkeypatch.setattr(ntp, "resolve", lambda host, port=123: address)
    monkeypatch.setattr(ntp, "query", fake_query)
    monkeypatch.setattr(ntp, "NTP_SAMPLE_GAP", 0)
    return asked, ntp.measure("name.example")


def test_lan_host_gets_a_burst_and_keeps_the_fastest(monkeypatch):
    asked, s = _measure(monkeypatch, "192.0.2.10", [0.004, 0.001, 0.003])
    assert asked == ["192.0.2.10"] * 3
    assert s.delay_s == 0.001
    assert s.host == "name.example" and s.address == "192.0.2.10"


def test_public_server_gets_one_query(monkeypatch):
    asked, s = _measure(monkeypatch, "216.239.35.0", [0.030, 0.001, 0.001])
    assert asked == ["216.239.35.0"]
    assert s.address == "216.239.35.0"


def test_kiss_of_death_ends_the_burst(monkeypatch):
    with pytest.raises(ntp.KissOfDeath):
        _measure(monkeypatch, "192.0.2.10", [ntp.KissOfDeath("x", "RATE"), 0.001, 0.001])


def test_burst_survives_one_lost_reply(monkeypatch):
    asked, s = _measure(
        monkeypatch, "192.0.2.10", [ntplib.NTPException("lost"), 0.002, 0.003]
    )
    assert len(asked) == 3 and s.delay_s == 0.002


@pytest.mark.usefixtures("real_sockets")
def test_kiss_of_death_is_not_a_sample():
    srv = FakeNtpServer(0.0, stratum=0, ref=b"RATE")
    try:
        with pytest.raises(ntp.KissOfDeath) as err:
            ntp.query("127.0.0.1", srv.port, timeout=1)
    finally:
        srv.close()
    assert err.value.code == "RATE"


def test_single_host_has_no_spread():
    r = alignment.evaluate(["a"], {"a": sample("a", 0.1)}, None, 0.01)
    assert r.spread_s is None


def test_reference_text_keeps_unknown_codes():
    assert ntp.reference_text(int.from_bytes(b"PPS\x00", "big"), 1) == "PPS"
    assert ntp.reference_text(int.from_bytes(b"NMEA", "big"), 0) == "NMEA"
