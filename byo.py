"""Bring your own outage: a strict document holding 10 minutes of real telemetry, turned into an Incident so the
same alarm, tools, verifier and blast-radius check run on it. See docs/bring-your-own.md.

The topology is fixed: the services must be mapped onto the shop's 11 names (sim.CALLS), because the dependency
tool and the blast-radius check reason over that call graph.
"""
from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, FiniteFloat, create_model

import investigate
import sim

FORMAT = "sherlog-incident/1"
LEVELS = ("INFO", "WARN", "ERROR")
MAX_LOG_LINES = 2000
MAX_LOG_CHARS = 500
# the downloadable sample: the classic ranking blames cart, a loud victim; the blast-radius check points at cache
SAMPLE = ("cache", "latency", 5)

_STRICT = ConfigDict(extra="forbid", strict=True)  # a typo must fail loudly, not be dropped or coerced


def _within_limits(metric: str):
    lo, hi = sim.LIMITS[metric]
    allowed = f"at least {lo:g}" if hi is None else f"between {lo:g} and {hi:g}"

    def check(values: list[float]) -> list[float]:
        bad = next((t for t, v in enumerate(values) if v < lo or (hi is not None and v > hi)), None)
        if bad is not None:
            raise ValueError(f"tick {bad} is {values[bad]:g}, but {metric} must be {allowed}")
        return values

    return check


def _ticks(metric: str):
    """Exactly one finite reading per tick, oldest first, within the metric's limits."""
    return Annotated[list[FiniteFloat], Field(min_length=sim.WINDOW_TICKS, max_length=sim.WINDOW_TICKS),
                     AfterValidator(_within_limits(metric))]


Metrics = create_model("Metrics", __config__=_STRICT, **{m: (_ticks(m), ...) for m in sim.METRICS})
Series = create_model("Series", __config__=_STRICT, **{s: (Metrics, ...) for s in sim.SERVICES})


class LogEntry(BaseModel):
    model_config = _STRICT
    tick: int = Field(ge=0, lt=sim.WINDOW_TICKS)
    service: Literal[sim.SERVICES]  # who wrote the line
    level: Literal[LEVELS]
    text: str = Field(min_length=1, max_length=MAX_LOG_CHARS)


class IncidentDoc(BaseModel):
    model_config = _STRICT
    format: Literal[FORMAT]
    tick_seconds: Literal[sim.TICK_SECONDS]
    series: Series
    logs: list[LogEntry] = Field(default_factory=list, max_length=MAX_LOG_LINES)


# every way a line can name a service: by name ("rpc payment.Charge failed") or by its RPC alone ("Charge failed")
_NAMES = {**{s: s for s in sim.SERVICES}, **{rpc: s for s, rpc in sim.RPC.items()}}
_MENTION = re.compile(r"\b(" + "|".join(map(re.escape, _NAMES)) + r")\b")


def about(writer: str, text: str) -> str:
    """The service a log line implicates: the first other service it names, as a whole word, case-sensitive, by
    name or RPC; otherwise the service that wrote it. A caller's complaint about an RPC points at the callee."""
    return next((_NAMES[word] for word in _MENTION.findall(text) if _NAMES[word] != writer), writer)


def to_incident(doc: IncidentDoc) -> sim.Incident:
    """An Incident whose truth is unknown: no root, fault or decoy, so there is nothing to score it against.
    Seed and onset only mean something for a simulated outage; nothing outside the simulator reads them."""
    series = {s: {m: tuple(v) for m, v in metrics.items()} for s, metrics in doc.series.model_dump().items()}
    logs = tuple(sim.LogLine(e.tick, e.service, e.level, e.text, about(e.service, e.text))
                 for e in sorted(doc.logs, key=lambda e: e.tick))  # the log tool shows the newest lines last
    return sim.Incident(root=None, fault=None, seed=0, onset=-1, series=series, logs=logs)


def export(inc: sim.Incident) -> dict:
    """A simulated outage as a document, without what only the simulator knows: the culprit, fault, seed, decoy,
    and each line's "about" (a reader derives that from the text)."""
    return {
        "format": FORMAT,
        "tick_seconds": sim.TICK_SECONDS,
        "series": {s: {m: list(inc.series[s][m]) for m in sim.METRICS} for s in sim.SERVICES},
        "logs": [{"tick": x.tick, "service": x.service, "level": x.level, "text": x.text} for x in inc.logs],
    }


def sample() -> dict:
    return export(sim.simulate(*SAMPLE))


def triage(inc: sim.Incident) -> dict:
    """The no-AI checks, instant and free: when the alarm fired, both rankings, and whether the classic ranking's
    top suspect accounts for everything abnormal (if not, `blast.better` names a service that explains more)."""
    ranking = sim.zscore_ranking(inc)
    return {
        "alarm": inc.alarm,
        "now": inc.now,
        "tick_seconds": sim.TICK_SECONDS,
        "zscore_ranking": [{"service": s, "metric": m, "z": round(z, 1)} for s in ranking for m, z in [inc.worst(s)]],
        "robust_ranking": sim.robust_ranking(inc),
        "blast": investigate.blast_radius(inc, ranking[0]),
    }
