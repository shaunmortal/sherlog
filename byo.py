"""Bring your own outage: a strict document holding 10 minutes of real telemetry, turned into an Incident so the
same alarm, tools, verifier and blast-radius check run on it. See docs/bring-your-own.md.

The dependency tool and the blast-radius check reason over a call graph. A document brings its own in `calls`
(each service and the services it calls), or leaves it out and maps its services onto the shop's 11 (sim.CALLS).
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import (AfterValidator, BaseModel, ConfigDict, Field, FiniteFloat, TypeAdapter, ValidationError,
                      ValidationInfo, create_model, field_validator)
from pydantic_core import InitErrorDetails, PydanticCustomError

import investigate
import sim

FORMAT = "sherlog-incident/1"
LEVELS = ("INFO", "WARN", "ERROR")
MAX_LOG_LINES = 2000
MAX_LOG_CHARS = 500
MIN_SERVICES, MAX_SERVICES = 2, 40
MAX_NAME_CHARS = 40
# lowercase words joined by dashes, so a name reads the same in a log line, a URL or a metric label
SERVICE_NAME = re.compile(rf"[a-z](?:[a-z0-9-]{{0,{MAX_NAME_CHARS - 2}}}[a-z0-9])?")
RPC_NAME = re.compile(rf"[A-Za-z][A-Za-z0-9_]{{0,{MAX_NAME_CHARS - 1}}}")
# the downloadable sample: the classic ranking blames cart, a loud victim; the blast-radius check points at cache
SAMPLE = ("cache", "latency", 5)
# the custom-graph sample: six of the sample's services under other names and RPCs, wired as their own call graph
GRAPH_SAMPLE = {"frontend": ("gateway", "Route"), "catalog": ("auth", "Verify"), "checkout": ("orders", "PlaceOrder"),
                "cart": ("inventory", "Reserve"), "payment": ("payments", "Charge"), "cache": ("postgres", "Query")}

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


@lru_cache(maxsize=32)
def _series_model(services: tuple[str, ...]) -> type[BaseModel]:
    """Every one of these services, no other, each with every metric. The fields are aliased because a name like
    api-gateway is not a Python identifier."""
    fields = {f"s{i}": (Metrics, Field(alias=s)) for i, s in enumerate(services)}
    return create_model("Series", __config__=_STRICT, **fields)


def _service_name(name: str) -> str:
    if not SERVICE_NAME.fullmatch(name):
        raise ValueError(f"{name!r} is not a valid service name: use 1 to {MAX_NAME_CHARS} lowercase letters, digits "
                         "and dashes, starting with a letter and not ending with a dash")
    return name


def _rpc_name(name: str) -> str:
    if not RPC_NAME.fullmatch(name):
        raise ValueError(f"{name!r} is not a valid RPC name: use 1 to {MAX_NAME_CHARS} letters, digits and "
                         "underscores, starting with a letter")
    return name


ServiceName = Annotated[str, AfterValidator(_service_name)]


def _graph(calls: dict[str, list[str]]) -> dict[str, list[str]]:
    """Every callee is one of the services; no service calls itself or lists a callee twice."""
    for service, callees in calls.items():
        if service in callees:
            raise ValueError(f"{service} calls itself")
        unknown = next((c for c in callees if c not in calls), None)
        if unknown:
            raise ValueError(f"{service} calls {unknown}, which is not one of the services in calls; add it, with "
                             "the services it calls (or [])")
        twice = next((c for c in callees if callees.count(c) > 1), None)
        if twice:
            raise ValueError(f"{service} lists {twice} twice")
    return calls


# service -> the services it calls; its keys are the document's services
Calls = Annotated[dict[ServiceName, list[ServiceName]], Field(min_length=MIN_SERVICES, max_length=MAX_SERVICES),
                  AfterValidator(_graph)]


def services_of(calls: dict | None) -> tuple[str, ...]:
    return sim.SERVICES if calls is None else tuple(calls)


class LogEntry(BaseModel):
    model_config = _STRICT
    tick: int = Field(ge=0, lt=sim.WINDOW_TICKS)
    service: str = Field(min_length=1, max_length=MAX_NAME_CHARS)  # who wrote the line: one of the services
    level: Literal[LEVELS]
    text: str = Field(min_length=1, max_length=MAX_LOG_CHARS)


class IncidentDoc(BaseModel):
    model_config = _STRICT
    format: Literal[FORMAT]
    tick_seconds: Literal[sim.TICK_SECONDS]
    # before series, rpc and logs, which are checked against its services
    calls: Calls | None = None
    rpc: dict[ServiceName, Annotated[str, AfterValidator(_rpc_name)]] | None = None
    series: dict[str, Metrics]
    logs: list[LogEntry] = Field(default_factory=list, max_length=MAX_LOG_LINES)

    @field_validator("series", mode="plain", json_schema_input_type=dict[str, Metrics])
    @classmethod
    def _series_of_these_services(cls, series, info: ValidationInfo) -> dict:
        if "calls" not in info.data:  # calls was refused, so there is nothing to check the series against
            return series
        return _series_model(services_of(info.data["calls"])).model_validate(series).model_dump(by_alias=True)

    @field_validator("rpc")
    @classmethod
    def _rpc_of_these_services(cls, rpc: dict[str, str] | None, info: ValidationInfo) -> dict[str, str] | None:
        if rpc is None or "calls" not in info.data:
            return rpc
        calls = info.data["calls"]
        if calls is None:
            raise ValueError("rpc needs calls: without them the services are the shop's, whose RPC names are fixed")
        unknown = next((s for s in rpc if s not in calls), None)
        if unknown:
            raise ValueError(f"{unknown} is not one of the services in calls")
        owners: dict[str, str] = {}
        for service, name in rpc.items():
            if name in calls or name in owners:
                raise ValueError(f"{name} names both {owners.get(name, name)} and {service}; give each its own name")
            owners = {**owners, name: service}
        return rpc

    @field_validator("logs")
    @classmethod
    def _logs_by_these_services(cls, logs: list[LogEntry], info: ValidationInfo) -> list[LogEntry]:
        if "calls" not in info.data:
            return logs
        calls = info.data["calls"]
        services = services_of(calls)
        where = (f"the {len(services)} services in calls" if calls is not None else
                 f"the shop's {len(services)} services; map it onto one of them, or send your own call graph in calls")
        problems = [InitErrorDetails(type=PydanticCustomError("unknown_service", "{service} is not one of {where}",
                                                               {"service": repr(e.service), "where": where}),
                                     loc=(i, "service"), input=e.service)
                    for i, e in enumerate(logs) if e.service not in services]
        if problems:
            raise ValidationError.from_exception_data("logs", problems)
        return logs


_CALLS = TypeAdapter(Calls, config=_STRICT)


def read_calls(data) -> dict[str, list[str]]:
    """A call graph on its own, checked as the server checks `calls`."""
    try:
        return _CALLS.validate_python(data)
    except ValidationError as exc:
        raise ValueError("; ".join(f"calls{''.join(f'.{x}' for x in e['loc'])}: {e['msg']}"
                                   for e in exc.errors())) from None


def naming(services: tuple[str, ...], rpc: dict[str, str] | None = None) -> dict[str, str]:
    """Every word that names a service: its name, or its RPC alone ("rpc payment.Charge failed", "Charge failed")."""
    return {**{s: s for s in services}, **{name: s for s, name in (rpc or {}).items()}}


SHOP_NAMES = naming(sim.SERVICES, sim.RPC)


@lru_cache(maxsize=32)
def _mention(words: tuple[str, ...]) -> re.Pattern:
    # the longest first, so a line naming api-gateway is about api-gateway, not api
    return re.compile(r"\b(" + "|".join(map(re.escape, sorted(words, key=len, reverse=True))) + r")\b")


def about(writer: str, text: str, names: dict[str, str] = SHOP_NAMES) -> str:
    """The service a log line implicates: the first other service it names, as a whole word, case-sensitive, by
    name or RPC; otherwise the service that wrote it. A caller's complaint about an RPC points at the callee."""
    return next((names[word] for word in _mention(tuple(names)).findall(text) if names[word] != writer), writer)


