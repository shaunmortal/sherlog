"""The investigator: a Claude agent with read-only tools over the shop's telemetry, and a
deterministic verifier that accepts a suspect only if a tool call the agent cites really
showed that service abnormal.
"""
from __future__ import annotations

import logging
import os
from collections import Counter
from dataclasses import dataclass
from typing import Iterator

import anthropic

import sim

log = logging.getLogger("sherlog")

MODEL = os.environ.get("SHERLOG_MODEL", "claude-opus-5-5")
# who can investigate, from most careful to cheapest; the console lets visitors pick one
MODELS = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5"}
FALLBACK_MODELS = {"claude-opus-5-5"}  # server-side refusal fallbacks; Claude Haiku 5.5 has none
AUTO = "auto"  # Haiku first, Opus only when Haiku cannot give a verified, confident answer
AUTO_ROUTE = (MODELS["haiku"], MODELS["opus"])
EFFORT = os.environ.get("SHERLOG_EFFORT", "medium")
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOKENS = 16000
MAX_TOOL_ROUNDS = 8
MAX_SUSPECTS = 5
EVIDENCE_Z = 3.0          # a metric this far above normal counts as evidence
EVIDENCE_LOG_LINES = 2    # this many WARN/ERROR lines implicating a service count as evidence
ANSWER_CONFIDENCE = 0.8   # below this, or if the top suspect fails verification, escalate to a human
TOP_ANOMALIES_SHOWN = 8
LOG_LINES_SHOWN = 12
SERIES_POINTS_SHOWN = 12
FAULT_TYPES = (*sim.FAULTS, "unknown")

SYSTEM = f"""You are the on-call engineer for an online shop built from {len(sim.SERVICES)} microservices. \
An alarm just fired; find the service that caused it.

A broken service makes everything that calls it look unhealthy too, so tell the cause apart from its victims: \
the cause is an unhealthy service whose problem is not explained by anything it calls. A caller's log line about \
a failed or slow RPC points at the callee, not at the caller. Some warnings are routine noise.

Your tools are read-only, and every result starts with a call id such as [c3]. Investigate as much as you need \
(at most {MAX_TOOL_ROUNDS} rounds of tool calls), then call submit_ranking with up to {MAX_SUSPECTS} suspects, \
most likely first. For every suspect, cite the call ids whose results show that suspect abnormal: an automatic \
verifier rejects any suspect whose cited calls do not. Confidence is your probability (0 to 1) that the suspect \
is the cause.

In submit_ranking also write ticket_note, for engineers: 2-3 sentences naming the cause, the key evidence and a \
next step. And customer_update: 2 plain-language sentences for shoppers about what is affected, with no internal \
service names and no speculation."""

_SERVICE = {"type": "string", "enum": list(sim.SERVICES)}


def _tool(name: str, description: str, properties: dict) -> dict:
    schema = {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}
    return {"name": name, "description": description, "strict": True, "input_schema": schema}


_SUSPECT = {
    "type": "object",
    "properties": {
        "service": _SERVICE,
        "fault_type": {"type": "string", "enum": list(FAULT_TYPES)},
        "reason": {"type": "string", "description": "One sentence: why this service."},
        "confidence": {"type": "number", "description": "Probability 0 to 1 that this is the cause."},
        "evidence": {
            "type": "array", "items": {"type": "string"},
            "description": "Call ids, like c3, whose results show this service abnormal.",
        },
    },
    "required": ["service", "fault_type", "reason", "confidence", "evidence"],
    "additionalProperties": False,
}

TOOLS = [
    _tool("get_metric", "One metric of one service: normal baseline, current value, z-score, how long it has been "
          "abnormal, and recent values.", {"service": _SERVICE, "metric": {"type": "string", "enum": list(sim.METRICS)}}),
    _tool("get_logs", "Recent WARN and ERROR log lines written by one service, newest last.", {"service": _SERVICE}),
    _tool("get_dependencies", "The services this service calls and is called by, with the current health of each.",
          {"service": _SERVICE}),
    _tool("submit_ranking", "Submit the final answer: up to 5 suspects, most likely first, with the ticket note and "
          "customer update.", {
              "causes": {"type": "array", "items": _SUSPECT},
              "ticket_note": {"type": "string"},
              "customer_update": {"type": "string"},
          }),
]


def tools_without(*names: str) -> list[dict]:
    """The agent's tools minus some, for ablations."""
    return [t for t in TOOLS if t["name"] not in names]


