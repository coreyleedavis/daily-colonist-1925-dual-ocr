#!/usr/bin/env python3
"""
rapl_sampler.py  --  measure CPU package (and DRAM) energy without weakening the box
====================================================================================
Run this with sudo in ONE terminal while your (unprivileged) OCR job runs in
another. Only this small process reads the root-locked RAPL counters; the energy
files stay 0400 for everyone else, so no PLATYPUS-style side channel is opened
system-wide. RAPL is whole-socket regardless of who runs the workload, so this
measures the same thing CodeCarbon's RAPL reader would -- just safely.

Why a poller (not just read-start / read-end):
  The energy_uj counter is an unsigned register that WRAPS. On this class of
  CPU it can wrap roughly every few minutes under full load, so a naive
  end-minus-start over a multi-hour OCR run would be badly wrong. Polling every
  ~20s catches each wrap and accumulates correctly.

Usage:
  # Idle baseline first (box otherwise quiet), 2 minutes:
  sudo python3 rapl_sampler.py --duration 120 --label idle --out rapl_idle.csv

  # Then, alongside the OCR job, until you Ctrl-C it when the job finishes:
  sudo python3 rapl_sampler.py --label tesseract_1925 --out rapl_tess.csv

  # Or for a fixed window:
  sudo python3 rapl_sampler.py --duration 3600 --label tess --out rapl_tess.csv

Combine with the OCR run:
  Wh_per_page = (sampler Wh)  /  (pages_total from summary.json)
  Net of idle: subtract  idle_watts * job_duration_seconds / 3600  from the Wh.
"""

import argparse
import csv
import glob
import os
import signal
import sys
import time

RAPL_ROOT = "/sys/class/powercap"


def _read_int(path):
    with open(path) as f:
        return int(f.read().strip())


def discover_domains():
    """Find top-level package domains and any DRAM domains. Package domains
    already include their core/uncore subdomains, so we do NOT sum those
    (avoids double counting); DRAM on server parts is separate, so we add it."""
    domains = []
    for d in sorted(glob.glob(os.path.join(RAPL_ROOT, "intel-rapl:*"))):
        base = os.path.basename(d)           # e.g. intel-rapl:0  or  intel-rapl:0:1
        energy = os.path.join(d, "energy_uj")
        if not os.path.exists(energy):
            continue
        try:
            name = open(os.path.join(d, "name")).read().strip()
        except OSError:
            name = base
        colons = base.count(":")
        is_package = (colons == 1)           # intel-rapl:N  (top-level socket)
        is_dram = (name == "dram")
        if is_package or is_dram:
            try:
                max_range = _read_int(os.path.join(d, "max_energy_range_uj"))
            except OSError:
                max_range = None
            domains.append({"path": energy, "name": f"{name}:{base}",
                            "max": max_range, "last": None, "accum_uj": 0})
    return domains


def poll(domains):
    """Add the latest delta (handling wraparound) to each domain's accumulator."""
    for dom in domains:
        try:
            cur = _read_int(dom["path"])
        except PermissionError:
            sys.exit("Permission denied reading RAPL -- run this script with sudo.")
        if dom["last"] is not None:
            delta = cur - dom["last"]
            if delta < 0 and dom["max"]:     # counter wrapped
                delta += dom["max"]
            if delta >= 0:
                dom["accum_uj"] += delta
        dom["last"] = cur


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--interval", type=float, default=20.0,
                    help="Seconds between polls (keep well under the wrap period; default 20)")
    ap.add_argument("--duration", type=float, default=None,
                    help="Stop after N seconds (default: run until Ctrl-C)")
    ap.add_argument("--label", default="run", help="Tag for this measurement")
    ap.add_argument("--out", default="rapl_energy.csv", help="Timeseries CSV path")
    args = ap.parse_args()

    domains = discover_domains()
    if not domains:
        sys.exit("No RAPL package/DRAM domains found (need an Intel CPU and sudo).")
    print("Measuring domains: " + ", ".join(d["name"] for d in domains))
    print("Polling every %.0fs. %s" % (
        args.interval,
        "Ctrl-C to stop." if not args.duration else f"Stopping after {args.duration}s."))

    poll(domains)                  # establish baseline reading
    t0 = time.time()
    rows = []
    stop = {"flag": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))

    try:
        while not stop["flag"]:
            time.sleep(args.interval)
            poll(domains)
            elapsed = time.time() - t0
            total_j = sum(d["accum_uj"] for d in domains) / 1e6
            watts = total_j / elapsed if elapsed else 0
            rows.append({"elapsed_s": round(elapsed, 1),
                         "total_J": round(total_j, 1),
                         "mean_W": round(watts, 1)})
            print(f"\r  {elapsed:7.0f}s   {total_j/3600:8.3f} Wh   "
                  f"mean {watts:6.1f} W", end="", flush=True)
            if args.duration and elapsed >= args.duration:
                break
    finally:
        elapsed = time.time() - t0
        per_domain = {d["name"]: round(d["accum_uj"] / 1e6, 1) for d in domains}
        total_j = sum(d["accum_uj"] for d in domains) / 1e6
        summary = {
            "label": args.label,
            "duration_s": round(elapsed, 1),
            "total_J": round(total_j, 1),
            "total_Wh": round(total_j / 3600, 4),
            "total_kWh": round(total_j / 3.6e6, 6),
            "mean_W": round(total_j / elapsed, 1) if elapsed else 0,
            "per_domain_J": per_domain,
        }
        print("\n\n==== ENERGY SUMMARY ====")
        for k, v in summary.items():
            print(f"  {k}: {v}")
        with open(args.out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["elapsed_s", "total_J", "mean_W"])
            w.writeheader()
            w.writerows(rows)
        with open(args.out.replace(".csv", "_summary.txt"), "w") as f:
            for k, v in summary.items():
                f.write(f"{k}: {v}\n")
        print(f"\n  timeseries -> {args.out}")


if __name__ == "__main__":
    main()
