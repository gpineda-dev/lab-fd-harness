#!/usr/bin/env python3
"""
analyze_run.py - Metrology analyzer for clock-tick benchmark runs.

Usage:
    # Analyze output directly from pipe:
    ./clock-tick.sh -m relative ... | python3 tools/analyze_run.py

    # Or compare multiple saved runs:
    python3 tools/analyze_run.py naive.log relative.log computed.log pure.log
"""
import sys
import re
import math
from datetime import datetime
from typing import List, Dict, Any, Optional


def parse_timestamp(time_str: str) -> datetime:
    return datetime.strptime(time_str, "%H:%M:%S.%f")


def parse_run(text: str) -> Optional[Dict[str, Any]]:
    strat_match = re.search(r"Strategy:\s+(\S+)", text)
    ref_match = re.search(r"Reference:\s+(\S+)", text)
    mode_match = re.search(r"Mode:\s+(\S+)", text)
    cadence_match = re.search(r"Cadence:\s+([\d\.]+)s", text)
    work_match = re.search(r"Work duration:\s+([\d\.]+)s", text)
    cycles_match = re.search(r"Cycles:\s+(\d+)", text)
    calc_match = re.search(r"Calc engine:\s+(\S+)", text)
    drift_match = re.search(r"Net cumulative drift:\s+([\+\-\d\.]+)\s+ms", text)

    cycle_lines = re.findall(r"\[Cycle\s+(\d+)\]\s+(\d{2}:\d{2}:\d{2}\.\d{3})", text)
    if not cycle_lines:
        return None

    cadence = float(cadence_match.group(1)) if cadence_match else 2.0
    work = float(work_match.group(1)) if work_match else 0.5
    cycles_count = int(cycles_match.group(1)) if cycles_match else len(cycle_lines)
    calc = calc_match.group(1) if calc_match else "bc"
    net_drift = float(drift_match.group(1)) if drift_match else 0.0

    if strat_match:
        strategy = strat_match.group(1)
        reference = ref_match.group(1) if ref_match else "init"
        mode_label = f"{strategy}(ref={reference})"
    else:
        mode_label = mode_match.group(1) if mode_match else "unknown"

    parsed_cycles = []
    t0 = parse_timestamp(cycle_lines[0][1])

    prev_t = t0
    intervals = []
    cycle_drifts = []

    for idx_str, ts_str in cycle_lines:
        idx = int(idx_str)
        t = parse_timestamp(ts_str)

        # Elapsed from T0 in seconds
        elapsed_from_t0 = (t - t0).total_seconds()
        # Theoretical target for this cycle
        target_elapsed = idx * cadence
        cycle_drift_ms = (elapsed_from_t0 - target_elapsed) * 1000.0
        cycle_drifts.append(cycle_drift_ms)

        if idx > 0:
            dt = (t - prev_t).total_seconds()
            intervals.append(dt)
        prev_t = t

        parsed_cycles.append({
            "cycle": idx,
            "timestamp": ts_str,
            "drift_ms": cycle_drift_ms
        })

    mean_interval = (sum(intervals) / len(intervals)) if intervals else cadence
    if len(intervals) > 1:
        variance = sum((x - mean_interval) ** 2 for x in intervals) / (len(intervals) - 1)
        std_dev_ms = math.sqrt(variance) * 1000.0
    else:
        std_dev_ms = 0.0

    return {
        "mode": mode_label,
        "cadence": cadence,
        "work": work,
        "cycles": cycles_count,
        "calc": calc,
        "net_drift_ms": net_drift,
        "mean_interval": mean_interval,
        "jitter_std_ms": std_dev_ms,
        "min_interval": min(intervals) if intervals else cadence,
        "max_interval": max(intervals) if intervals else cadence,
        "cycle_drifts": cycle_drifts,
        "parsed_cycles": parsed_cycles,
    }


def print_single_report(run: Dict[str, Any]):
    print("=" * 65)
    print(f" METROLOGY REPORT: {run['mode'].upper()} (Cadence: {run['cadence']}s)")
    print("=" * 65)
    print(f"  Target Cadence:        {run['cadence']:.4f}s")
    print(f"  Mean Interval:         {run['mean_interval']:.4f}s")
    print(f"  Jitter StdDev (σ):     ±{run['jitter_std_ms']:.3f} ms")
    print(f"  Min / Max Interval:    {run['min_interval']:.4f}s / {run['max_interval']:.4f}s")
    print(f"  Net Cumulative Drift:  {run['net_drift_ms']:+.3f} ms")
    print("-" * 65)
    print(f"{'Cycle':<6} | {'Timestamp':<14} | {'Target (s)':<10} | {'Drift (ms)':<12}")
    print("-" * 65)
    for c in run['parsed_cycles']:
        target_s = c['cycle'] * run['cadence']
        print(f"{c['cycle']:<6} | {c['timestamp']:<14} | +{target_s:<9.1f} | {c['drift_ms']:+10.3f} ms")
    print("=" * 65)


def print_comparison_table(runs: List[Dict[str, Any]]):
    print("\n" + "=" * 80)
    print(" COMPARATIVE BENCHMARK SYNTHESIS")
    print("=" * 80)
    headers = ["Metric"] + [r["mode"] for r in runs]
    
    col_w = max(18, max(len(h) for h in headers) + 2)
    fmt = f"%-24s" + "".join([f" | %-{col_w}s" for _ in runs])
    sep = "-" * (24 + (col_w + 3) * len(runs))

    print(fmt % tuple(headers))
    print(sep)

    print(fmt % tuple(["Target Cadence"] + [f"{r['cadence']:.3f}s" for r in runs]))
    print(fmt % tuple(["Mean Interval"] + [f"{r['mean_interval']:.4f}s" for r in runs]))
    print(fmt % tuple(["Jitter (σ)"] + [f"±{r['jitter_std_ms']:.2f} ms" for r in runs]))
    print(fmt % tuple(["Min Interval"] + [f"{r['min_interval']:.4f}s" for r in runs]))
    print(fmt % tuple(["Max Interval"] + [f"{r['max_interval']:.4f}s" for r in runs]))
    print(fmt % tuple(["Net Final Drift"] + [f"{r['net_drift_ms']:+.2f} ms" for r in runs]))
    print(sep)


def main():
    if len(sys.argv) > 1:
        runs = []
        for file_path in sys.argv[1:]:
            try:
                with open(file_path, "r") as f:
                    parsed = parse_run(f.read())
                    if parsed:
                        runs.append(parsed)
            except Exception as e:
                print(f"Error reading {file_path}: {e}", file=sys.stderr)

        if not runs:
            print("No valid run logs found.", file=sys.stderr)
            return 1

        if len(runs) == 1:
            print_single_report(runs[0])
        else:
            print_comparison_table(runs)
    else:
        # Read from stdin
        raw = sys.stdin.read()
        parsed = parse_run(raw)
        if parsed:
            print_single_report(parsed)
        else:
            print("Could not parse benchmark output from stdin.", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
