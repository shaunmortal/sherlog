"""Sherlog as an MCP investigator: the caller's own model investigates an outage with the agent's read-only tools,
and check_answer puts its answer through the agent's own gate. app.py registers these as MCP tools and a prompt.

Stateless, as the endpoint is: every call names its incident again, and each result's citation id is a hash of the
incident, the tool and its arguments, so check_answer re-runs every cited call from its id alone.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Annotated, Callable, Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema, model_validator

import byo
import investigate
import mcp_server
import sim

CALL_ID_HEX = 10  # hex digits in a citation id: no two calls on even an 80-service upload share one in practice
MAX_CITATIONS = 20  # per suspect
MAX_CITATION_CHARS = 64
MAX_REASON_CHARS = 500
SHIP, HAND_OVER = "ship", "hand to a human"
CONFIDENT = f"{investigate.ANSWER_CONFIDENCE:.0%}"
NO_ROLLBACK = ("Uploaded telemetry gets no rollback replay: only a simulated outage can be replayed with the answer "
               f"rolled back, so this verdict rests on the citations, the {CONFIDENT} rule and the blast radius.")
SAMPLE_SHOP_NAMES = {new: old for old, (new, _) in byo.GRAPH_SAMPLE.items()}  # the custom sample's names -> the shop's
DEFAULT_OUTAGE = "41"  # the README's walk-through
# the upload's full schema is triage_incident's; repeated in every investigation tool it would bloat the tool list
DOCUMENT_SCHEMA = {"type": "object", "description": "A sherlog-incident/1 document: triage_incident's schema and "
                                                    "docs/bring-your-own.md describe it."}


class IncidentSpec(BaseModel):
    """Which outage: exactly one of the three."""
    model_config = ConfigDict(extra="forbid")
    recorded: int | None = Field(None, ge=1, description="A recorded outage's number, as list_recorded_outages lists "
                                                         "it. Its cause stays hidden until reveal_truth.")
    sample: Literal["shop", "custom"] | None = Field(None, description="A sample: the shop's 11 services, or six "
                                                                      "renamed services with their own call graph.")
    document: Annotated[byo.IncidentDoc, WithJsonSchema(DOCUMENT_SCHEMA)] | None = Field(
        None, description="Your own telemetry, checked as POST /api/triage checks it, with the same 2 MB cap.")

    @model_validator(mode="after")
    def _exactly_one(self) -> IncidentSpec:
        if sum(v is not None for v in (self.recorded, self.sample, self.document)) != 1:
            raise ValueError("give exactly one of recorded, sample or document")
        return self


INCIDENT = Field(description='The outage: {"recorded": 41}, {"sample": "shop"} or {"document": {...}}. Pass the same '
                             "one to every call of an investigation.")
SERVICE = Field(max_length=byo.MAX_NAME_CHARS, description="One of the incident's services; top_anomalies and "
                                                           "get_dependencies name them.")


class Look(BaseModel):
    model_config = ConfigDict(extra="forbid")
    incident: IncidentSpec = INCIDENT

    def call_args(self) -> dict:
        """The tool's own arguments, in the order the agent's tools take them."""
        return self.model_dump(exclude={"incident"})


class ServiceLook(Look):
    service: str = SERVICE


class MetricLook(ServiceLook):
    metric: str = Field(max_length=byo.MAX_NAME_CHARS, description="One of the metrics the service reports: "
                                                                   "latency_ms, error_rate, cpu_pct or memory_mb in "
                                                                   "the shop.")


class Suspect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service: str = SERVICE
    confidence: float = Field(ge=0, le=1, description="Your probability, 0 to 1, that this service is the cause.")
    evidence: list[Annotated[str, Field(max_length=MAX_CITATION_CHARS)]] = Field(
        max_length=MAX_CITATIONS, description="The call ids, such as c3f9a01b2e4, whose results show this service "
                                              "abnormal.")
    fault_type: Literal[investigate.FAULT_TYPES] = "unknown"
    reason: str = Field("", max_length=MAX_REASON_CHARS, description="One sentence: why this service.")


class CheckArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    incident: IncidentSpec = INCIDENT
    suspects: list[Suspect] = Field(max_length=investigate.MAX_SUSPECTS, description="Most likely first.")


class RevealArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recorded: int = Field(ge=1, description="The recorded outage's number, as passed in incident.recorded.")


@dataclass(frozen=True)
class Case:
    key: str  # what the incident's citation ids are derived from
    inc: sim.Incident
    rollback: Callable[[str], dict] | None  # a service -> the rollback replay's result; None for an upload


def recorded_case(number: int, record: dict) -> Case:
    inc = sim.simulate(record["root"], record["fault"], record["seed"], record.get("hard", False))
    return Case(f"recorded:{number}", inc, lambda service: investigate.rollback_check(inc, service))


def _upload(doc: dict) -> sim.Incident:
    return byo.to_incident(byo.IncidentDoc.model_validate_json(json.dumps(doc)))


def sample_case(graph: str, doc: dict) -> Case:
    """A sample, read as the upload it is; it is a simulated outage, so it can still be replayed rolled back."""
    inc = _upload(doc)

    def rollback(service: str) -> dict:
        rolled_back = SAMPLE_SHOP_NAMES[service] if graph == "custom" else service
        shop = byo.export(sim.simulate(*byo.SAMPLE, rolled_back=rolled_back))
        return investigate.rollback_check(inc, service, _upload(byo.graph_sample(shop) if graph == "custom" else shop))

    return Case(f"sample:{graph}", inc, rollback)


def document_case(doc: byo.IncidentDoc) -> Case:
    # the series validator hands back plain dicts, which pydantic would warn about while dumping them as they are
    dumped = doc.model_dump(mode="json", warnings=False)
    digest = hashlib.sha256(json.dumps(dumped, sort_keys=True).encode()).hexdigest()
    return Case(f"document:{digest}", byo.to_incident(doc), None)


def call_id(key: str, tool: str, args: dict) -> str:
    """A citation id: a hash of the incident, the tool and its arguments, the same on every server and every day."""
    digest = hashlib.sha256(json.dumps([key, tool, args], sort_keys=True).encode()).hexdigest()
    return f"c{digest[:CALL_ID_HEX]}"


def look(case: Case, tool: str, args: dict) -> dict:
    """One read-only call, with the text Sherlog's own agent gets, under its citation id."""
    cid = call_id(case.key, tool, args)
    call = investigate.run_tool(case.inc, tool, args, cid)
    if call.error:
        raise mcp_server.ToolError(f"Bad input. {call.text.removeprefix(f'[{cid}] error: ')}")
    return {"call_id": cid, "text": call.text}


