"""Sherlog web app: break a service in the simulated shop and watch the agent investigate.

    uv run --env-file .env uvicorn app:app --reload
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import random
import re
import subprocess
import threading
import time
from collections import deque
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterator, Literal

import anthropic
from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

import bench
import byo
import guide
import investigate
import mcp_investigation as inquiry
import mcp_server
import reference
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
SHOWN_KEY_CHARS = 80  # a 422 echoes at most this much of any one key in an error location
SHOWN_MSG_CHARS = 500  # and of any one message (long enough for the list of allowed names)


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
# Swagger UI is served by a route below, so the policy can hash its inline script; nothing links ReDoc, so it is off
app = FastAPI(title="Sherlog API", version=_version(), docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")

# --- headers on every response: a content security policy and the usual hardening ----------------------------------
# Scripts and stylesheets load only from this server, the font host and two folders on the CDN: one exact version of
# three.js (the story page's 3D shop) and of Swagger UI (/docs), each file pinned in the page with its integrity
# hash. Each page's inline <script>, import map and <style> is allowed by its hash, read from the page at start-up,
# so editing a page cannot leave its own code blocked. No 'unsafe-inline' anywhere: the pages set their dynamic
# styles through the DOM, never through style="" attributes.
THREE = "https://cdn.jsdelivr.net/npm/three@0.160.0/"
SWAGGER_UI = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.34.1/"
FONT_CSS, FONT_FILES = "https://fonts.googleapis.com", "https://fonts.gstatic.com"
PAGES = ("landing.html", "index.html", "status.html", "reference.html", "404.html", "guide.html", "swagger.html")
INLINE_CODE = re.compile(r'<(script|style)(?: type="importmap")?>(.*?)</\1>', re.S)


def _inline_hashes(pages: list[str]) -> dict[str, str]:
    """The sha256 source of every inline script and style block on these pages, by tag."""
    found: dict[str, set] = {"script": set(), "style": set()}
    for page in pages:
        for tag, code in INLINE_CODE.findall(page):
            found[tag].add(f"'sha256-{base64.b64encode(hashlib.sha256(code.encode()).digest()).decode()}'")
    return {tag: " ".join(sorted(hashes)) for tag, hashes in found.items()}


_HASHES = _inline_hashes([(STATIC / name).read_text(encoding="utf-8") for name in PAGES])
CSP = "; ".join([
    "default-src 'self'",
    f"script-src 'self' {THREE} {SWAGGER_UI} {_HASHES['script']}",
    f"style-src 'self' {FONT_CSS} {SWAGGER_UI} {_HASHES['style']}",
    f"font-src {FONT_FILES}",
    "img-src 'self' data:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])
SECURITY_HEADERS = [(name.encode(), value.encode()) for name, value in {
    "content-security-policy": CSP,
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
    "x-frame-options": "DENY",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
    "cross-origin-opener-policy": "same-origin",
}.items()]


# Pages, docs and static files change only with a deploy, which starts the CDN's cache afresh, so the CDN may keep
# them 5 minutes and serve a stale copy while it refetches. Browsers still revalidate (max-age=0). Never the API or
# MCP, whose answers change with live runs.
PAGE_CACHE = (b"cache-control", b"public, max-age=0, s-maxage=300, stale-while-revalidate=86400")


def cacheable(scope: dict, status: int, headers: list) -> bool:
    """A successful GET or HEAD of a page or static file that did not set its own Cache-Control."""
    path = scope["path"]
    return (scope["method"] in ("GET", "HEAD") and status == 200
            and not (path in ("/api", "/mcp") or path.startswith(("/api/", "/mcp/")))
            and all(name.lower() != b"cache-control" for name, _ in headers))


class SecurityHeaders:
    """Adds SECURITY_HEADERS to every HTTP response, streamed ones too (plain ASGI, so nothing is buffered), and
    PAGE_CACHE to the cacheable ones."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.inner(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = message.get("headers", [])
                cache = [PAGE_CACHE] if cacheable(scope, message["status"], headers) else []
                message = {**message, "headers": [*headers, *SECURITY_HEADERS, *cache]}
            await send(message)

        await self.inner(scope, receive, send_with_headers)


app.add_middleware(SecurityHeaders)

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
    rest = {s: {m: inc.series[s][m][inc.now + 1:] for m in inc.measured[s]} for s in inc.services}
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


