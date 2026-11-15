"""Sherlog web app: break a service in the simulated shop and watch the agent investigate.

    uv run --env-file .env uvicorn app:app --reload
"""
from __future__ import annotations

import json
import logging
import os
import random
import subprocess
import threading
import time
from collections import deque
from functools import lru_cache
from pathlib import Path
from typing import Iterator, Literal

import anthropic
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, ValidationError

import bench
import byo
import investigate
import sim

STATIC = Path(__file__).parent / "static"
LIVE_RUNS_PER_IP_PER_HOUR = 6
LIVE_RUNS_PER_DAY = 200
HOUR_SECONDS = 3600
HEALTHY_SEED = 7
# a model call that runs longer than this is stuck (recorded calls wrote at most about 1,600 output tokens): fail it,
# so the stream ends with an error event instead of waiting out the SDK's 10-minute default, three times over
LIVE_CALL_TIMEOUT_SECONDS = 120
LIVE_CALL_RETRIES = 1
STREAM_FAILED = "The investigation hit an unexpected error. Try again, or watch a recorded outage."
SHORT_SHA = 7
# a 422's detail names only the first few problems: one wrong field in every log line of an upload is thousands
# of errors, too many for the console's alert line to read out; `errors` still lists every one for a script
SHOWN_PROBLEMS = 3


def _git_version() -> str:
    """The checked-out commit, read once at start-up. Deploys carry no .git, so they report their sha from the env."""
    try:
        done = subprocess.run(["git", "rev-parse", f"--short={SHORT_SHA}", "HEAD"], cwd=STATIC.parent,
                              capture_output=True, text=True, timeout=2, check=True)
    except (OSError, subprocess.SubprocessError):
        return "dev"
    return done.stdout.strip() or "dev"


GIT_VERSION = _git_version()


def _version() -> str:
    """The deployed commit's short sha, else the checked-out one."""
    return os.environ.get("VERCEL_GIT_COMMIT_SHA", "")[:SHORT_SHA] or GIT_VERSION


log = logging.getLogger("sherlog")
app = FastAPI(title="Sherlog API", version=_version())
app.mount("/static", StaticFiles(directory=STATIC), name="static")

# ponytail: in-memory limits are per server instance; move to Redis if the app runs on many instances.
_lock = threading.Lock()
_runs_by_ip: dict[str, deque] = {}
_runs_today = {"day": "", "count": 0}


class BreakRequest(BaseModel):
    service: Literal[sim.SERVICES]
    fault: Literal[tuple(sim.FAULTS)]
    model: Literal[(*investigate.MODELS, investigate.AUTO)] = "opus"
    hard: bool = False


def _live_enabled() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def _client_ip(request: Request) -> str:
    # ponytail: Vercel sets x-real-ip to the visitor's address itself; a client-sent x-forwarded-for could
    # be forged to dodge the per-visitor limit. Behind another proxy, configure uvicorn --proxy-headers.
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")


def _take_live_slot(ip: str) -> str | None:
    """Reserve one live run for this IP, or say why not. Live runs cost API credits."""
    now = time.time()
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    with _lock:
        if _runs_today["day"] != today:
            _runs_today.update(day=today, count=0)
            _runs_by_ip.clear()  # keeps the table to at most a day's worth of visitors
        recent = _runs_by_ip.get(ip, deque())
        while recent and now - recent[0] > HOUR_SECONDS:
            recent.popleft()
        if len(recent) >= LIVE_RUNS_PER_IP_PER_HOUR:
            return "You have used your live runs for this hour. Try a recorded outage meanwhile."
        if _runs_today["count"] >= LIVE_RUNS_PER_DAY:
            return "Today's live runs are used up. Try a recorded outage meanwhile."
        recent.append(now)
        _runs_by_ip[ip] = recent  # only visitors who actually got a run are tracked
        _runs_today["count"] += 1
    return None


REPLAY_VARIANTS = ("agent", "hard-agent")  # the Opus agent's recorded runs, normal then hard mode
# then the cheaper models' hard-mode runs that answered wrong and that the rollback replay caught, so a visitor
# can watch the check fail an answer, not only pass one
CAUGHT_VARIANTS = ("hard-haiku", "hard-sonnet")


def _replays() -> list[dict]:
    paths = [bench.runs_path(v) for v in (*REPLAY_VARIANTS, *CAUGHT_VARIANTS)]
    return _load_replays(tuple(p.stat().st_mtime if p.exists() else 0.0 for p in paths))


def _caught(record: dict) -> bool:
    """The answer went out, named the wrong service, and rolling it back did not end the outage."""
    answer = record["truth"]["ai_answer"]
    return bool(answer) and answer != record["root"] and not bench.rollback_proves(
        record["root"], record["fault"], record["seed"], record.get("hard", False), answer)