@dataclass(frozen=True)
class Call:
    id: str
    name: str
    args: dict
    text: str
    shows: frozenset[str]  # services this call showed abnormal: what the verifier checks
    error: bool


# --- tools -------------------------------------------------------------------------------

def _at(inc: sim.Incident, tick: int) -> str:
    """A tick as time relative to the alarm, e.g. t-15s."""
    ref = inc.alarm if inc.alarm is not None else inc.now
    return f"t{(tick - ref) * sim.TICK_SECONDS:+d}s"


def _top_anomalies(inc: sim.Incident, args: dict) -> tuple[str, set[str]]:
    rows = sorted(((inc.window_z(s, m), s, m) for s in sim.SERVICES for m in sim.METRICS), reverse=True)
    rows = rows[:TOP_ANOMALIES_SHOWN]
    lines = [
        f"{s:<10} {m:<11} z={z:+6.1f}   normal {inc.stats[(s, m)][0]:.4g} -> now {inc.series[s][m][inc.now]:.4g}"
        for z, s, m in rows
    ]
    return "\n".join(lines), {s for z, s, _ in rows if z >= EVIDENCE_Z}


def _get_metric(inc: sim.Incident, args: dict) -> tuple[str, set[str]]:
    s, m = args["service"], args["metric"]
    mean, std = inc.stats[(s, m)]
    z = inc.window_z(s, m)
    if inc.z(s, m, inc.now) >= EVIDENCE_Z:
        since = inc.now
        while since > sim.BASELINE_TICKS and inc.z(s, m, since - 1) >= EVIDENCE_Z:
            since -= 1
        state = f"abnormal since {_at(inc, since)}"
    else:
        state = "not abnormal now"
    recent = inc.series[s][m][inc.now - SERIES_POINTS_SHOWN + 1: inc.now + 1]
    text = (
        f"normal {mean:.4g} ± {std:.2g}; now {inc.series[s][m][inc.now]:.4g}; z over the alarm window {z:+.1f}; {state}\n"
        f"last {len(recent)} values, {sim.TICK_SECONDS}s apart, oldest first: {' '.join(f'{v:g}' for v in recent)}"
    )
    return text, {s} if z >= EVIDENCE_Z else set()


def _get_logs(inc: sim.Incident, args: dict) -> tuple[str, set[str]]:
    s = args["service"]
    seen = [line for line in inc.logs if line.service == s and line.tick <= inc.now]
    loud = [line for line in seen if line.level != "INFO"]
    shown = loud[-LOG_LINES_SHOWN:]
    minutes = (inc.now + 1) * sim.TICK_SECONDS // 60
    head = f"{len(loud)} WARN/ERROR and {len(seen) - len(loud)} INFO lines in the last {minutes} min; newest {len(shown)}:"
    body = "\n".join(f"{_at(inc, line.tick)} {line.level} {line.text}" for line in shown) or "(no warnings or errors)"
    counts = Counter(line.about for line in shown)
    return f"{head}\n{body}", {svc for svc, n in counts.items() if n >= EVIDENCE_LOG_LINES}


def _get_dependencies(inc: sim.Incident, args: dict) -> tuple[str, set[str]]:
    s = args["service"]

    def health(x: str) -> str:
        metric, z = inc.worst(x)
        return f"{x} ({metric} z={z:+.1f})" if z >= EVIDENCE_Z else f"{x} (normal)"

    text = (
        f"{health(s)}\n"
        f"calls (downstream): {', '.join(map(health, sim.CALLS[s])) or 'nothing'}\n"
        f"called by (upstream): {', '.join(map(health, sim.CALLERS[s])) or 'nobody (entry point)'}"
    )
    neighbours = (s, *sim.CALLS[s], *sim.CALLERS[s])
    return text, {x for x in neighbours if inc.worst(x)[1] >= EVIDENCE_Z}


_HANDLERS = {
    "top_anomalies": _top_anomalies,
    "get_metric": _get_metric,
    "get_logs": _get_logs,
    "get_dependencies": _get_dependencies,
}


def _check_args(name: str, args) -> str | None:
    if name not in _HANDLERS:
        return f"unknown tool {name!r}"
    if not isinstance(args, dict):
        return "arguments must be an object"
    if name == "top_anomalies":
        return None
    if args.get("service") not in sim.SERVICES:
        return f"unknown service {args.get('service')!r}; known: {', '.join(sim.SERVICES)}"
    if name == "get_metric" and args.get("metric") not in sim.METRICS:
        return f"unknown metric {args.get('metric')!r}; known: {', '.join(sim.METRICS)}"
    return None