REAL_RESULTS = STATIC.parent / "runs" / "real-benchmark" / "results.json"
REAL_METHODS = ("auto", "graph", "mmnsigma", "nsigma", "baro")  # the README's real-benchmark table, in its order
REAL_PAIRED = ("graph", "mmnsigma", "nsigma")  # the auto route against each, case by case
REAL_AGENT = ("cases", "answered", "handed_to_human", "wrong_when_answered", "handed_to_opus", "cost_usd_total",
              "cost_usd_median")


@lru_cache(maxsize=1)
def _real_benchmark() -> dict:
    """The test set of the public benchmark of real outages, from runs/real-benchmark/results.json, which
    scripts/benchmark/report.py recomputes from the run records with no API calls (docs/real-benchmark.md)."""
    res = json.loads(REAL_RESULTS.read_text())
    scores = ("AC@1", "AC@1_ci95", "AC@3", "Avg@5")
    return {
        "source": "runs/real-benchmark/results.json, recomputed by `python scripts/benchmark/report.py` with no API "
                  "calls; the method and every table: docs/real-benchmark.md. Data: RCAEval benchmark, MIT licence.",
        "cases": res["split"]["test"],
        "systems": "three real microservice systems (12, 15 and 68 services), each fault injected on purpose",
        "system_count": sum(k.startswith("system ") for k in res["methods"]["auto"]),
        "methods": {k: {"label": res["methods"][k]["label"], **{s: res["methods"][k]["all"][s] for s in scores}}
                    for k in REAL_METHODS},
        "auto_vs": {k: res["paired"]["auto"][k] for k in REAL_PAIRED},
        "most_abnormal_first": res["methods"]["zscore"]["all"]["AC@1"],  # the z-score ranking's first pick
        "agent": {k: res["methods"]["auto"]["sherlog"][k] for k in REAL_AGENT},
    }


@lru_cache(maxsize=1)
def _healthy_series() -> dict:
    """A fault-free shop, looped by the console while nothing is broken."""
    return sim.simulate(None, None, seed=HEALTHY_SEED).series


def _clip(part, limit: int):
    """A key or message as a 422 shows it: a huge key in an upload must not come back in full."""
    if not isinstance(part, str) or len(part) <= limit:
        return part
    return part[:limit - 1] + "…"


def _bad_input(errors: list[dict]) -> dict:
    """One sentence the console can show, plus each error's location for an uploader's script. The sentence's paths
    skip the leading body/path/query (or arguments, for an MCP tool), so a nested field reads series.cart.cpu_pct.3.
    Every echoed key and message is clipped, so a huge key in an upload does not come back in full."""
    errors = [{"loc": [_clip(part, SHOWN_KEY_CHARS) for part in e["loc"]], "msg": _clip(e["msg"], SHOWN_MSG_CHARS)}
              for e in errors]
    problems = "; ".join(f"{'.'.join(map(str, e['loc'][1:])) or e['loc'][0]}: {e['msg']}"
                         for e in errors[:SHOWN_PROBLEMS])
    more = f"; and {len(errors) - SHOWN_PROBLEMS} more." if len(errors) > SHOWN_PROBLEMS else ""
    return {"detail": f"Bad input. {problems}{more}", "errors": errors}