def _possible_calls(inc: sim.Incident) -> Iterator[tuple[str, dict]]:
    yield "top_anomalies", {}
    for s in inc.services:
        yield from (("get_metric", {"service": s, "metric": m}) for m in inc.measured[s])
        yield "get_logs", {"service": s}
        yield "get_dependencies", {"service": s}


def _cited_calls(case: Case, cited: set[str]) -> dict[str, investigate.Call]:
    """Every cited call, re-run from its id alone. An id that names no call on this incident is left out, so the
    verifier reports it as a call never made."""
    if not cited:
        return {}
    calls = {call_id(case.key, tool, args): (tool, args) for tool, args in _possible_calls(case.inc)}
    return {cid: investigate.run_tool(case.inc, *calls[cid], cid) for cid in cited if cid in calls}


def check(case: Case, suspects: list[Suspect]) -> dict:
    """The agent's gate around anyone's answer: the citation verifier and the confidence rule, then the answer's
    blast radius and rollback replay, and whether it ships or goes to a human, with the reasons."""
    unknown = next((i for i, s in enumerate(suspects) if s.service not in case.inc.services), None)
    if unknown is not None:
        raise mcp_server.ToolError(f"Bad input. suspects.{unknown}.service: unknown service "
                                   f"{suspects[unknown].service!r}; known: {', '.join(case.inc.services)}")
    causes = [s.model_dump() for s in suspects]
    calls = _cited_calls(case, {str(e).strip("[] ") for c in causes for e in c["evidence"]})
    verdict = investigate.build_verdict({"causes": causes}, calls)
    answer = verdict["answer"]
    rollback = case.rollback(answer) if answer and case.rollback else None
    disproved = f"rolling back {answer} does not stop the alarm" if rollback and not rollback["proves"] else None
    reasons = [r for r in (investigate.handoff_reason(case.inc, verdict), disproved) if r]
    return {
        **{k: verdict[k] for k in ("causes", "answer", "escalation")},
        "blast": investigate.blast_radius(case.inc, answer) if answer else None,
        "rollback": rollback,
        "decision": HAND_OVER if reasons else SHIP,
        "reasons": reasons,
        **({"note": NO_ROLLBACK} if case.rollback is None else {}),
    }


# --- the investigate_outage prompt ---------------------------------------------------------------------------------

