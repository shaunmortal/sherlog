"""One benchmark case as a Sherlog incident document (sherlog-incident/1, see docs/bring-your-own.md).

Pure functions over plain rows, so they are tested without pandas (tests/test_benchmark_adapter.py); convert.py
reads the files and calls them. Nothing here sees a label: the inputs are a case's telemetry, its injection time
and its candidate services, as the benchmark's blind task view hands them to every method.

The window: 120 ticks of 5 s, the injection at tick 60. The 60 ticks before it are Sherlog's healthy baseline,
the 60 after it the outage. Every method in the benchmark is told the injection time; Sherlog is told it this way.
"""
from __future__ import annotations

import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # sim and byo live at the repository root
import byo  # noqa: E402
import sim  # noqa: E402

TICKS, BASELINE, TICK = sim.WINDOW_TICKS, sim.BASELINE_TICKS, sim.TICK_SECONDS

# the benchmark's metric kind -> Sherlog's metric name. p50 latency is dropped and p90 kept, as the benchmark's
# own runner does for every method; latency becomes milliseconds so it keeps Sherlog's unit, floor and limits
KINDS = {"latency-90": "latency_ms", "cpu": "cpu", "mem": "mem", "diskio": "diskio", "socket": "socket",
         "workload": "workload", "error": "errors"}
DROPPED_KINDS = ("latency-50",)
SCALE = {"latency_ms": 1000.0}  # seconds -> milliseconds
# a service that reports both its failed requests and all its requests gets Sherlog's error_rate, the share failed
ORDER = ("latency_ms", "error_rate", "errors", "cpu", "mem", "diskio", "socket", "workload")


def window_start(inject_time: int) -> int:
    """Unix second at which tick 0 starts: the injection lands exactly on the first tick after the baseline."""
    return inject_time - BASELINE * TICK


def service_name(raw: str) -> str:
    """A name as an upload must spell it: a few proxy pseudo-services in the metrics are spelt in CamelCase
    ('PassthroughCluster'); the benchmark's scorer lower-cases every name too, so nothing it scores changes."""
    name = re.sub(r"[^a-z0-9-]+", "-", raw.lower()).strip("-")[:byo.MAX_NAME_CHARS].rstrip("-")
    return name or "unnamed"


def split_column(column: str) -> tuple[str, str] | None:
    """'ts-order-service_latency-90' -> ('ts-order-service', 'latency_ms'); None for time and dropped kinds."""
    if column in ("time", "time.1") or "_" not in column:
        return None
    service, kind = column.rsplit("_", 1)
    if kind in DROPPED_KINDS or kind not in KINDS:
        return None
    return service_name(service), KINDS[kind]