@app.exception_handler(RequestValidationError)
def bad_input(request: Request, exc: RequestValidationError) -> JSONResponse:
    """422 with the problems in one sentence and every error listed."""
    return JSONResponse(_bad_input([{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]), status_code=422)


def _wants_page(request: Request) -> bool:
    """A browser asking for a page: it accepts HTML and the path is not the API's or the MCP endpoint's."""
    path = request.url.path
    api = path in ("/api", "/mcp") or path.startswith("/api/")
    return not api and "text/html" in request.headers.get("accept", "")


@app.exception_handler(StarletteHTTPException)
async def not_found(request: Request, exc: StarletteHTTPException) -> Response:
    """A browser that follows a dead link gets the site's 404 page; scripts and API clients get JSON as before."""
    if exc.status_code == 404 and _wants_page(request):
        return FileResponse(STATIC / "404.html", status_code=404)
    return await http_exception_handler(request, exc)


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


@app.get("/llms.txt")
@app.head("/llms.txt", include_in_schema=False)
def llms_txt() -> FileResponse:
    """A map of Sherlog for AI tools, in plain text: the pages, the API, the MCP endpoint and its tools, the upload
    format and the docs."""
    return FileResponse(STATIC / "llms.txt", media_type="text/plain; charset=utf-8")


@app.get("/reference", include_in_schema=False)
@app.head("/reference", include_in_schema=False)
def api_reference() -> HTMLResponse:
    """The API reference the footers link to: every route in the OpenAPI schema, in the site's accessible style."""
    return HTMLResponse(reference.render(app.openapi(), (STATIC / "reference.html").read_text(encoding="utf-8")))


@app.get("/docs", include_in_schema=False)
def swagger_ui() -> FileResponse:
    """Swagger UI, for developers who want to try each route: our own page around it, so it passes WCAG 2.2 AA."""
    return FileResponse(STATIC / "swagger.html")


# the docs, rendered from the repository's markdown once, at start-up (guide.py)
GUIDE = guide.build((STATIC / "guide.html").read_text(encoding="utf-8"))


@app.get("/guide")
@app.head("/guide", include_in_schema=False)
def guide_index() -> HTMLResponse:
    """The docs: how Sherlog works, the benchmarks on real outages and a real running system, the MCP connector,
    the upload format, open-weights models and reproducibility."""
    return HTMLResponse(GUIDE[""])


@app.get("/guide/{page}")
@app.head("/guide/{page}", include_in_schema=False)
def guide_page(page: str) -> HTMLResponse:
    """One page of the docs, rendered from the repository's markdown, such as `/guide/mcp`."""
    if page not in GUIDE:  # "" is the index, and a path segment is never empty
        raise HTTPException(404, "No docs page with that name.")
    return HTMLResponse(GUIDE[page])


# what crawlers and security researchers look for at the site's root
SITE_FILES = {"/robots.txt": ("robots.txt", "text/plain; charset=utf-8"),
              "/sitemap.xml": ("sitemap.xml", "application/xml"),
              "/.well-known/security.txt": ("security.txt", "text/plain; charset=utf-8")}


def _site_file(name: str, media: str) -> Callable[[], FileResponse]:
    # a closure with no parameters: a lambda's default arguments would become query parameters a visitor could set
    def serve() -> FileResponse:
        return FileResponse(STATIC / name, media_type=media)
    return serve


for _path, (_name, _media) in SITE_FILES.items():
    app.add_api_route(_path, _site_file(_name, _media), methods=["GET", "HEAD"], include_in_schema=False)


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
    """The shop's services, call graph, faults and metrics, the models, the benchmark tables (the shop's and the real
    outages') and a healthy loop."""
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
        "real_benchmark": _real_benchmark(),
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


# --- bring your own outage: real telemetry, with its own call graph or the shop's, see docs/bring-your-own.md -

MAX_BODY_MB = 2  # a full incident with every allowed log line fits well inside this


class InvestigateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    incident: byo.IncidentDoc
    model: Literal[(*investigate.MODELS, investigate.AUTO)] = "opus"


async def _read_body(request: Request) -> bytes:
    """The body, refused with 413 as soon as it passes the cap (never holding more than that)."""
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
    return bytes(body)


async def _read_capped(request: Request, schema: type[BaseModel]) -> BaseModel:
    """Read the body under the cap and validate it with the same 422 answer FastAPI gives everywhere else."""
    body = await _read_body(request)
    try:
        return schema.model_validate_json(body)
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_context=False, include_input=False)
        raise RequestValidationError([{**e, "loc": ("body", *e["loc"])} for e in errors]) from None


@lru_cache(maxsize=2)
def _sample_incident(graph: str) -> dict:
    return byo.graph_sample() if graph == "custom" else byo.sample()


@app.get("/api/sample-incident")
def sample_incident(graph: Literal["shop", "custom"] = "shop") -> dict:
    """One simulated outage in the bring-your-own format, culprit removed: download it, edit it, send it back.
    `graph=custom` gives six of its services renamed, with their own call graph in `calls`."""
    return _sample_incident(graph)


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


# --- the MCP connector: the free, read-only parts of Sherlog as tools for Claude and other MCP clients -------------
# The protocol is in mcp_server.py; docs/mcp.md says how to connect. No tool calls Claude, so none needs a key or a
# live run, and none is limited beyond the body-size cap the upload routes have.

