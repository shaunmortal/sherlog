"""Turn Prometheus query_range answers into a Sherlog incident document (see docs/bring-your-own.md).

    python scripts/prom_to_sherlog.py latency_ms=latency.json error_rate=errors.json \\
        cpu_pct=cpu.json memory_mb=memory.json --map cartservice=cart -o incident.json
    python scripts/prom_to_sherlog.py all.json --service-label app -o incident.json
    python scripts/prom_to_sherlog.py all.json --calls calls.json -o incident.json

A METRIC=FILE input holds one Sherlog metric; in a bare FILE every series says which metric it is in a "metric"
label (add one with label_replace). The 10 minutes up to the newest sample (or --end) become 120 ticks of 5 s;
a single missing reading is filled with a straight line, longer gaps are refused rather than invented.
--calls gives your own call graph, {"service": ["services it calls"], ...}; without it the services must be the
shop's 11.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # byo and sim live at the repository root
import byo  # noqa: E402
import sim  # noqa: E402

# Only one missing reading in a row is ever filled: the alarm needs three abnormal readings in a row, and two
# invented ones beside a single real spike could fake that.
MAX_GAP_SECONDS = 2 * sim.TICK_SECONDS  # between two samples
MAX_EDGE_SECONDS = sim.TICK_SECONDS     # before the first sample or after the last, where the nearest is held
METRIC_LABEL = "metric"

Points = list[tuple[float, float]]


def _when(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%H:%M:%S UTC")


def load_matrix(answer: dict, metric: str | None, service_label: str, rename: dict[str, str],
                services: tuple[str, ...] = sim.SERVICES) -> dict[tuple[str, str], Points]:
    """(service, metric) -> its samples in time order, from one query_range answer. NaN and Inf samples are
    dropped, so they become gaps."""
    data = answer.get("data") or {}
    if answer.get("status") != "success" or data.get("resultType") != "matrix":
        raise ValueError('expected a successful query_range answer with resultType "matrix"')
    out: dict[tuple[str, str], Points] = {}
    for result in data.get("result", []):
        labels = result.get("metric", {})
        raw = labels.get(service_label)
        service, name = rename.get(raw, raw), metric or labels.get(METRIC_LABEL)
        if service not in services:
            fix = ("add it to --calls, rename it with --map OLD=NEW" if services != sim.SERVICES else
                   f"map it onto one of {', '.join(sim.SERVICES)} with --map OLD=NEW, give your own call graph "
                   "with --calls")
            raise ValueError(f"series {labels} has service {service!r} (label {service_label!r}); {fix}, or leave it "
                             "out of the query")
        if name not in sim.METRICS:
            raise ValueError(f"series {labels} has metric {name!r}; give the file as METRIC=FILE or set a "
                             f"{METRIC_LABEL!r} label to one of {', '.join(sim.METRICS)}")
        if (service, name) in out:
            raise ValueError(f"two series for {service} {name}: aggregate them in the query, e.g. "
                             f"avg by ({service_label}) (...)")
        points = sorted((float(t), float(v)) for t, v in result.get("values", []) if math.isfinite(float(v)))
        if not points:
            raise ValueError(f"{service} {name} has no finite samples")
        out[(service, name)] = points
    return out


def resample(points: Points, ticks: list[float], where: str) -> list[float]:
    """The value at every tick: the sample taken then, else a straight line between the samples around it, else
    the nearest one at the window's edge. More than one missing reading in a row is refused."""
    times = [t for t, _ in points]
    out = []
    for tick in ticks:
        i = bisect.bisect_right(times, tick)
        before, after = (points[i - 1] if i else None), (points[i] if i < len(points) else None)
        if before and before[0] == tick:
            out.append(before[1])
            continue
        start = before[0] if before else tick
        end = after[0] if after else tick
        if end - start > (MAX_GAP_SECONDS if before and after else MAX_EDGE_SECONDS):
            raise ValueError(f"{where} has no samples from {_when(start)} to {_when(end)} ({end - start:.0f} s); only "
                             f"one missing reading is filled, so query with step={sim.TICK_SECONDS}s")
        if before and after:
            out.append(before[1] + (after[1] - before[1]) * (tick - start) / (end - start))
        else:
            out.append((before or after)[1])
    return out


