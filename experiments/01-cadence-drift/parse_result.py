#!/usr/bin/env python3
"""
parse_result.py - Metrology Run Parser & Reconstituter for clock-tick.sh.

Parses stdout output from clock-tick.sh (single or multi-run, from files or stdin),
reconstitutes run parameters, cycle events, wall vs monotonic metrology,
and computes drift, jitter, overruns, and comparative benchmark synthesis.

Usage:
    # From stdin (single run or piped benchmark):
    uv run fd-harness run ./experiments/01-cadence-drift/clock-tick.sh ... | python3 parse_result.py

    # From file(s):
    python3 parse_result.py run1.log run2.log
    python3 parse_result.py multi_run.log

    # JSON export:
    python3 parse_result.py --json run1.log
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class CycleEvent:
    cycle: int
    wall_time_str: str
    elapsed_wall_s: float
    target_wall_s: float
    drift_wall_ms: float
    wall_interval_s: Optional[float] = None
    mono_ts: Optional[float] = None
    mono_interval_s: Optional[float] = None
    lag_ms: Optional[float] = None
    skipped_cycles: int = 0
    spike_injected: bool = False
    spike_duration_s: Optional[float] = None


@dataclass
class RunResult:
    # Metadata / Header
    strategy: str = "unknown"
    reference: str = "init"
    cadence_s: float = 2.0
    work_duration_s: float = 0.5
    calc_engine: str = "bc"
    cycles_requested: int = 0
    started_at: str = ""
    finished_at: str = ""

    # Footer metrics (from benchmark script)
    reported_theoretical_s: Optional[float] = None
    reported_actual_s: Optional[float] = None
    reported_net_drift_ms: Optional[float] = None

    # Reconstituted cycle events
    cycles: List[CycleEvent] = field(default_factory=list)

    # Computed Metrology Metrics (Wall Clock)
    mean_wall_interval_s: float = 0.0
    jitter_wall_std_ms: float = 0.0
    min_wall_interval_s: float = 0.0
    max_wall_interval_s: float = 0.0
    net_wall_drift_ms: float = 0.0

    # Computed Metrology Metrics (Monotonic Clock if present)
    has_monotonic: bool = False
    mean_mono_interval_s: Optional[float] = None
    jitter_mono_std_ms: Optional[float] = None
    min_mono_interval_s: Optional[float] = None
    max_mono_interval_s: Optional[float] = None
    net_mono_drift_ms: Optional[float] = None

    # Telemetry / Overrun statistics
    total_skipped_cycles: int = 0
    overrun_event_count: int = 0
    spike_injected_count: int = 0

    @property
    def label(self) -> str:
        return f"{self.strategy}(ref={self.reference})"


def _parse_time(time_str: str) -> datetime:
    return datetime.strptime(time_str, "%H:%M:%S.%f")


def _std_dev(values: List[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((x - mean) ** 2 for x in values) / (len(values) - 1)
    return math.sqrt(variance)


def parse_single_run_text(text: str) -> Optional[RunResult]:
    """Parses a single benchmark run block."""
    if "=== Cadence Drift Benchmark ===" not in text and "@@@ CLAP:START @@@" not in text:
        return None

    run = RunResult()

    # Header parsing
    strat_m = re.search(r"Strategy:\s+(\S+)", text)
    ref_m = re.search(r"Reference:\s+(\S+)", text)
    cadence_m = re.search(r"Cadence:\s+([\d\.]+)s", text)
    work_m = re.search(r"Work duration:\s+([\d\.]+)s", text)
    calc_m = re.search(r"Calc engine:\s+(\S+)", text)
    cycles_m = re.search(r"Cycles:\s+(\d+)", text)
    started_m = re.search(r"Started at:\s+(\S+)", text)

    if strat_m:
        run.strategy = strat_m.group(1)
    if ref_m:
        run.reference = ref_m.group(1)
    if cadence_m:
        run.cadence_s = float(cadence_m.group(1))
    if work_m:
        run.work_duration_s = float(work_m.group(1))
    if calc_m:
        run.calc_engine = calc_m.group(1)
    if cycles_m:
        run.cycles_requested = int(cycles_m.group(1))
    if started_m:
        run.started_at = started_m.group(1)

    # Footer parsing
    finished_m = re.search(r"Finished at:\s+(\S+)", text)
    theo_m = re.search(r"Theoretical duration:\s+([\d\.]+)s", text)
    act_m = re.search(r"Actual duration:\s+([\d\.]+)s", text)
    drift_m = re.search(r"Net cumulative drift:\s+([\+\-\d\.]+)\s+ms", text)

    if finished_m:
        run.finished_at = finished_m.group(1)
    if theo_m:
        run.reported_theoretical_s = float(theo_m.group(1))
    if act_m:
        run.reported_actual_s = float(act_m.group(1))
    if drift_m:
        run.reported_net_drift_ms = float(drift_m.group(1))

    # Line-by-line parsing of execution block between CLAP:START and CLAP:END
    lines = text.splitlines()
    in_clap = False

    current_cycle_idx: Optional[int] = None
    current_cycle_time: Optional[str] = None
    current_mono_ts: Optional[float] = None
    current_lag_ms: Optional[float] = None
    current_skipped = 0
    current_spike = False
    current_spike_dur: Optional[float] = None

    def flush_current_cycle():
        nonlocal current_cycle_idx, current_cycle_time, current_mono_ts
        nonlocal current_lag_ms, current_skipped, current_spike, current_spike_dur
        if current_cycle_idx is None or current_cycle_time is None:
            return
        event = CycleEvent(
            cycle=current_cycle_idx,
            wall_time_str=current_cycle_time,
            elapsed_wall_s=0.0,
            target_wall_s=current_cycle_idx * run.cadence_s,
            drift_wall_ms=0.0,
            mono_ts=current_mono_ts,
            lag_ms=current_lag_ms,
            skipped_cycles=current_skipped,
            spike_injected=current_spike,
            spike_duration_s=current_spike_dur,
        )
        run.cycles.append(event)
        current_cycle_idx = None
        current_cycle_time = None
        current_mono_ts = None
        current_lag_ms = None
        current_skipped = 0
        current_spike = False
        current_spike_dur = None

    pending_mono_ts: Optional[float] = None
    pending_lag_ms: Optional[float] = None

    for line in lines:
        if "@@@ CLAP:START @@@" in line:
            in_clap = True
            continue
        if "@@@ CLAP:END @@@" in line:
            flush_current_cycle()
            in_clap = False
            continue

        if not in_clap:
            continue

        clean_line = re.sub(r"\x1b\[[0-9;]*m", "", line)

        # Check for harness supervisor wire traces: [HARNESS OUT] tick ...
        if "[HARNESS OUT] tick" in clean_line:
            parts = clean_line.strip().split()
            if len(parts) >= 5 and parts[2] == "tick":
                try:
                    if len(parts) == 5:
                        # Legacy push: [HARNESS OUT] tick <cycle> <mono_ts>
                        pending_mono_ts = float(parts[4])
                        pending_lag_ms = 0.0
                    elif len(parts) >= 9:
                        # Pull clock: [HARNESS OUT] tick <id> <cycle> <skipped> <lag_ms> <mono_ts> <status>
                        pending_lag_ms = float(parts[6])
                        pending_mono_ts = float(parts[7])
                except ValueError:
                    pass
            continue

        # Check for cycle start: [Cycle N] HH:MM:SS.mmm
        cycle_m = re.search(r"\[Cycle\s+(\d+)\]\s+(\d{2}:\d{2}:\d{2}\.\d{3})", line)
        if cycle_m:
            flush_current_cycle()
            current_cycle_idx = int(cycle_m.group(1))
            current_cycle_time = cycle_m.group(2)
            if pending_mono_ts is not None:
                current_mono_ts = pending_mono_ts
                pending_mono_ts = None
            if pending_lag_ms is not None:
                current_lag_ms = pending_lag_ms
                pending_lag_ms = None
            continue

        # Check for metrology telemetry: [METROLOGY] wall_date=... | harness_mono=...s | lag=...ms
        if "[METROLOGY]" in line:
            mono_m = re.search(r"harness_mono=([\d\.]+)s", line)
            lag_m = re.search(r"lag=([\d\.]+)ms", line)
            if mono_m:
                current_mono_ts = float(mono_m.group(1))
            if lag_m:
                current_lag_ms = float(lag_m.group(1))
            continue

        # Check for overrun spike injection: [!] OVERRUN SPIKE INJECTED: duration=...s
        if "OVERRUN SPIKE INJECTED" in line:
            current_spike = True
            spike_dur_m = re.search(r"duration=([\d\.]+)s", line)
            if spike_dur_m:
                current_spike_dur = float(spike_dur_m.group(1))
            continue

        # Check for telemetry overrun notification: [TELEMETRY] Overrun: skipped N cycle(s) (lag=...ms)
        if "[TELEMETRY] Overrun:" in line:
            skip_m = re.search(r"skipped\s+(\d+)\s+cycle", line)
            lag_m = re.search(r"lag=([\d\.]+)ms", line)
            if skip_m:
                current_skipped += int(skip_m.group(1))
            if lag_m and current_lag_ms is None:
                current_lag_ms = float(lag_m.group(1))
            continue

    flush_current_cycle()

    if not run.cycles:
        return None

    # Compute Metrology & Intervals
    t0_wall = _parse_time(run.cycles[0].wall_time_str)
    prev_t_wall = t0_wall
    wall_intervals: List[float] = []

    has_mono = any(c.mono_ts is not None for c in run.cycles)
    run.has_monotonic = has_mono
    prev_mono: Optional[float] = None
    mono_intervals: List[float] = []

    for i, c in enumerate(run.cycles):
        t_wall = _parse_time(c.wall_time_str)
        elapsed_wall = (t_wall - t0_wall).total_seconds()
        c.elapsed_wall_s = elapsed_wall
        c.drift_wall_ms = (elapsed_wall - c.target_wall_s) * 1000.0

        if i > 0:
            dt_wall = (t_wall - prev_t_wall).total_seconds()
            c.wall_interval_s = dt_wall
            # Only consider standard 1-cycle step for nominal jitter calculation
            prev_cycle = run.cycles[i - 1].cycle
            if c.cycle == prev_cycle + 1:
                wall_intervals.append(dt_wall)

            if has_mono and c.mono_ts is not None and prev_mono is not None:
                dt_mono = c.mono_ts - prev_mono
                c.mono_interval_s = dt_mono
                if c.cycle == prev_cycle + 1:
                    mono_intervals.append(dt_mono)

        prev_t_wall = t_wall
        if c.mono_ts is not None:
            prev_mono = c.mono_ts

        if c.skipped_cycles > 0:
            run.total_skipped_cycles += c.skipped_cycles
            run.overrun_event_count += 1
        if c.spike_injected:
            run.spike_injected_count += 1

    # Wall metrics
    if wall_intervals:
        run.mean_wall_interval_s = sum(wall_intervals) / len(wall_intervals)
        run.jitter_wall_std_ms = _std_dev(wall_intervals) * 1000.0
        run.min_wall_interval_s = min(wall_intervals)
        run.max_wall_interval_s = max(wall_intervals)
    else:
        run.mean_wall_interval_s = run.cadence_s
        run.jitter_wall_std_ms = 0.0
        run.min_wall_interval_s = run.cadence_s
        run.max_wall_interval_s = run.cadence_s

    run.net_wall_drift_ms = run.cycles[-1].drift_wall_ms

    # Monotonic metrics
    if has_mono and mono_intervals:
        run.mean_mono_interval_s = sum(mono_intervals) / len(mono_intervals)
        run.jitter_mono_std_ms = _std_dev(mono_intervals) * 1000.0
        run.min_mono_interval_s = min(mono_intervals)
        run.max_mono_interval_s = max(mono_intervals)
        first_mono = run.cycles[0].mono_ts or 0.0
        last_mono = run.cycles[-1].mono_ts or 0.0
        target_mono_s = run.cycles[-1].target_wall_s
        run.net_mono_drift_ms = ((last_mono - first_mono) - target_mono_s) * 1000.0

    return run


def parse_runs_from_text(text: str) -> List[RunResult]:
    """Splits full input text into individual runs and parses each."""
    blocks = re.split(r"(?==== Cadence Drift Benchmark ===)", text)
    runs: List[RunResult] = []
    for block in blocks:
        if not block.strip():
            continue
        parsed = parse_single_run_text(block)
        if parsed:
            runs.append(parsed)

    # Fallback: if no benchmark header was found, try whole text
    if not runs:
        parsed = parse_single_run_text(text)
        if parsed:
            runs.append(parsed)
    return runs


def format_single_run_report(run: RunResult) -> str:
    lines = []
    lines.append("=" * 80)
    lines.append(f" METROLOGY REPORT: {run.label.upper()} (Target Cadence: {run.cadence_s}s)")
    lines.append("=" * 80)
    lines.append(f"  Strategy:               {run.strategy}")
    lines.append(f"  Reference Anchor:       {run.reference}")
    lines.append(f"  Math Engine:            {run.calc_engine}")
    lines.append(f"  Simulated Work:         {run.work_duration_s:.3f}s")
    lines.append(f"  Cycles Executed:        {len(run.cycles)} / {run.cycles_requested or len(run.cycles)}")
    if run.total_skipped_cycles > 0 or run.overrun_event_count > 0:
        lines.append(f"  Overrun Events:         {run.overrun_event_count} (Total skipped cycles: {run.total_skipped_cycles})")
    lines.append("-" * 80)
    lines.append(f"  [WALL CLOCK] Mean Interval:       {run.mean_wall_interval_s:.4f}s")
    lines.append(f"  [WALL CLOCK] Jitter StdDev (σ):   ±{run.jitter_wall_std_ms:.3f} ms")
    lines.append(f"  [WALL CLOCK] Min / Max Interval:  {run.min_wall_interval_s:.4f}s / {run.max_wall_interval_s:.4f}s")
    lines.append(f"  [WALL CLOCK] Net Final Drift:     {run.net_wall_drift_ms:+.3f} ms")

    if run.has_monotonic:
        lines.append("-" * 80)
        lines.append(f"  [MONOTONIC]  Mean Interval:       {run.mean_mono_interval_s:.4f}s")
        lines.append(f"  [MONOTONIC]  Jitter StdDev (σ):   ±{run.jitter_mono_std_ms:.3f} ms")
        lines.append(f"  [MONOTONIC]  Min / Max Interval:  {run.min_mono_interval_s:.4f}s / {run.max_mono_interval_s:.4f}s")
        if run.net_mono_drift_ms is not None:
            lines.append(f"  [MONOTONIC]  Net Monotonic Drift: {run.net_mono_drift_ms:+.3f} ms")

    if run.reported_net_drift_ms is not None:
        lines.append("-" * 80)
        lines.append(f"  Benchmark Reported Drift: {run.reported_net_drift_ms:+.3f} ms (Actual: {run.reported_actual_s}s, Theo: {run.reported_theoretical_s}s)")

    lines.append("-" * 80)
    header = f"{'Cycle':<6} | {'Timestamp':<13} | {'Target(s)':<10} | {'Drift(ms)':<11} | {'Interval':<10}"
    if run.has_monotonic:
        header += f" | {'Lag(ms)':<8} | {'Mono(s)':<13}"
    header += " | Notes"
    lines.append(header)
    lines.append("-" * len(header))

    for c in run.cycles:
        target_str = f"+{c.target_wall_s:.2f}s"
        drift_str = f"{c.drift_wall_ms:+.3f}ms"
        inv_str = f"{c.wall_interval_s:.4f}s" if c.wall_interval_s is not None else "---"
        row = f"{c.cycle:<6} | {c.wall_time_str:<13} | {target_str:<10} | {drift_str:<11} | {inv_str:<10}"

        if run.has_monotonic:
            lag_str = f"{c.lag_ms:.3f}" if c.lag_ms is not None else "---"
            mono_str = f"{c.mono_ts:.4f}" if c.mono_ts is not None else "---"
            row += f" | {lag_str:<8} | {mono_str:<13}"

        notes = []
        if c.spike_injected:
            notes.append(f"SPIKE({c.spike_duration_s}s)")
        if c.skipped_cycles > 0:
            notes.append(f"SKIPPED({c.skipped_cycles})")
        note_str = ", ".join(notes) if notes else ""
        row += f" | {note_str}"

        lines.append(row)

    lines.append("=" * 80)
    return "\n".join(lines)


def format_comparison_table(runs: List[RunResult]) -> str:
    lines = []
    lines.append("\n" + "=" * 92)
    lines.append(" COMPARATIVE METROLOGY SYNTHESIS")
    lines.append("=" * 92)

    headers = ["Metric"] + [r.label for r in runs]
    col_w = max(20, max(len(h) for h in headers) + 2)
    fmt = f"%-24s" + "".join([f" | %-{col_w}s" for _ in runs])
    sep = "-" * (24 + (col_w + 3) * len(runs))

    lines.append(fmt % tuple(headers))
    lines.append(sep)

    lines.append(fmt % tuple(["Target Cadence"] + [f"{r.cadence_s:.3f}s" for r in runs]))
    lines.append(fmt % tuple(["Simulated Work"] + [f"{r.work_duration_s:.3f}s" for r in runs]))
    lines.append(fmt % tuple(["Cycles (Exec/Req)"] + [f"{len(r.cycles)}/{r.cycles_requested}" for r in runs]))
    lines.append(fmt % tuple(["Mean Interval (Wall)"] + [f"{r.mean_wall_interval_s:.4f}s" for r in runs]))
    lines.append(fmt % tuple(["Jitter σ (Wall)"] + [f"±{r.jitter_wall_std_ms:.2f} ms" for r in runs]))
    lines.append(fmt % tuple(["Min Interval (Wall)"] + [f"{r.min_wall_interval_s:.4f}s" for r in runs]))
    lines.append(fmt % tuple(["Max Interval (Wall)"] + [f"{r.max_wall_interval_s:.4f}s" for r in runs]))
    lines.append(fmt % tuple(["Net Drift (Wall)"] + [f"{r.net_wall_drift_ms:+.2f} ms" for r in runs]))

    # If any run has monotonic telemetry, display monotonic row comparison
    if any(r.has_monotonic for r in runs):
        lines.append(sep)
        lines.append(fmt % tuple(["Jitter σ (Monotonic)"] + [
            f"±{r.jitter_mono_std_ms:.2f} ms" if r.jitter_mono_std_ms is not None else "N/A"
            for r in runs
        ]))
        lines.append(fmt % tuple(["Net Drift (Monotonic)"] + [
            f"{r.net_mono_drift_ms:+.2f} ms" if r.net_mono_drift_ms is not None else "N/A"
            for r in runs
        ]))

    # Overrun / Skip row
    if any(r.total_skipped_cycles > 0 for r in runs):
        lines.append(sep)
        lines.append(fmt % tuple(["Skipped Cycles"] + [f"{r.total_skipped_cycles}" for r in runs]))

    lines.append(sep)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Metrology run parser and reconstituter for clock-tick.sh benchmark outputs."
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="Path to benchmark stdout log file(s). If omitted or '-', reads from stdin.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output parsed run(s) and reconstituted metrics as JSON.",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Only display comparative summary table, omitting per-cycle details.",
    )
    parser.add_argument(
        "--details",
        action="store_true",
        help="Force detailed per-cycle reports for all runs.",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Write report output to specified file path instead of stdout.",
    )

    args = parser.parse_args()

    # Collect raw text from files or stdin
    raw_texts: List[str] = []
    if not args.files or args.files == ["-"]:
        raw_texts.append(sys.stdin.read())
    else:
        for fpath_str in args.files:
            fpath = Path(fpath_str)
            if not fpath.exists():
                print(f"Error: File not found '{fpath}'", file=sys.stderr)
                return 1
            raw_texts.append(fpath.read_text(encoding="utf-8"))

    # Parse all runs
    all_runs: List[RunResult] = []
    for raw in raw_texts:
        runs = parse_runs_from_text(raw)
        all_runs.extend(runs)

    if not all_runs:
        print("Error: No valid clock-tick benchmark runs found in input.", file=sys.stderr)
        return 1

    # Output generation
    output_text = ""
    if args.json:
        payload = [asdict(r) for r in all_runs]
        output_text = json.dumps(payload, indent=2)
    else:
        if len(all_runs) == 1 and not args.summary:
            output_text = format_single_run_report(all_runs[0])
        elif args.details:
            reports = [format_single_run_report(r) for r in all_runs]
            reports.append(format_comparison_table(all_runs))
            output_text = "\n\n".join(reports)
        else:
            # Multiple runs: comparison table by default
            output_text = format_comparison_table(all_runs)

    if args.output:
        Path(args.output).write_text(output_text, encoding="utf-8")
    else:
        print(output_text)

    return 0


if __name__ == "__main__":
    sys.exit(main())