MCP_INSTRUCTIONS = (
    "Sherlog is an AI on-call investigator that has to prove its answer, benchmarked on 252 real outages of a "
    "public benchmark and on a simulated 11-service shop. These tools are free and read-only: none calls Claude or "
    "needs a key. To investigate an outage yourself, pass the same incident to every tool ({\"recorded\": 41} for a "
    "recorded outage, {\"sample\": \"shop\"} or {\"sample\": \"custom\"}, or {\"document\": ...} for your own "
    "telemetry): start with top_anomalies, dig in with get_metric, get_logs and get_dependencies, and cite the call "
    "id each result starts with. Before you answer, call check_answer: Sherlog's code re-runs every cited call, "
    "rejects a suspect its citations do not show abnormal, hands an answer under 80% confidence to a human, and "
    "checks the answer's blast radius and a replay with it rolled back. Report its decision as it is. "
    "get_recorded_verdict and reveal_truth show a recorded outage's answer, so call them only after check_answer; "
    "the investigate_outage prompt has the steps. benchmark_summary gives the headline numbers and their caveats; "
    "list_recorded_outages lists the recorded investigations; sample_incident and triage_incident run the no-AI "
    "checks on telemetry in the sherlog-incident/1 format.")
HEADLINE_METHODS = ("one-shot", "haiku", "sonnet", "agent", "auto")  # the README's results table, in its order


class _NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _ReplayArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    number: int = Field(ge=1, description="The recorded outage's number, as list_recorded_outages lists it.")


class _SampleArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    graph: Literal["shop", "custom"] = Field(
        "shop", description="shop: the simulated shop's 11 services. custom: six of them under other names, with "
                            "their own call graph in calls.")


class _TriageArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    incident: byo.IncidentDoc = Field(
        description="A sherlog-incident/1 document, checked as POST /api/triage checks it: sample_incident returns "
                    "one, and docs/bring-your-own.md describes the format.")


def _tool(args: type[BaseModel], run: Callable[[BaseModel], dict], title: str, description: str) -> mcp_server.Tool:
    """A tool whose arguments are checked as an upload is, with the same messages, each path under arguments."""
    def call(arguments: dict) -> dict:
        try:  # through JSON, so a number or a string is read exactly as the upload routes read it
            parsed = args.model_validate_json(json.dumps(arguments))
        except ValidationError as exc:
            answer = _bad_input([{"loc": ["arguments", *e["loc"]], "msg": e["msg"]}
                                 for e in exc.errors(include_url=False, include_context=False, include_input=False)])
            raise mcp_server.ToolError(answer["detail"], answer) from None
        return run(parsed)

    return mcp_server.Tool(title, description, args.model_json_schema(), call)


def _replay_entry(number: int) -> dict:
    """A recorded outage's record and whether it is a caught wrong answer; a tool error if there is none."""
    entries = _replays()
    if number > len(entries):
        raise mcp_server.ToolError(f"No recorded outage with that number: they are numbered 1 to {len(entries)}.")
    return entries[number - 1]


def _recorded_verdict(args: _ReplayArgs) -> dict:
    _replay_entry(args.number)
    listed, events = replays(), replay(args.number)
    verdict = next((e for e in events if e["type"] == "verdict"), None)
    return {
        **listed[args.number - 1],
        "tool_calls": [{k: e[k] for k in ("id", "name", "args", "text")} for e in events if e["type"] == "tool"],
        "verdict": verdict and {k: v for k, v in verdict.items() if k != "type"},
        "truth": {k: v for k, v in events[-1].items() if k != "type"},
    }


def _method_scores(s: dict) -> dict:
    right = round((s["right_when_answered"] or 0) * s["answered"])  # as bench.gate_table counts them
    return {"label": s["label"], "cause_first": s["agent_ac1"], "answered_alone": s["answered"],
            "wrong_answers": s["answered"] - right, "median_cost_usd": s["median_cost_usd"],
            "rollback_caught_wrong": s["rollback_caught"], "rollback_rejected_right": s["rollback_wrongly_rejected"],
            "vs_call_graph_rule": {"only_method_right": s["only_agent_right_vs_graph"],
                                   "only_rule_right": s["only_graph_right"], "mcnemar_p": s["graph_mcnemar_p"]}}


def _benchmark(ev: dict, prefix: str, seed_base: int) -> dict:
    opus = ev[f"{prefix}agent"]  # the Opus agent's rows carry the no-AI controls, computed on the same outages
    return {
        "outages": opus["outages"],
        "seeds": f"{seed_base}-{seed_base + opus['outages'] - 1}",
        "no_ai": {"zscore_cause_first": opus["baseline_ac1"], "robust_zscore_cause_first": opus["robust_ac1"],
                  "call_graph_rule_cause_first": opus["graph_ac1"], "call_graph_rule_wrong": opus["graph_wrong"]},
        "methods": {v: _method_scores(ev[f"{prefix}{v}"]) for v in HEADLINE_METHODS},
    }