def run_tool(inc: sim.Incident, name: str, args, call_id: str) -> Call:
    """Run one read-only tool. Bad input comes back as an error result, never an exception."""
    problem = _check_args(name, args)
    if problem:
        return Call(call_id, name, args if isinstance(args, dict) else {}, f"[{call_id}] error: {problem}",
                    frozenset(), True)
    text, shows = _HANDLERS[name](inc, args)
    shown_args = ", ".join(str(v) for v in args.values())
    return Call(call_id, name, dict(args), f"[{call_id}] {name}({shown_args})\n{text}", frozenset(shows), False)


# --- verifier ----------------------------------------------------------------------------

def verify_cause(cause: dict, calls: dict[str, Call]) -> tuple[bool, str]:
    """Accept a suspect only if at least one call it cites was made and showed it abnormal."""
    service = cause.get("service")
    cited = [str(c).strip("[] ") for c in cause.get("evidence") or []]
    if not cited:
        return False, "no evidence cited"
    supporting = [c for c in cited if c in calls and service in calls[c].shows]
    if supporting:
        return True, f"{', '.join(supporting)} show {service} abnormal"
    missing = [c for c in cited if c not in calls]
    if len(missing) == len(cited):
        return False, f"cited call(s) {', '.join(missing)} were never made"
    return False, f"none of {', '.join(cited)} shows {service} abnormal"


def _confidence(value) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _escalation(top: dict | None) -> str | None:
    if top is None:
        return "no suspects submitted"
    if not top["verified"]:
        return f"top suspect failed verification ({top['verify_note']})"
    if top["confidence"] < ANSWER_CONFIDENCE:
        return f"confidence {top['confidence']:.0%} is below {ANSWER_CONFIDENCE:.0%}"
    return None


def build_verdict(raw: dict, calls: dict[str, Call]) -> dict:
    """Verify every submitted suspect; answer only with a verified, confident first suspect."""
    causes = []
    for cause in (raw.get("causes") or [])[:MAX_SUSPECTS]:
        if not isinstance(cause, dict):
            continue
        clean = {
            "service": str(cause.get("service")),
            "fault_type": str(cause.get("fault_type", "unknown")),
            "reason": str(cause.get("reason", "")),
            "confidence": _confidence(cause.get("confidence")),
            "evidence": [str(e).strip("[] ") for e in cause.get("evidence") or []],
        }
        ok, note = verify_cause(clean, calls)
        causes.append({**clean, "verified": ok, "verify_note": note})
    top = causes[0] if causes else None
    escalation = _escalation(top)
    return {
        "type": "verdict",
        "causes": causes,
        "answer": None if escalation else top["service"],
        "escalation": escalation,
        "ticket_note": str(raw.get("ticket_note", "")),
        "customer_update": str(raw.get("customer_update", "")),
        "calls": len(calls),
    }


# --- the agent loop ----------------------------------------------------------------------

def _ask(client, messages: list, tools: list[dict], model: str) -> object:
    fallback = {"betas": [FALLBACK_BETA], "fallbacks": "default"} if model in FALLBACK_MODELS else {}
    return client.beta.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=SYSTEM,
        tools=tools,
        messages=messages,
        thinking={"type": "adaptive", "display": "summarized"},
        output_config={"effort": EFFORT},
        **fallback,
    )


def _tool_event(call: Call) -> dict:
    return {"type": "tool", "id": call.id, "name": call.name, "args": call.args, "text": call.text,
            "error": call.error}


def _alert_text(inc: sim.Incident, first: Call) -> str:
    return (
        f"ALERT: a metric stayed {sim.ALARM_Z:g} standard deviations above normal for "
        f"{sim.ALARM_SUSTAIN_TICKS * sim.TICK_SECONDS}s. Times are relative to the alarm (t+0s); telemetry is "
        f"available up to {_at(inc, inc.now)}.\n"
        f"Services: {', '.join(sim.SERVICES)}. Metrics: {', '.join(sim.METRICS)}.\n\n"
        f"Strongest deviations right now:\n{first.text}"
    )


