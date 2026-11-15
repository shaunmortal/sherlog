"""The testbed experiment's pure parts, testable without starting a server: the seeded fault plan, ten minutes of
collected telemetry turned into a `sherlog-incident/1` document, and the rollback decision.

The plan's truth (culprit, fault, onset, decoy) never enters a document; the runner stores it beside it.
"""
from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass

import byo
import investigate
import sim
from testbed.system import GRAPH, RPC, SERVICES, callers_of

PLAN_SEED = 2026
FAULTS = tuple(sim.FAULTS)
DECOY_CPU_DUTY = (0.80, 0.95)    # the batch job's share of a core, like the simulator's 80-95% CPU decoy
DECOY_WARMUP_MB = (40, 60)       # the cache warm-up: well above the 6 MB noise floor, and it slows no one down
ROLLBACK_TICKS = 24              # 2 minutes of real telemetry after a rollback ...
SETTLE_TICKS = 2                 # ... judged after the first 10 s, while requests already in flight finish
LEVELS = {"INFO": "INFO", "WARN": "WARN", "WARNING": "WARN", "ERROR": "ERROR", "CRITICAL": "ERROR"}

Reading = dict[str, float | None]   # metric -> value; None: no request finished in the tick, or no scrape


@dataclass(frozen=True)
class Outage:
    id: str
    hard: bool
    service: str
    fault: str
    onset: int
    decoy: str | None = None
    decoy_kind: str | None = None     # "cpu" (a batch job) or "memory" (a cache warm-up)
    decoy_level: float | None = None  # the batch job's duty cycle, or the warm-up's size in MB
    decoy_onset: int | None = None

    def truth(self) -> dict:
        return {"service": self.service, "fault": self.fault, "onset": self.onset, "decoy": self.decoy,
                "decoy_kind": self.decoy_kind, "decoy_level": self.decoy_level, "decoy_onset": self.decoy_onset}