@lru_cache(maxsize=1)
def _load_replays(mtimes: tuple) -> list[dict]:
    """Each recorded run with whether it is a caught wrong answer. The caught ones come after every Opus run, so
    adding them renumbers nothing."""
    opus = [{"record": r, "caught": False} for v in REPLAY_VARIANTS for r in bench.load(bench.runs_path(v))]
    caught = [{"record": r, "caught": True} for v in CAUGHT_VARIANTS for r in bench.load(bench.runs_path(v))
              if _caught(r)]
    return [*opus, *caught]


def _live_runs_today() -> int:
    today = time.strftime("%Y-%m-%d", time.gmtime())
    with _lock:
        return _runs_today["count"] if _runs_today["day"] == today else 0


def _sse(events: Iterator[dict]) -> Iterator[str]:
    try:
        for event in events:
            yield f"data: {json.dumps(event)}\n\n"
    except Exception:
        # investigate() already turns API errors into events; this catches the rest, so the console gets a
        # reason instead of a stream that just stops
        log.exception("live stream failed")
        yield f"data: {json.dumps({'type': 'error', 'message': STREAM_FAILED})}\n\n"


def _enrich(events: Iterator[dict], inc: sim.Incident) -> Iterator[dict]:
    """Add what the console shows but the agent never sees: the telemetry after the agent's cut-off (so the
    charts keep running while it investigates), and the blast-radius and rollback checks of its answer."""
    rest = {s: {m: inc.series[s][m][inc.now + 1:] for m in sim.METRICS} for s in sim.SERVICES}
    for event in events:
        if event["type"] == "telemetry":
            yield {**event, "rest": rest}
        elif event["type"] == "verdict" and event.get("answer"):
            # only a simulated outage can be replayed with the answer rolled back; uploaded telemetry has no seed
            rollback = {"rollback": investigate.rollback_check(inc, event["answer"])} if inc.root else {}
            yield {**event, "blast": investigate.blast_radius(inc, event["answer"]), **rollback}
        else:
            yield event


def _live_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(timeout=LIVE_CALL_TIMEOUT_SECONDS, max_retries=LIVE_CALL_RETRIES)


def _evidence() -> dict:
    """Benchmark tables for the Evidence view, precomputed in runs/evidence.json by bench.py."""
    paths = [bench.EVIDENCE, *(bench.runs_path(v) for v in bench.VARIANTS)]
    return _load_evidence(tuple(p.stat().st_mtime if p.exists() else 0.0 for p in paths))


@lru_cache(maxsize=1)
def _load_evidence(mtimes: tuple) -> dict:
    return bench.evidence_cached()


@lru_cache(maxsize=1)
def _healthy_series() -> dict:
    """A fault-free shop, looped by the console while nothing is broken."""
    return sim.simulate(None, None, seed=HEALTHY_SEED).series


@app.exception_handler(RequestValidationError)
def bad_input(request: Request, exc: RequestValidationError) -> JSONResponse:
    """422 with one sentence the console can show, plus each error's location for an uploader's script.
    The path skips the leading body/path/query, so a nested field reads series.cart.cpu_pct.3, not just 3."""
    errors = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
    problems = "; ".join(f"{'.'.join(map(str, e['loc'][1:])) or e['loc'][0]}: {e['msg']}"
                         for e in errors[:SHOWN_PROBLEMS])
    more = f"; and {len(errors) - SHOWN_PROBLEMS} more." if len(errors) > SHOWN_PROBLEMS else ""
    return JSONResponse({"detail": f"Bad input. {problems}{more}", "errors": errors}, status_code=422)


# every page and the health check also answer HEAD: uptime monitors send it, and a GET-only route answers 405.
# HEAD stays out of /docs, where it would only repeat the GET.
@app.get("/")
@app.head("/", include_in_schema=False)
def story() -> FileResponse:
    """The story page: what Sherlog does, told with a recorded outage and the benchmark."""
    return FileResponse(STATIC / "landing.html")


@app.get("/console")
@app.head("/console", include_in_schema=False)
def console() -> FileResponse:
    """The console: break a service, replay a recorded outage, read the evidence or upload your own."""
    return FileResponse(STATIC / "index.html")


@app.get("/status")
@app.head("/status", include_in_schema=False)
def status() -> FileResponse:
    """The status page: live mode, replays, today's live runs and the deployed version."""
    return FileResponse(STATIC / "status.html")


@app.get("/api/health")
@app.head("/api/health", include_in_schema=False)
def health() -> dict:
    """What the status page shows. Local and fast: no API calls, no secrets."""
    recorded = len(_replays())
    return {
        "ok": recorded > 0,
        "live": _live_enabled(),
        "models": [*investigate.MODELS, investigate.AUTO],
        "recorded_outages": recorded,
        "live_runs_today": _live_runs_today(),
        "live_runs_per_day": LIVE_RUNS_PER_DAY,
        "live_runs_per_visitor_per_hour": LIVE_RUNS_PER_IP_PER_HOUR,
        "server_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "version": _version(),
    }