def outage_spec(outage: str, recorded: int) -> dict:
    """The prompt's one argument as an incident: a recorded outage's number, or a sample's name."""
    outage = outage.strip()
    if outage in ("shop", "custom"):
        return {"sample": outage}
    if outage.isdecimal() and len(outage) <= len(str(recorded)) and 1 <= int(outage) <= recorded:
        return {"recorded": int(outage)}
    raise ValueError(f"outage must be a recorded outage's number, 1 to {recorded}, or shop or custom for a sample.")


def prompt_text(spec: dict) -> str:
    score = (f"\n6. Do not call get_recorded_verdict or reveal_truth before check_answer: they show the answer. "
             f'Afterwards, call reveal_truth with {{"recorded": {spec["recorded"]}}} to score yourself against the '
             "real cause and against Sherlog's own recorded agent.") if "recorded" in spec else ""
    return f"""Investigate an outage with the sherlog tools, then let Sherlog's code check your answer before you \
give it.

Pass "incident": {json.dumps(spec)} to every sherlog tool. An alarm fired: a metric stayed {sim.ALARM_Z:g} standard \
deviations above normal for {sim.ALARM_SUSTAIN_TICKS * sim.TICK_SECONDS}s. Find the service that caused it.

1. Start with top_anomalies: the strongest deviations right now.
2. Dig in with get_metric, get_logs and get_dependencies. {investigate.CAUSE_VS_VICTIMS}
3. Every result starts with a call id, such as [c3f9a01b2e4]. For each suspect, note the call ids whose results \
show that suspect abnormal.
4. Before you answer, call check_answer with up to {investigate.MAX_SUSPECTS} suspects, most likely first, each with \
its confidence (your probability, 0 to 1, that it is the cause) and the call ids it rests on. Sherlog's code re-runs \
every cited call and rejects a suspect its citations do not show abnormal. It answers only when the first suspect is \
verified and at least {CONFIDENT} likely, then checks that the answer explains every abnormal service (the blast \
radius) and replays the outage with the answer rolled back.
5. Report check_answer's decision as it is. If it says "{HAND_OVER}", say so and give its reasons; do not present a \
rejected or disproved suspect as the cause.{score}"""


PROMPT = ("Investigate an outage yourself", "Investigate a recorded outage or a sample with Sherlog's read-only "
          "tools, citing the call ids, then have check_answer verify the answer as Sherlog verifies its own: the "
          f"citations, the {CONFIDENT} rule, the blast radius and the rollback replay.")
PROMPT_ARGUMENTS = {"outage": f"A recorded outage's number, as list_recorded_outages lists them ({DEFAULT_OUTAGE}, "
                              "the walk-through, if left out), or shop or custom for a sample."}
CITE = "The text starts with its call id, such as [c3f9a01b2e4]: cite it in check_answer."
DESCRIPTIONS = {  # each investigation tool's title and description
    "top_anomalies": ("Find the strongest anomalies", "Step one of an investigation: the strongest deviations in the "
                      "incident right now, each service and metric with its z-score, normal level and current value, "
                      f"as Sherlog's own agent sees them first. {CITE}"),
    "get_metric": ("Read one metric", "One metric of one service: its normal baseline, current value, z-score over the "
                   "alarm window, how long it has been abnormal and its recent values, as Sherlog's own agent reads "
                   f"them. {CITE}"),
    "get_logs": ("Read one service's logs", "Recent WARN and ERROR log lines written by one service, newest last. A "
                 f"caller's line about a failed or slow call points at the service it called. {CITE}"),
    "get_dependencies": ("Read one service's dependencies", "The services one service calls and is called by, with "
                         f"the current health of each: how the cause is told from its victims. {CITE}"),
    "check_answer": ("Check an answer before giving it", "Up to 5 suspects, most likely first, each with its "
                     "confidence and the call ids it rests on. Sherlog's code re-runs every cited call (an id is a "
                     "hash of the call, so no state is kept), verifies a suspect only if a cited call shows it "
                     f"abnormal, and answers only when the first suspect is verified and at least {CONFIDENT} likely; "
                     "then it checks the answer's blast radius (does it explain every abnormal service) and, for a "
                     "recorded outage or a sample, replays the outage with the answer rolled back. Returns each "
                     "suspect verified or rejected with the reason, the decision (ship, or hand to a human, with the "
                     "reasons), the blast radius and the rollback. Uploaded telemetry gets no rollback replay."),
    "reveal_truth": ("Reveal a recorded outage's cause", "After check_answer, for a recorded outage only: the real "
                     "cause and fault (in hard mode also the decoy, a harmless anomaly that starts with the fault), "
                     "what Sherlog's own recorded agent answered and whether it was right, and the no-AI z-score "
                     "ranking's first three, so you can score your own investigation."),
}