def _benchmark_summary(_: BaseModel) -> dict:
    ev = _evidence()
    normal, hard = _benchmark(ev, "", bench.SEED_BASE), _benchmark(ev, "hard-", bench.HARD_SEED_BASE)
    vs_rule = [b["methods"]["agent"]["vs_call_graph_rule"] for b in (normal, hard)]
    return {
        "real_outages": {**_real_benchmark(), "note": "AC@k: the root-cause service is among a method's first k, "
                         "scored by the benchmark's own evaluator; Avg@5: the mean of AC@1 to AC@5. The published "
                         "baselines were re-run on the same cases with the benchmark's own runner."},
        "source": "normal and hard: the controlled benchmark on the simulated shop, in runs/evidence.json, scored by "
                  "`python bench.py --report` with no API calls. cause_first: the true cause ranked first with its "
                  "citations verified.",
        "normal": normal,
        "hard": {**hard, "note": "adds a decoy: a service with a harmless anomaly that starts with the fault"},
        "caveats": [
            "Real outages: one benchmark suite of injected faults (one root cause each, at a known time) and one "
            "live run per case. The cases are public, so they may be in the models' training data; "
            "docs/real-benchmark.md checks for a learned prior and finds no sign of one, though that cannot rule one "
            "out.",
            "The controlled benchmark's telemetry is simulated: a shop this project wrote. The no-AI rules run on the "
            "same data, so the comparisons are fair, but the absolute numbers are optimistic.",
            "In the simulator, rolling back the true cause is exactly what clears the fault, so the rollback numbers "
            "measure the gate, not accuracy in the field. Uploaded telemetry gets no rollback test.",
            f"On the controlled benchmark, the Opus agent's edge over the no-AI call-graph rule is small: only the "
            f"agent was right in {vs_rule[0]['only_method_right']} normal and {vs_rule[1]['only_method_right']} hard outages, only the "
            f"rule in {vs_rule[0]['only_rule_right']} and {vs_rule[1]['only_rule_right']} (exact McNemar "
            f"p = {vs_rule[0]['mcnemar_p']:.2g} and {vs_rule[1]['mcnemar_p']:.2g}).",
            f"The benchmarks are small: one hard-mode outage moves a score by {100 / hard['outages']:.1f} points.",
            "Auto was not run on its own: its rows apply the live route's handoff rule to the recorded Haiku and Opus "
            "runs on the same outages.",
        ],
    }


# --- Sherlog as an MCP investigator: the caller's own model investigates, Sherlog's code checks its answer -----------
# The tools and the gate are in mcp_investigation.py; here they meet the recorded outages and the samples.

@lru_cache(maxsize=2)
def _sample_case(graph: str) -> inquiry.Case:
    return inquiry.sample_case(graph, _sample_incident(graph))


def _case(spec: inquiry.IncidentSpec) -> inquiry.Case:
    if spec.recorded is not None:
        return inquiry.recorded_case(spec.recorded, _replay_entry(spec.recorded)["record"])
    if spec.sample is not None:
        return _sample_case(spec.sample)
    return inquiry.document_case(spec.document)


def _look(tool: str) -> Callable[[inquiry.Look], dict]:
    return lambda args: inquiry.look(_case(args.incident), tool, args.call_args())


def _reveal_truth(args: inquiry.RevealArgs) -> dict:
    entry = _replay_entry(args.recorded)
    record, caught = entry["record"], entry["caught"]
    verdict = next((e for e in record["events"] if e["type"] == "verdict"), {})
    return {
        "recorded": args.recorded, "root": record["root"], "fault": record["fault"],
        "hard": bool(record.get("hard")), "decoy": record["truth"].get("decoy"),
        "sherlog": {"model": record.get("model", bench.OPUS), "answer": verdict.get("answer"),
                    "escalation": verdict.get("escalation"),
                    "suspects": [{k: c[k] for k in ("service", "confidence", "verified")}
                                 for c in verdict.get("causes", [])],
                    "right": verdict.get("answer") == record["root"], "caught": caught},
        "zscore_top3": record["truth"]["baseline"],
    }