def resample(times: list[float], values: list[float], start: int) -> list[float] | None:
    """Mean of each 5 s tick from `start`. A tick with no reading holds the previous tick (the first ticks take
    the first reading); a series with no reading in the window at all is None."""
    sums, counts = [0.0] * TICKS, [0] * TICKS
    for t, v in zip(times, values):
        k = int((t - start) // TICK)
        if 0 <= k < TICKS and v is not None and math.isfinite(v):
            sums[k] += v
            counts[k] += 1
    if not any(counts):
        return None
    first = next(sums[k] / counts[k] for k in range(TICKS) if counts[k])
    out, last = [], first
    for k in range(TICKS):
        last = sums[k] / counts[k] if counts[k] else last
        out.append(last)
    return out


def _clamp(metric: str, value: float) -> float:
    """Into Sherlog's limits for its own metric names (a service with no traffic reports 0 ms latency, under the
    0.1 ms floor), as the Prometheus converter does; values keep 6 significant digits."""
    lo, hi = sim.LIMITS.get(metric, (None, None))
    value = value if lo is None else max(lo, value)
    return value if hi is None else min(hi, value)


def series_of(columns: dict[str, list[float]], times: list[float], start: int) -> dict[str, dict[str, list[float]]]:
    """service -> Sherlog metric -> 120 readings, from the benchmark's metric columns (1 s rows)."""
    out: dict[str, dict[str, list[float]]] = {}
    for column, values in columns.items():
        parsed = split_column(column)
        if parsed is None:
            continue
        service, metric = parsed
        ticks = resample(times, values, start)
        if ticks is not None:
            scale = SCALE.get(metric, 1.0)
            out.setdefault(service, {})[metric] = [_clamp(metric, float(f"{v * scale:.6g}")) for v in ticks]
    for service, metrics in out.items():
        if "errors" in metrics and "workload" in metrics:
            failed, total = metrics.pop("errors"), metrics["workload"]
            metrics["error_rate"] = [min(1.0, f / n) if n > 0 else 0.0 for f, n in zip(failed, total)]
    return {s: {m: out[s][m] for m in ORDER if m in out[s]} for s in sorted(out)}


def trace_name(name: str, services: tuple[str, ...]) -> str:
    """A traced service under its metrics name: 'frontendservice' is 'frontend' when only that one has metrics."""
    short = name.removesuffix("service")
    return short if name not in services and short in services else name


def calls_from_spans(spans: list[tuple[str, str, str]], services: tuple[str, ...]) -> dict[str, set[str]]:
    """Caller -> callees among `services`, from (span id, parent span id, service) rows. A call through a service
    that reports no metrics (a proxy, say) links its caller to its callees."""
    owner = {span: service for span, _, service in spans}
    return calls_from_edges({(owner[parent], service) for _, parent, service in spans if parent in owner}, services)


def calls_from_edges(pairs: set[tuple[str, str]], services: tuple[str, ...]) -> dict[str, set[str]]:
    """Caller -> callees among `services`, from traced (caller, callee) service pairs."""
    edges: dict[str, set[str]] = {}
    for caller, callee in pairs:
        caller, callee = (trace_name(service_name(x), services) for x in (caller, callee))
        if caller != callee:
            edges.setdefault(caller, set()).add(callee)
    known = set(services)
    out: dict[str, set[str]] = {s: set() for s in services}
    for caller in services:
        seen, todo = {caller}, list(edges.get(caller, ()))
        while todo:
            callee = todo.pop()
            if callee in seen:
                continue
            seen.add(callee)
            if callee in known:
                out[caller].add(callee)
            else:
                todo.extend(edges.get(callee, ()))
    return out


DATASTORE = re.compile(r"(.+?)-(db|mongo|mysql|other-mongo)$")


def datastore_calls(services: tuple[str, ...]) -> dict[str, set[str]]:
    """A datastore named after a service is that service's own: carts -> carts-db, ts-order-service -> ts-order-mongo
    (traces do not cover database calls). Only names are read."""
    out: dict[str, set[str]] = {s: set() for s in services}
    for store in services:
        m = DATASTORE.fullmatch(store)
        if not m:
            continue
        stem, kind = m.groups()
        names = (f"{stem}-other-service",) if kind == "other-mongo" else (stem, f"{stem}-service")
        for owner in (s for s in services if s != store and s in names):
            out[owner].add(store)
    return out


# Calls no trace shows, written by hand from each system's published architecture (docs/real-benchmark.md).
# The 15-service system records no traces at all, so its whole graph is here.
HAND_CALLS = {
    # 7 of its 12 services are traced; these calls involve the other five
    "re2-ob": {"frontend-external": ["frontend"], "frontend": ["adservice", "cartservice", "shippingservice"],
               "checkoutservice": ["cartservice", "shippingservice"], "cartservice": ["redis"]},
    "re2-ss": {
        "front-end": ["carts", "catalogue", "orders", "user", "session-db"],
        "orders": ["carts", "payment", "shipping", "user"],
        "shipping": ["rabbitmq"], "queue-master": ["rabbitmq"], "rabbitmq-exporter": ["rabbitmq"],
    },
    "re2-tt": {},
}


# the 68-service system's untraced gateway calls whatever service starts a trace (a span with no parent id). The
# 12-service system's traces start in internal services too, so its entry point is wired by hand above instead.
ENTRY = {"re2-tt": "ts-ui-dashboard"}


def hand_calls(dataset: str, services: tuple[str, ...]) -> dict[str, set[str]]:
    known = set(services)
    return {s: {c for c in HAND_CALLS.get(dataset, {}).get(s, []) if c in known} for s in services}


def merge(*graphs: dict[str, set[str]]) -> dict[str, list[str]]:
    services = sorted({s for g in graphs for s in g})
    return {s: sorted({c for g in graphs for c in g.get(s, ()) if c != s}) for s in services}


_LEVEL_FIELD = re.compile(r"\b(?:level|severity)[\"']?\s*[=:]\s*[\"']?([A-Za-z]+)", re.I)
_LEVEL_AFTER_TIME = re.compile(r"^\S+\s+\S+\s+(TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL)\b")
_LEVEL_FIRST = re.compile(r"^\[?(TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL)\b")
_ERROR_WORDS = re.compile(r"\b(exception|error|fatal|panic)\b", re.I)
_WARN_WORDS = re.compile(r"\b(fail|failed|failure|timeout|timed out|refused|unavailable|retrying)\b", re.I)


def level_of(text: str) -> str:
    """INFO, WARN or ERROR: the level a line states (logfmt, JSON or a timestamp-then-level prefix), else a guess
    from its words, since some services log no level at all."""
    m = _LEVEL_FIELD.search(text) or _LEVEL_AFTER_TIME.match(text) or _LEVEL_FIRST.match(text)
    if m:
        word = m.group(1).upper()
        return "ERROR" if word in ("ERROR", "ERR", "FATAL", "SEVERE", "CRITICAL", "CRIT") else \
            "WARN" if word in ("WARN", "WARNING") else "INFO"
    if _ERROR_WORDS.search(text):
        return "ERROR"
    return "WARN" if _WARN_WORDS.search(text) else "INFO"


def log_lines(rows: list[tuple[float, str, str]], services: tuple[str, ...], start: int) -> list[dict]:
    """(unix time, writer, text) rows -> the document's log lines: WARN and ERROR lines only, by services of the
    incident, in the window, oldest first, at most byo.MAX_LOG_LINES (the earliest, which are the ones before the
    agent's cut-off). The log tool shows only WARN and ERROR lines; INFO lines would only use up the allowance."""
    known = set(services)
    out = []
    for t, writer, text in sorted(rows, key=lambda r: r[0]):
        k, writer = int((t - start) // TICK), service_name(writer)
        if not 0 <= k < TICKS or writer not in known:
            continue
        flat = " ".join(str(text).split())[:byo.MAX_LOG_CHARS]
        if not flat:
            continue
        level = level_of(flat)
        if level != "INFO":
            out.append({"tick": k, "service": writer, "level": level, "text": flat})
        if len(out) == byo.MAX_LOG_LINES:
            break
    return out


def to_doc(series: dict, calls: dict[str, list[str]], logs: list[dict]) -> dict:
    """The sherlog-incident/1 document: every service that reports a metric, its calls among them, its lines."""
    services = tuple(series)
    metrics = [m for m in ORDER if any(m in series[s] for s in services)]
    return {
        "format": byo.FORMAT,
        "tick_seconds": TICK,
        "calls": {s: [c for c in calls.get(s, []) if c in series] for s in services},
        "metrics": metrics,
        "series": series,
        "logs": [line for line in logs if line["service"] in series],
    }