def _agent_loop(inc: sim.Incident, client, tools: list[dict], model: str) -> Iterator[dict]:
    offered = {t["name"] for t in tools}
    first = run_tool(inc, "top_anomalies", {}, "c0")
    calls = {first.id: first}
    yield _tool_event(first)
    messages = [{"role": "user", "content": _alert_text(inc, first)}]
    usage = {"input_tokens": 0, "output_tokens": 0, "model_calls": 0}

    for round_no in range(MAX_TOOL_ROUNDS + 2):
        response = _ask(client, messages, tools, model)
        usage = _add_usage(usage, response)
        if response.stop_reason == "refusal":
            yield {"type": "error", "message": "The model declined to continue this investigation."}
            break
        for block in response.content:
            if block.type == "thinking" and getattr(block, "thinking", ""):
                yield {"type": "thinking", "text": block.thinking}
        messages = [*messages, {"role": "assistant", "content": response.content}]

        uses = [b for b in response.content if b.type == "tool_use"]
        submission = next((u for u in uses if u.name == "submit_ranking"), None)
        if submission is not None:
            raw = submission.input if isinstance(submission.input, dict) else {}
            yield {**build_verdict(raw, calls), "usage": usage, "model": model}
            return
        if not uses:
            messages = [*messages, {"role": "user", "content": "Call submit_ranking now with your best ranking."}]
            continue

        results = []
        for use in uses:
            call_id = f"c{len(calls)}"
            call = (run_tool(inc, use.name, use.input, call_id) if use.name in offered
                    else Call(call_id, use.name, {}, f"[{call_id}] error: tool {use.name!r} is not available",
                              frozenset(), True))
            calls = {**calls, call.id: call}
            yield _tool_event(call)
            results.append({"type": "tool_result", "tool_use_id": use.id, "content": call.text, "is_error": call.error})
        if round_no >= MAX_TOOL_ROUNDS - 1:
            results.append({"type": "text", "text": "Tool budget used up: call submit_ranking now."})
        messages = [*messages, {"role": "user", "content": results}]

    yield {**build_verdict({}, calls), "usage": usage, "model": model}


def _add_usage(usage: dict, response) -> dict:
    counted = getattr(response, "usage", None)
    return {
        "input_tokens": usage["input_tokens"] + (getattr(counted, "input_tokens", 0) or 0),
        "output_tokens": usage["output_tokens"] + (getattr(counted, "output_tokens", 0) or 0),
        "model_calls": usage["model_calls"] + 1,
    }


def _api_error_message(exc: Exception) -> str:
    if isinstance(exc, anthropic.AuthenticationError):
        return "The Claude API key is missing or invalid."
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "The Claude API key is not allowed to use this model."
    if isinstance(exc, anthropic.RateLimitError):
        return "Claude API rate limit reached; try again in a minute."
    if isinstance(exc, anthropic.APIConnectionError):
        return "Could not reach the Claude API."
    if isinstance(exc, anthropic.APIStatusError):
        return f"Claude API error {exc.status_code}."
    if isinstance(exc, anthropic.AnthropicError):
        return "Claude client is not configured (is ANTHROPIC_API_KEY set?)."
    return "The investigation hit an unexpected error."


def investigate(inc: sim.Incident, client=None, tools: list[dict] = TOOLS, model: str = MODEL) -> Iterator[dict]:
    """Run the agent on an incident, yielding events; the last event is always the verdict.

    `tools` limits what the agent may use (the benchmark's ablations); submit_ranking must be included.
    """
    if inc.alarm is None:
        yield {"type": "error", "message": "No alarm fired, so there is nothing to investigate."}
        yield build_verdict({}, {})
        return
    try:
        yield from _agent_loop(inc, client or anthropic.Anthropic(), tools, model)
    except Exception as exc:  # last line of defence: the stream must always end with a verdict
        log.exception("investigation failed (root=%s fault=%s seed=%s)", inc.root, inc.fault, inc.seed)
        message = _api_error_message(exc)
        yield {"type": "error", "message": message}
        yield {**build_verdict({}, {}), "escalation": f"investigation failed: {message}"}


# --- one whole outage, as the console and the benchmark see it ------------------------------

def telemetry_event(inc: sim.Incident) -> dict:
    end = inc.now + 1
    return {
        "type": "telemetry",
        "alarm": inc.alarm,
        "now": inc.now,
        "tick_seconds": sim.TICK_SECONDS,
        "series": {s: {m: inc.series[s][m][:end] for m in sim.METRICS} for s in sim.SERVICES},
        "z": {s: {m: round(inc.window_z(s, m), 1) for m in sim.METRICS} for s in sim.SERVICES},
    }