def _clamp(metric: str, value: float) -> float:
    """Into the metric's limits, as the simulator's own monitoring does (no latency below 0.1 ms, say)."""
    lo, hi = sim.LIMITS[metric]
    return max(lo, value) if hi is None else min(hi, max(lo, value))


def convert(series: dict[tuple[str, str], Points], end: float | None = None,
            calls: dict[str, list[str]] | None = None) -> dict:
    """The incident document for the 10 minutes up to `end` (default: the newest sample), over the services of
    `calls`, the team's own call graph, or else the shop's."""
    services = byo.services_of(calls)
    missing = [f"{s} {m}" for s in services for m in sim.METRICS if (s, m) not in series]
    if missing:
        raise ValueError(f"no data for {len(missing)} series: {', '.join(missing)}")
    end = max(points[-1][0] for points in series.values()) if end is None else end
    ticks = [end - (sim.WINDOW_TICKS - 1 - k) * sim.TICK_SECONDS for k in range(sim.WINDOW_TICKS)]
    doc = {
        "format": byo.FORMAT,
        "tick_seconds": sim.TICK_SECONDS,
        **({} if calls is None else {"calls": calls}),
        "series": {s: {m: [_clamp(m, v) for v in resample(series[(s, m)], ticks, f"{s} {m}")] for m in sim.METRICS}
                   for s in services},
        "logs": [],
    }
    byo.IncidentDoc.model_validate(doc)  # the same checks the server runs, so a written file is always accepted
    return doc


def _pairs(items: list[str]) -> dict[str, str]:
    pairs = [item.partition("=") for item in items]
    bad = [item for item, (old, sep, new) in zip(items, pairs) if not (old and sep and new)]
    if bad:
        raise ValueError(f"--map takes OLD=NEW, not {', '.join(bad)}")
    return {old: new for old, _, new in pairs}


def _load_inputs(inputs: list[str], service_label: str, rename: dict[str, str],
                 services: tuple[str, ...]) -> dict[tuple[str, str], Points]:
    series: dict[tuple[str, str], Points] = {}
    for item in inputs:
        metric, sep, path = item.partition("=")
        if not sep:
            metric, path = None, item
        elif metric not in sim.METRICS:
            raise ValueError(f"unknown metric {metric!r} in {item!r}; known: {', '.join(sim.METRICS)}")
        found = load_matrix(json.loads(Path(path).read_text()), metric, service_label, rename, services)
        twice = sorted(series.keys() & found.keys())
        if twice:
            raise ValueError(f"{path} repeats series already read: {', '.join(f'{s} {m}' for s, m in twice)}")
        series = {**series, **found}
    return series


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("inputs", nargs="+", metavar="[METRIC=]FILE", help="query_range answers, as JSON")
    parser.add_argument("--service-label", default="service", help="the label naming the service (default: service)")
    parser.add_argument("--map", action="append", default=[], metavar="OLD=NEW",
                        help="rename a service, e.g. onto the shop's names: cartservice=cart (repeatable)")
    parser.add_argument("--calls", type=Path, metavar="FILE",
                        help='your own call graph as JSON, {"service": ["services it calls"], ...}; '
                             "without it the services must be the shop's 11")
    parser.add_argument("--end", type=float, help="unix time of the last tick (default: the newest sample)")
    parser.add_argument("-o", "--output", type=Path, help="write here instead of to standard output")
    args = parser.parse_args(argv)
    try:
        calls = None if args.calls is None else byo.read_calls(json.loads(args.calls.read_text()))
        series = _load_inputs(args.inputs, args.service_label, _pairs(args.map), byo.services_of(calls))
        doc = convert(series, args.end, calls)
    except (OSError, ValueError, TypeError) as exc:  # pydantic's ValidationError is a ValueError
        sys.exit(f"prom_to_sherlog: {exc}")
    text = json.dumps(doc)
    if args.output is None:
        print(text)
    else:
        args.output.write_text(text)
        print(f"wrote {args.output}: {sim.WINDOW_TICKS} ticks of {sim.TICK_SECONDS} s", file=sys.stderr)


if __name__ == "__main__":
    main()
