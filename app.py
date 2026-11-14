"""Sherlog web app: break a service in the simulated shop and watch the agent investigate.

    uv run --env-file .env uvicorn app:app --reload
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
from collections import deque
from functools import lru_cache
from pathlib import Path
from typing import Iterator, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import bench
import investigate
import sim

STATIC = Path(__file__).parent / "static"
LIVE_RUNS_PER_IP_PER_HOUR = 6
LIVE_RUNS_PER_DAY = 200
HOUR_SECONDS = 3600
HEALTHY_SEED = 7

app = FastAPI(title="Sherlog")
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


def _replays() -> list[dict]:
    paths = [bench.runs_path(v) for v in REPLAY_VARIANTS]
    return _load_replays(tuple(p.stat().st_mtime if p.exists() else 0.0 for p in paths))


@lru_cache(maxsize=1)
def _load_replays(mtimes: tuple) -> list[dict]:
    return [r for v in REPLAY_VARIANTS for r in bench.load(bench.runs_path(v))]


def _sse(events: Iterator[dict]) -> Iterator[str]:
    for event in events:
        yield f"data: {json.dumps(event)}\n\n"


def _enrich(events: Iterator[dict], inc: sim.Incident) -> Iterator[dict]:
    """Add what the console shows but the agent never sees: the telemetry after the agent's cut-off (so the
    charts keep running while it investigates), and the blast-radius and rollback checks of its answer."""
    rest = {s: {m: inc.series[s][m][inc.now + 1:] for m in sim.METRICS} for s in sim.SERVICES}
    for event in events:
        if event["type"] == "telemetry":
            yield {**event, "rest": rest}
        elif event["type"] == "verdict" and event.get("answer"):
            yield {**event, "blast": investigate.blast_radius(inc, event["answer"]),
                   "rollback": investigate.rollback_check(inc, event["answer"])}
        else:
            yield event


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


@app.get("/")
def story() -> FileResponse:
    """The story page: what Sherlog does, told with a recorded outage and the benchmark."""
    return FileResponse(STATIC / "landing.html")


@app.get("/console")
def console() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/meta")
def meta() -> dict:
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
    if not _live_enabled():
        raise HTTPException(503, "Live mode is off on this server. Try a recorded outage.")
    refusal = _take_live_slot(_client_ip(request))
    if refusal:
        raise HTTPException(429, refusal)
    incident = sim.simulate(body.service, body.fault, seed=random.randrange(1_000_000), hard=body.hard)
    return StreamingResponse(
        _sse(_enrich(investigate.incident_events(incident, model=investigate.MODELS.get(body.model, investigate.AUTO)), incident)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/replays")
def replays() -> list[dict]:
    # numbered, not named: a name like "cache-latency-1040" would give away the culprit
    return [{"id": number, "hard": bool(r.get("hard"))} for number, r in enumerate(_replays(), 1)]


@app.get("/api/replays/{number}")
def replay(number: int) -> list[dict]:
    records = _replays()
    if not 1 <= number <= len(records):
        raise HTTPException(404, "No recorded outage with that number.")
    record = records[number - 1]
    # outages are deterministic from their seed, so the ticks after the cut-off can be re-simulated
    incident = sim.simulate(record["root"], record["fault"], record["seed"], record.get("hard", False))
    return list(_enrich(iter(record["events"]), incident))
