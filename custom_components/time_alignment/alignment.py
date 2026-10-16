"""How aligned are the clocks, as a whole? Pure arithmetic over the samples.

Every NTP offset is measured against the clock of the machine running Home Assistant.
That clock is itself a participant — it may be the worst-aligned box on the network — so
an offset "against Home Assistant" mixes two errors. Subtracting the reference host's
offset from every other host's cancels Home Assistant's own error exactly (both were
measured against the same local clock within one poll), leaving host-minus-reference.

The spread (largest offset minus smallest, across the synchronised hosts that answered)
needs no reference at all, for the same reason.

Only a *synchronised* host can stand as the reference or count towards the spread and
the worst offset: an unsynchronised one is free-running, so its offset says nothing about
anyone else. It is reported as unsynchronised instead.

A measured offset is only known to within half the round trip (RFC 5905 §8: the error
from an asymmetric path is bounded by delay/2), and a host-minus-reference offset carries
both hosts' bounds. A host is out of tolerance only when its offset is beyond the
tolerance by more than that bound — otherwise an internet server 30 ms away would flap in
and out of a 10 ms tolerance on path noise alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .ntp import NtpSample

BASIS_REFERENCE = "reference"
BASIS_MEDIAN = "median"


@dataclass(frozen=True)
class HostAlignment:
    host: str
    offset_vs_reference_s: float | None
    # How far offset_vs_reference_s can be from the truth: half of each round trip.
    uncertainty_s: float | None
    within_tolerance: bool | None


@dataclass(frozen=True)
class NetworkAlignment:
    reference: str | None
    # What every offset is relative to: the reference host, the median of the
    # synchronised hosts (reference unset, down or unsynchronised), or None (nothing to
    # compare against).
    basis: str | None
    # The basis's offset against the local clock; negated, it is that clock's own error.
    basis_offset_s: float | None
    spread_s: float | None
    worst_host: str | None
    out_of_tolerance: list[str] = field(default_factory=list)
    unreachable: list[str] = field(default_factory=list)
    unsynchronised: list[str] = field(default_factory=list)
    hosts: dict[str, HostAlignment] = field(default_factory=dict)

    @property
    def reference_available(self) -> bool:
        return self.basis == BASIS_REFERENCE


def evaluate(
    hosts: list[str],
    samples: dict[str, NtpSample],
    reference: str | None,
    tolerance_s: float,
) -> NetworkAlignment:
    """Compare every host that answered against the reference and the tolerance.

    With no reference configured (or the reference not answering, or not synchronised),
    the median offset of the synchronised hosts stands in for it: a single wrong host then
    shows as the outlier it is, instead of everyone appearing offset from it."""
    answered = {h: samples[h] for h in hosts if h in samples}
    synced = {h: s for h, s in answered.items() if s.synchronised}
    unreachable = [h for h in hosts if h not in answered]
    unsynchronised = [h for h in answered if h not in synced]

    if reference is not None and reference in synced:
        basis = BASIS_REFERENCE
        base = synced[reference].offset_s
        base_uncertainty = synced[reference].delay_s / 2
    elif synced:
        basis = BASIS_MEDIAN
        base = _median(sorted(s.offset_s for s in synced.values()))
        # A median of several hosts has no single path to bound; each host keeps its own.
        base_uncertainty = 0.0
    else:
        basis, base, base_uncertainty = None, None, 0.0

    per_host: dict[str, HostAlignment] = {}
    out: list[str] = []
    worst: tuple[float, str] | None = None
    for host in hosts:
        sample = answered.get(host)
        if sample is None or base is None:
            per_host[host] = HostAlignment(host, None, None, None)
            continue
        rel = sample.offset_s - base
        if basis == BASIS_REFERENCE and host == reference:
            uncertainty = 0.0
        else:
            uncertainty = sample.delay_s / 2 + base_uncertainty
        if host not in synced:
            per_host[host] = HostAlignment(host, rel, uncertainty, False)
            continue
        ok = abs(rel) - uncertainty <= tolerance_s
        per_host[host] = HostAlignment(host, rel, uncertainty, ok)
        if not ok:
            out.append(host)
        if worst is None or abs(rel) > worst[0]:
            worst = (abs(rel), host)

    offsets = [s.offset_s for s in synced.values()]
    spread = (max(offsets) - min(offsets)) if len(offsets) >= 2 else None

    return NetworkAlignment(
        reference=reference,
        basis=basis,
        basis_offset_s=base,
        spread_s=spread,
        worst_host=worst[1] if worst else None,
        out_of_tolerance=out,
        unreachable=unreachable,
        unsynchronised=unsynchronised,
        hosts=per_host,
    )


def _median(values: list[float]) -> float:
    n = len(values)
    mid = n // 2
    return values[mid] if n % 2 else (values[mid - 1] + values[mid]) / 2


def host_problems(host: str, alignment: NetworkAlignment) -> list[str]:
    """Why one NTP host is not well aligned; empty when it is."""
    if host in alignment.unreachable:
        return ["unreachable"]
    if host in alignment.unsynchronised:
        return ["unsynchronised"]
    if host in alignment.out_of_tolerance:
        return ["out_of_tolerance"]
    return []


def hosts_with_problems(alignment: NetworkAlignment) -> list[str]:
    """Every host with any problem, in configured order."""
    return [h for h in alignment.hosts if host_problems(h, alignment)]