def to_incident(doc: IncidentDoc) -> sim.Incident:
    """An Incident whose truth is unknown: no root, fault or decoy, so there is nothing to score it against.
    Seed and onset only mean something for a simulated outage; nothing outside the simulator reads them."""
    calls = None if doc.calls is None else {s: tuple(c) for s, c in doc.calls.items()}
    names = SHOP_NAMES if calls is None else naming(tuple(calls), doc.rpc)
    series = {s: {m: tuple(v) for m, v in metrics.items()} for s, metrics in doc.series.items()}
    logs = tuple(sim.LogLine(e.tick, e.service, e.level, e.text, about(e.service, e.text, names))
                 for e in sorted(doc.logs, key=lambda e: e.tick))  # the log tool shows the newest lines last
    return sim.Incident(root=None, fault=None, seed=0, onset=-1, series=series, logs=logs, calls=calls)


def export(inc: sim.Incident) -> dict:
    """An outage as a document, without what only the simulator knows: the culprit, fault, seed, decoy, and each
    line's "about" (a reader derives that from the text). An upload's own call graph is kept."""
    graph = {} if inc.calls is None else {"calls": {s: list(c) for s, c in inc.calls.items()}}
    return {
        "format": FORMAT,
        "tick_seconds": sim.TICK_SECONDS,
        **graph,
        "series": {s: {m: list(inc.series[s][m]) for m in sim.METRICS} for s in inc.services},
        "logs": [{"tick": x.tick, "service": x.service, "level": x.level, "text": x.text} for x in inc.logs],
    }


def sample() -> dict:
    return export(sim.simulate(*SAMPLE))


def graph_sample() -> dict:
    """An upload from a system that is not the shop: the sample outage cut down to six of its services, renamed,
    with the calls among them as its own call graph. Log lines naming a service left out are dropped."""
    doc = sample()
    words = {**{old: new for old, (new, _) in GRAPH_SAMPLE.items()},
             **{sim.RPC[old]: rpc for old, (_, rpc) in GRAPH_SAMPLE.items()}}
    shop_word = _mention(tuple(SHOP_NAMES))

    def renamed(text: str) -> str | None:
        if any(word not in words for word in shop_word.findall(text)):
            return None
        return shop_word.sub(lambda m: words[m.group()], text)

    kept = [s for s in sim.SERVICES if s in GRAPH_SAMPLE]
    lines = [(line, renamed(line["text"])) for line in doc["logs"] if line["service"] in GRAPH_SAMPLE]
    return {
        "format": FORMAT,
        "tick_seconds": sim.TICK_SECONDS,
        "calls": {words[s]: [words[c] for c in sim.CALLS[s] if c in GRAPH_SAMPLE] for s in kept},
        "rpc": dict(GRAPH_SAMPLE.values()),
        "series": {words[s]: doc["series"][s] for s in kept},
        "logs": [{**line, "service": words[line["service"]], "text": text} for line, text in lines if text],
    }


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