def truth_event(inc: sim.Incident, verdict: dict | None) -> dict:
    causes = verdict["causes"] if verdict else []
    baseline = sim.zscore_ranking(inc)

    def hit(cause: dict) -> bool:
        return cause["service"] == inc.root and cause["verified"]

    return {
        "type": "truth",
        "root": inc.root,
        "decoy": inc.decoy,
        "fault": inc.fault,
        "seed": inc.seed,
        "answered": bool(verdict and verdict["answer"]),
        "ai_answer": verdict["answer"] if verdict else None,
        "ai_top1": bool(causes) and hit(causes[0]),
        "ai_top3": any(hit(c) for c in causes[:3]),
        "baseline": baseline[:3],
        "baseline_top1": baseline[0] == inc.root,
        "baseline_top3": inc.root in baseline[:3],
    }


def investigate_auto(inc: sim.Incident, client=None, tools: list[dict] = TOOLS) -> Iterator[dict]:
    """Cheapest model first. Only when its answer is not verified and confident does the strongest model
    take over, with a fresh investigation; its verdict is final."""
    cheap, strong = AUTO_ROUTE
    first_usage = None
    for event in investigate(inc, client, tools, cheap):
        if event["type"] != "verdict":
            yield event
            continue
        doubt = handoff_reason(inc, event)
        if doubt is None:
            yield {**event, "route": [cheap]}
            return
        yield {"type": "handoff", "from": cheap, "to": strong, "reason": doubt, "usage": event.get("usage")}
        first_usage = event.get("usage")
    for event in investigate(inc, client, tools, strong):
        yield {**event, "route": [cheap, strong], "first_usage": first_usage} if event["type"] == "verdict" else event


def handoff_reason(inc: sim.Incident, verdict: dict) -> str | None:
    """Why the cheap model's verdict is not good enough to stand, or None if it is."""
    if not verdict["answer"]:
        return verdict["escalation"]
    better = blast_radius(inc, verdict["answer"])["better"]
    if better:
        return f"{better} would explain more of the outage than {verdict['answer']}"
    return None


def upstream(service: str) -> set[str]:
    """The service and everything that calls it, directly or through others: where its trouble can show."""
    reach, todo = {service}, [service]
    while todo:
        for caller in sim.CALLERS[todo.pop()]:
            if caller not in reach:
                reach.add(caller)
                todo.append(caller)
    return reach


PROPAGATING = ("latency_ms", "error_rate")  # only slowness and failures travel up to callers


def _sustained(inc: sim.Incident, service: str, metrics: tuple[str, ...] = sim.METRICS) -> bool:
    """Abnormal for most of the window the agent saw, not just one spike: like the alarm, which needs 15 s."""
    start = inc.alarm if inc.alarm is not None else inc.now - sim.LOOKAHEAD_TICKS
    return any(sum(inc.z(service, m, t) >= EVIDENCE_Z for t in range(start, inc.now + 1)) >= sim.ALARM_SUSTAIN_TICKS
               for m in metrics)


def _explains(inc: sim.Incident, service: str, abnormal: list[str]) -> list[str]:
    """The abnormal services this one can account for: itself, and its callers only if its own latency or
    errors are abnormal (a busy CPU or a full heap that slows nobody down cannot make callers slow)."""
    reach = upstream(service) if _sustained(inc, service, PROPAGATING) else {service}
    return [s for s in abnormal if s in reach]


def blast_radius(inc: sim.Incident, service: str) -> dict:
    """Does the answer explain the whole outage? Every abnormal service should be it or one of its callers."""
    abnormal = [s for s in sim.zscore_ranking(inc) if _sustained(inc, s)]
    explained = _explains(inc, service, abnormal)
    best = max(sim.SERVICES, key=lambda s: len(_explains(inc, s, abnormal)))
    return {
        "service": service,
        "abnormal": abnormal,
        "explained": explained,
        "unexplained": [s for s in abnormal if s not in explained],
        # another service that accounts for strictly more of the outage: the answer may be a decoy
        "better": best if len(_explains(inc, best, abnormal)) > len(explained) else None,
    }


def incident_events(inc: sim.Incident, client=None, tools: list[dict] = TOOLS, model: str = MODEL) -> Iterator[dict]:
    """Everything one outage produces, in order: telemetry, the investigation, then the truth.
    `model` is a model id, or AUTO for the cheap-first route."""
    yield telemetry_event(inc)
    verdict = None
    events = investigate_auto(inc, client, tools) if model == AUTO else investigate(inc, client, tools, model)
    for event in events:
        if event["type"] == "verdict":
            verdict = event
        yield event
    yield truth_event(inc, verdict)