@app.get("/api/meta")
def meta() -> dict:
    """The shop's services, call graph, faults and metrics, the models, the benchmark tables and a healthy loop."""
    return {
        "services": sim.SERVICES,
        "calls": sim.CALLS,
        "faults": sim.FAULTS,
        "metrics": sim.METRICS,
        "live": _live_enabled(),
        "model": investigate.MODEL,
        "models": investigate.MODELS,
        "benchmark": bench.summarize(bench.load(bench.runs_path("agent"))),
        "evidence": _evidence(),
        "baseline_ticks": sim.BASELINE_TICKS,
        "std_floor": sim.STD_FLOOR,
        "healthy": _healthy_series(),
    }


@app.post("/api/break")
def break_service(body: BreakRequest, request: Request) -> StreamingResponse:
    """Break a service in a freshly simulated shop and stream the investigation as server-sent events."""
    if not _live_enabled():
        raise HTTPException(503, "Live mode is off on this server. Try a recorded outage.")
    refusal = _take_live_slot(_client_ip(request))
    if refusal:
        raise HTTPException(429, refusal)
    incident = sim.simulate(body.service, body.fault, seed=random.randrange(1_000_000), hard=body.hard)
    events = investigate.incident_events(incident, client=_live_client(),
                                         model=investigate.MODELS.get(body.model, investigate.AUTO))
    return StreamingResponse(
        _sse(_enrich(events, incident)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/replays")
def replays() -> list[dict]:
    """The recorded outages that replay for free: the Opus agent's runs, then the wrong answers the rollback caught."""
    # numbered, not named: a name like "cache-latency-1040" would give away the culprit. The earliest runs predate
    # the model field; they ran on the default, Opus, as bench.summarize assumes too
    return [{"id": number, "hard": bool(r["record"].get("hard")), "caught": r["caught"],
             "model": r["record"].get("model", bench.OPUS)} for number, r in enumerate(_replays(), 1)]


@app.get("/api/replays/{number}")
def replay(number: int) -> list[dict]:
    """One recorded outage's events, as a live run streams them, with its answer's blast radius and rollback."""
    records = _replays()
    if not 1 <= number <= len(records):
        raise HTTPException(404, "No recorded outage with that number.")
    record = records[number - 1]["record"]
    # outages are deterministic from their seed, so the ticks after the cut-off can be re-simulated
    incident = sim.simulate(record["root"], record["fault"], record["seed"], record.get("hard", False))
    return list(_enrich(iter(record["events"]), incident))


# --- bring your own outage: real telemetry in the shop's format, see docs/bring-your-own.md ------------------

MAX_BODY_MB = 2  # a full incident with every allowed log line fits well inside this


class InvestigateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    incident: byo.IncidentDoc
    model: Literal[(*investigate.MODELS, investigate.AUTO)] = "opus"


async def _read_capped(request: Request, schema: type[BaseModel]) -> BaseModel:
    """Read the body, refusing it with 413 as soon as it passes the cap (never holding more than that), and
    validate it with the same 422 answer FastAPI gives everywhere else."""
    too_big = HTTPException(413, f"The request body is over {MAX_BODY_MB} MB.")
    cap = MAX_BODY_MB * 1_000_000
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > cap:
        raise too_big
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > cap:
            raise too_big
    try:
        return schema.model_validate_json(bytes(body))
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_context=False, include_input=False)
        raise RequestValidationError([{**e, "loc": ("body", *e["loc"])} for e in errors]) from None


@lru_cache(maxsize=1)
def _sample_incident() -> dict:
    return byo.sample()


@app.get("/api/sample-incident")
def sample_incident() -> dict:
    """One simulated outage in the bring-your-own format, culprit removed: download it, edit it, send it back."""
    return _sample_incident()


@app.post("/api/triage")
async def triage(request: Request) -> dict:
    """The no-AI checks on uploaded telemetry. No key and no live run needed, so no limit beyond the size cap."""
    return byo.triage(byo.to_incident(await _read_capped(request, byo.IncidentDoc)))


@app.post("/api/investigate")
async def investigate_upload(request: Request) -> StreamingResponse:
    """The agent on uploaded telemetry: the same events as /api/break, minus the truth nobody knows."""
    if not _live_enabled():
        raise HTTPException(503, "Live mode is off on this server. POST the incident to /api/triage for the no-AI checks.")
    body = await _read_capped(request, InvestigateRequest)
    incident = byo.to_incident(body.incident)
    if incident.alarm is None:  # refused before it costs the visitor a live run
        raise HTTPException(422, "No alarm fired in this telemetry, so there is nothing to investigate.")
    refusal = _take_live_slot(_client_ip(request))
    if refusal:
        raise HTTPException(429, refusal)
    events = investigate.incident_events(incident, client=_live_client(),
                                         model=investigate.MODELS.get(body.model, investigate.AUTO))
    untold = (e for e in events if e["type"] != "truth")  # an uploaded outage has no known culprit to reveal
    return StreamingResponse(
        _sse(_enrich(untold, incident)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