def _outage(k: int, hard: bool, seed: int) -> Outage:
    """Outage k of its mode. Faults take turns; each fault visits the services in its own seeded order, so the
    first 32 of a mode never repeat a (service, fault) pair."""
    mode = "h" if hard else "n"
    fault = FAULTS[k % len(FAULTS)]
    order = random.Random(f"{seed}-{mode}-{fault}").sample(SERVICES, len(SERVICES))
    service = order[(k // len(FAULTS)) % len(SERVICES)]
    rng = random.Random(f"{seed}-{mode}-{k}")
    onset = rng.randint(*sim.ONSET_RANGE)
    oid = f"real-{mode}{k:02d}"  # says nothing about the culprit
    if not hard:
        return Outage(oid, False, service, fault, onset)
    # as in the simulator: a harmless anomaly on a service the fault cannot reach, starting with it, never before
    decoy = rng.choice([s for s in SERVICES if s != service and s not in callers_of(service)])
    if rng.random() < sim.DECOY_CPU_SHARE:
        kind, level = "cpu", round(rng.uniform(*DECOY_CPU_DUTY), 2)
    else:
        kind, level = "memory", float(rng.randint(*DECOY_WARMUP_MB))
    return Outage(oid, True, service, fault, onset, decoy, kind, level, onset + rng.randint(0, 2))


def plan(normal: int, hard: int, seed: int = PLAN_SEED) -> list[Outage]:
    """The experiment's outages in the order they run: two normal, then one hard, so an experiment cut short
    still has both. Outage k is the same however many are planned, so a bigger plan extends a smaller one."""
    normals = [_outage(k, False, seed) for k in range(normal)]
    hards = [_outage(k, True, seed) for k in range(hard)]
    out = []
    while normals or hards:
        out += normals[:2] + hards[:1]
        normals, hards = normals[2:], hards[1:]
    return out


# --- telemetry -> document -------------------------------------------------------------------

def _reading(value: float, metric: str) -> float:
    """Clamped to the metric's limits and rounded as the simulator's monitoring records it."""
    lo, hi = sim.LIMITS[metric]
    value = max(lo, value) if hi is None else min(hi, max(lo, value))
    return round(value, sim.DECIMALS[metric])


def series_of(samples: list[dict[str, Reading]]) -> tuple[dict[str, dict[str, list[float]]], int]:
    """Per-tick readings as series, and how many readings were missing. A missing reading holds the one before
    it (the first ones take the first real reading); a series with no reading at all is refused."""
    series, filled = {}, 0
    for s in SERVICES:
        series[s] = {}
        for m in sim.METRICS:
            raw = [tick[s][m] for tick in samples]
            known = [v for v in raw if v is not None]
            if not known:
                raise ValueError(f"{s} reported no {m} at all")
            last, values = known[0], []
            for v in raw:
                filled += v is None
                last = last if v is None else v
                values.append(_reading(last, m))
            series[s][m] = values
    return series, filled


def window_logs(logs: list[tuple[float, str, str, str]], start: float,
                tick_seconds: float = sim.TICK_SECONDS) -> list[dict]:
    """(unix time, service, level, text) lines -> the document's log lines: those inside the window, oldest
    first. Beyond byo.MAX_LOG_LINES, WARN and ERROR lines are kept first, then the newest INFO lines."""
    lines = []
    for when, service, level, text in sorted(logs):
        tick = math.floor((when - start) / tick_seconds)
        text = text.strip()[:byo.MAX_LOG_CHARS]
        if 0 <= tick < sim.WINDOW_TICKS and level in LEVELS and text and service in SERVICES:
            lines.append({"tick": tick, "service": service, "level": LEVELS[level], "text": text})
    if len(lines) <= byo.MAX_LOG_LINES:
        return lines
    loud = [i for i, line in enumerate(lines) if line["level"] != "INFO"][-byo.MAX_LOG_LINES:]
    quiet = [i for i, line in enumerate(lines) if line["level"] == "INFO"]
    room = byo.MAX_LOG_LINES - len(loud)
    keep = set(loud) | set(quiet[len(quiet) - room:] if room else [])
    return [line for i, line in enumerate(lines) if i in keep]


def to_document(samples: list[dict[str, Reading]], logs: list[tuple[float, str, str, str]], start: float,
                tick_seconds: float = sim.TICK_SECONDS) -> tuple[dict, int]:
    """The window as an upload with the testbed's own call graph and RPC names, and the count of filled
    readings. `samples` holds one reading per tick and service, `start` is the window's first second."""
    if len(samples) != sim.WINDOW_TICKS:
        raise ValueError(f"a document holds {sim.WINDOW_TICKS} ticks, not {len(samples)}")
    series, filled = series_of(samples)
    doc = {"format": byo.FORMAT, "tick_seconds": sim.TICK_SECONDS, "calls": GRAPH, "rpc": RPC, "series": series,
           "logs": window_logs(logs, start, tick_seconds)}
    return doc, filled


def incident(doc: dict) -> sim.Incident:
    """The document as the server reads it: the same strict validation as POST /api/triage."""
    return byo.to_incident(byo.IncidentDoc.model_validate_json(json.dumps(doc)))


# --- the rollback decision -------------------------------------------------------------------

def _sustained(values: list[float], mean: float, std: float) -> bool:
    run = 0
    for v in values:
        run = run + 1 if (v - mean) / std >= sim.ALARM_Z else 0
        if run >= sim.ALARM_SUSTAIN_TICKS:
            return True
    return False


def rollback_decision(doc: dict, after: dict[str, dict[str, list[float]]]) -> dict:
    """Did rolling the service back stop the alarm? `after` holds the ROLLBACK_TICKS readings that followed the
    rollback. The app's alarm rule against the window's own pre-fault baseline: a metric ALARM_Z standard
    deviations above normal for ALARM_SUSTAIN_TICKS ticks in a row, judged after SETTLE_TICKS. As in
    investigate.rollback_check, it also proves the answer when every latency and error alarm ends while a CPU
    or memory anomaly that slows nobody down (hard mode's decoy) keeps the alarm on."""
    inc = incident(doc)
    still = [(s, m) for s in inc.services for m in sim.METRICS
             if _sustained(after[s][m][SETTLE_TICKS:], *inc.stats[(s, m)])]
    harm_before = inc.alarm_on(investigate.PROPAGATING) is not None
    harm_after = any(m in investigate.PROPAGATING for _, m in still)
    harm_ended = harm_before and not harm_after
    return {"alarm_after": bool(still), "still_alarming": [f"{s}.{m}" for s, m in still],
            "harm_ended": harm_ended, "proves": not still or harm_ended}