INVESTIGATION_TOOLS = {
    "top_anomalies": (inquiry.Look, _look("top_anomalies")),
    "get_metric": (inquiry.MetricLook, _look("get_metric")),
    "get_logs": (inquiry.ServiceLook, _look("get_logs")),
    "get_dependencies": (inquiry.ServiceLook, _look("get_dependencies")),
    "check_answer": (inquiry.CheckArgs, lambda args: inquiry.check(_case(args.incident), args.suspects)),
    "reveal_truth": (inquiry.RevealArgs, _reveal_truth),
}
INVESTIGATE_PROMPT = mcp_server.Prompt(
    *inquiry.PROMPT, inquiry.PROMPT_ARGUMENTS,
    lambda arguments: inquiry.prompt_text(inquiry.outage_spec(arguments.get("outage", inquiry.DEFAULT_OUTAGE),
                                                              len(_replays()))))

MCP = mcp_server.Server("sherlog", _version(), MCP_INSTRUCTIONS, {
    "list_recorded_outages": _tool(
        _NoArgs, lambda _: {"outages": replays()}, "List the recorded outages",
        "Every recorded investigation that replays for free: its number, whether it ran in hard mode (with a decoy), "
        "whether it is a confident wrong answer the rollback replay caught, and the Claude model that investigated. "
        "Numbered, not named, so the culprit stays hidden. The Opus agent's runs come first (#41 is the README's "
        "walk-through), the caught wrong answers last."),
    "get_recorded_verdict": _tool(
        _ReplayArgs, _recorded_verdict, "Get a recorded verdict and its checks",
        "One recorded investigation: the agent's tool calls (ids c0, c1, ... with the text each returned), its "
        "verdict (suspects with confidence, the cited call ids and whether plain code verified each citation, the "
        "answer or the hand-over to a human, a ticket note and a customer update), the blast-radius check (does the "
        "answer explain every abnormal service), the rollback replay (does the outage stop with the answer rolled "
        "back) and the truth: the real cause, and what the no-AI z-score ranking said. Recorded, so no AI runs."),
    "triage_incident": _tool(
        _TriageArgs, lambda args: byo.triage(byo.to_incident(args.incident)), "Triage telemetry without AI",
        "The no-AI checks on 10 minutes of telemetry, as POST /api/triage runs them: the tick the alarm fired at "
        "(null if none), the z-score and robust z-score rankings, and the blast-radius check of the z-score ranking's "
        "top suspect, whose `better` names a service that explains more of the outage. Without `calls` the services "
        "are the shop's 11; with `calls` they are your own 2 to 80, and `metrics` may name your own metrics. A full "
        "document is large (the shop sample is "
        "about 42 KB of JSON), so for a big export POST it to /api/triage instead."),
    "sample_incident": _tool(
        _SampleArgs, lambda args: _sample_incident(args.graph), "Get a sample incident",
        "One simulated outage as a sherlog-incident/1 document with the culprit removed, ready for triage_incident: "
        "graph=shop (11 services, about 42 KB of JSON) or graph=custom (6 renamed services with their own call graph, "
        "about 27 KB). On the shop sample the z-score ranking blames cart, a loud victim; the blast-radius check "
        "points at cache, the real cause."),
    "benchmark_summary": _tool(
        _NoArgs, _benchmark_summary, "Summarize the benchmark",
        "The headline numbers. First, 252 real outages of a public benchmark (three real microservice systems): how "
        "often Sherlog and the best published baselines, re-run on the same cases, ranked the root cause first, "
        "case-by-case tests and the cost. Then, from runs/evidence.json, 132 normal and 44 hard-mode simulated "
        "outages: how often each method named the true cause first, answered alone and answered wrong, its median "
        "cost, what the rollback replay caught, each method against the no-AI call-graph rule outage by outage, the "
        "no-AI baselines, and the caveats that bound these numbers."),
    **{name: _tool(args, run, *inquiry.DESCRIPTIONS[name]) for name, (args, run) in INVESTIGATION_TOOLS.items()},
}, {"investigate_outage": INVESTIGATE_PROMPT})


@app.post("/mcp")
async def mcp_endpoint(request: Request) -> Response:
    """The MCP endpoint: the Model Context Protocol over Streamable HTTP, JSON-RPC 2.0 with one JSON answer per POST
    and no session. Free, read-only tools and one prompt; see docs/mcp.md. GET and DELETE answer 405."""
    try:
        body = await _read_body(request)
    except HTTPException as exc:
        return JSONResponse(mcp_server.error(None, mcp_server.INVALID_REQUEST, exc.detail), status_code=exc.status_code)
    status, answer = mcp_server.handle(body, request.headers, MCP)
    return Response(status_code=status) if answer is None else JSONResponse(answer, status_code=status)
