#!/usr/bin/env python3
"""Run the integration's own NTP/PTP code against real hosts, outside Home Assistant.

    tools/probe.py [--reference HOST] [--ptp] HOST [HOST ...]

Hosts are names or addresses, e.g.:
    tools/probe.py --reference ntp1.example ntp1.example router.example time.google.com

The same code paths the integration uses (ntp.measure, alignment.evaluate,
ptp.async_discover), so a result here is a result for the integration. Needs only
``ntplib``, not Home Assistant: the protocol modules import nothing from it, and the
package's own ``__init__`` (which does) is skipped.
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
import types

_PACKAGE = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "time_alignment"
# Register the package without running its __init__ (that one imports Home Assistant).
_pkg = types.ModuleType("time_alignment")
_pkg.__path__ = [str(_PACKAGE)]
sys.modules["time_alignment"] = _pkg

from time_alignment import alignment, ntp, ptp  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("hosts", nargs="*")
    ap.add_argument("--reference")
    ap.add_argument("--tolerance-ms", type=float, default=10.0)
    ap.add_argument("--ptp", action="store_true", help="also sweep PTP (needs UDP 320)")
    ap.add_argument("--ptp-domain", type=int, default=0)
    args = ap.parse_args()

    samples: dict[str, ntp.NtpSample] = {}
    for host in args.hosts:
        try:
            samples[host] = ntp.measure(host)
        except Exception as err:  # noqa: BLE001 - a probe reports, never stops
            print(f"{host:24} no answer: {err}")

    result = alignment.evaluate(args.hosts, samples, args.reference, args.tolerance_ms / 1000)
    print(f"{'host':24} {'offset ms':>10} {'vs ref ms':>10} {'rtt ms':>8} {'rootdist ms':>11} "
          f"{'st':>3} ref        problems")
    for host, s in samples.items():
        rel = result.hosts[host].offset_vs_reference_s
        print(f"{host:24} {s.offset_s * 1000:10.3f} "
              f"{'' if rel is None else f'{rel * 1000:10.3f}':>10} {s.delay_s * 1000:8.3f} "
              f"{s.root_distance_s * 1000:11.3f} {s.stratum:3} {s.reference:10} "
              f"{','.join(alignment.host_problems(host, result)) or '-'}")
    if result.spread_s is not None:
        print(f"spread {result.spread_s * 1000:.3f} ms; worst {result.worst_host}; "
              f"basis {result.basis}")

    if args.ptp:
        try:
            clocks = asyncio.run(ptp.async_discover(domain=args.ptp_domain))
        except OSError as err:
            print(f"PTP: cannot listen on UDP {ptp.PTP_GENERAL_PORT}: {err}")
            return 1
        print(f"PTP: {len(clocks)} clock(s) answered in domain {args.ptp_domain}")
        for cid, c in clocks.items():
            print(f"  {cid} {c.source_address} {c.port_state} offset={c.offset_ns} ns "
                  f"gm={c.parent.get('grandmaster_identity')} "
                  f"problems={ptp.problems(c, 1000) or '-'}")
        for problem in ptp.network_problems(clocks):
            print(f"PTP: {problem}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
