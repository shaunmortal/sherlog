"""Run Sherlog on a real running system. See docs/real-system.md.

    uv run --with-requirements requirements.txt --with psutil --env-file .env python -m testbed.run --live
    uv run --with-requirements requirements.txt --with psutil python -m testbed.run          # no-AI checks only

For each planned outage: start the eight services and the load generator as OS processes, warm up, then record
10 minutes (120 ticks of 5 s) while the planned fault (and in hard mode a decoy) is injected at its onset. The
window becomes a sherlog-incident/1 document, checked as POST /api/triage checks it. The no-AI checks run on it
and, with --live, the agent on both routes (auto, and Opus) by the code path of POST /api/investigate, while the
system keeps running. Then each distinct top suspect is rolled back in turn, the culprit last, and 2 more minutes
of telemetry decide whether the alarm stops. Each outage's record is appended to runs/real/outages.jsonl the
moment it finishes, so a crash loses at most the outage in progress; a re-run skips finished outages. Live spend
is written to runs/real/spend.jsonl after every run and counts against --cap across re-runs.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import app
import bench
import byo
import investigate
import sim
from testbed import outage as plan_
from testbed.system import HOST, SERVICES, port

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "runs" / "real"
WARMUP_S = 30
STAGGER_S = 120               # bed i starts i * this later, so the beds' faults do not all begin together
START_TIMEOUT_S = 20
HTTP_TIMEOUT_S = 2.0
CONTROL_TRIES = 3
STOP_TIMEOUT_S = 5
LOAD_RPS = 20
CAP_USD = 6.0
ROUTES = ("auto", "opus")     # the auto route (Haiku first) and Opus on its own; no other model
# held back before a live run starts, above the most any recorded run cost (hard auto $0.09, hard Opus $0.12);
# a run whose cost is unknown (it failed midway) is charged this
HOLD_USD = {"auto": 0.15, "opus": 0.20}
AUTO_TYPICAL_USD = 0.03       # kept back for every outage whose auto run is still to come, so Opus cannot use it up
MB = 2**20
_write = threading.Lock()


class Clock:
    """Ticks of `seconds` from the start of the window; each collected reading closes one tick."""

    def __init__(self, seconds: float):
        self.seconds, self.wall, self.start, self.ticks = seconds, time.time(), time.monotonic(), 0

    def wait(self) -> None:
        self.ticks += 1
        time.sleep(max(0.0, self.start + self.ticks * self.seconds - time.monotonic()))


class Bed:
    """One copy of the system on its own ports: eight service processes and a load generator."""

    def __init__(self, index: int, work: Path):
        self.index, self.dir = index, work / f"bed{index}"
        self.procs: dict[str, subprocess.Popen] = {}
        self.pool = ThreadPoolExecutor(len(SERVICES))
        self.last: dict[str, dict] = {}            # service -> its last /_stats answer
        self.cpu: dict[str, dict[int, float]] = {}  # service -> {pid: CPU seconds} at the last reading
        self.at: float | None = None

    def start(self) -> None:
        self.stop()
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True)
        out = (self.dir / "processes.out").open("a")
        env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC")}  # services need no key
        spawn = lambda *args: subprocess.Popen([sys.executable, "-m", *args], cwd=ROOT, stdout=out,  # noqa: E731
                                               stderr=subprocess.STDOUT, start_new_session=True, env=env)
        self.procs = {s: spawn("testbed.service", s, "--bed", str(self.index), "--dir", str(self.dir))
                      for s in SERVICES}
        deadline = time.monotonic() + START_TIMEOUT_S
        while any(self._http(s, "GET", "/_stats") is None for s in SERVICES):
            if time.monotonic() > deadline or any(p.poll() is not None for p in self.procs.values()):
                raise RuntimeError(f"bed {self.index}: services did not start; see {self.dir / 'processes.out'}")
            time.sleep(0.2)
        self.procs["load"] = spawn("testbed.load", "--bed", str(self.index), "--rps", str(LOAD_RPS))
        self.last, self.cpu, self.at = {}, {}, None

    def stop(self) -> None:
        for p in self.procs.values():
            try:
                os.killpg(p.pid, signal.SIGTERM)  # the group: a decoy's batch job dies with its service
            except ProcessLookupError:
                pass
        for p in self.procs.values():
            try:
                p.wait(STOP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                p.wait()
        self.procs = {}

    def _http(self, service: str, method: str, path: str, body: dict | None = None) -> dict | None:
        conn = http.client.HTTPConnection(HOST, port(self.index, service), timeout=HTTP_TIMEOUT_S)
        try:
            data = json.dumps(body).encode() if body is not None else None
            conn.request(method, path, data, {"Content-Length": str(len(data or b""))})
            return json.loads(conn.getresponse().read())
        except (OSError, http.client.HTTPException, ValueError):
            return None
        finally:
            conn.close()

    def control(self, service: str, **body) -> dict:
        for _ in range(CONTROL_TRIES):
            answer = self._http(service, "POST", "/_control", body)
            if answer is not None:
                return answer
        raise RuntimeError(f"bed {self.index}: {service} did not take {body}")

    def _usage(self, service: str) -> tuple[float, float]:
        """CPU seconds the service's process tree used since the last reading, and its resident memory in MB:
        what a container's metrics count, a decoy's batch job included."""
        import psutil  # the testbed's only extra dependency; imported here so the tests need not install it

        root = psutil.Process(self.procs[service].pid)
        seen, now, rss = self.cpu.get(service, {}), {}, 0
        for p in [root, *root.children(recursive=True)]:
            try:
                times = p.cpu_times()
                rss += p.memory_info().rss
            except psutil.Error:  # a child that just exited
                continue
            now[p.pid] = times.user + times.system
        self.cpu[service] = now
        return sum(t - seen.get(pid, 0.0) for pid, t in now.items()), rss / MB

    def _reading(self, service: str, stats: dict | None, seconds: float | None) -> plan_.Reading:
        cpu_s, rss = self._usage(service)
        latency = errors = None
        before = self.last.get(service)
        if stats is not None:
            done = stats["requests"] - before["requests"] if before else 0
            if done > 0:
                latency = (stats["latency_ms"] - before["latency_ms"]) / done
                errors = (stats["errors"] - before["errors"]) / done
            self.last[service] = stats
        return {"latency_ms": latency, "error_rate": errors, "memory_mb": rss,
                "cpu_pct": None if seconds is None else 100 * cpu_s / seconds}

    def sample(self) -> dict[str, plan_.Reading]:
        """One reading per service since the last call: mean latency and error rate of the requests that
        finished (from /_stats), CPU % of one core and memory (from the OS)."""
        stats = dict(zip(SERVICES, self.pool.map(lambda s: self._http(s, "GET", "/_stats"), SERVICES)))
        now = time.monotonic()
        seconds, self.at = (None if self.at is None else now - self.at), now
        return {s: self._reading(s, stats[s], seconds) for s in SERVICES}

    def logs(self) -> list[tuple[float, str, str, str]]:
        lines = []
        for s in SERVICES:
            path = self.dir / f"{s}.log"
            for raw in (path.read_text(errors="replace").splitlines() if path.exists() else []):
                when, _, rest = raw.partition(" ")
                level, _, text = rest.partition(" ")
                try:
                    lines.append((float(when), s, level, text))
                except ValueError:  # a traceback's continuation line
                    continue
        return lines


def collect(bed: Bed, clock: Clock, ticks: int, actions: dict[int, list] | None = None) -> list[dict]:
    """`ticks` readings, running each action at the start of its tick."""
    out = []
    for k in range(ticks):
        for act in (actions or {}).get(k, []):
            act()
        clock.wait()
        out.append(bed.sample())
    return out


# --- live investigations, under the spend cap -------------------------------------------------

class Budget:
    def __init__(self, cap: float, spent: float, auto_left: int):
        self.cap, self.spent, self.held, self.auto_left = cap, spent, 0.0, auto_left
        self.lock = threading.Lock()

    def hold(self, route: str) -> bool:
        """Reserve a run's worst case, or refuse if it could break the cap. Opus also leaves room for every
        auto run still to come."""
        amount = HOLD_USD[route]
        with self.lock:
            keep = AUTO_TYPICAL_USD * self.auto_left if route == "opus" else 0.0
            if self.spent + self.held + amount + keep > self.cap:
                return False
            self.held += amount
            self.auto_left -= route == "auto"
            return True

    def settle(self, route: str, cost: float | None) -> float:
        charged = HOLD_USD[route] if cost is None else cost
        with self.lock:
            self.held -= HOLD_USD[route]
            self.spent += charged
        return charged


def run_cost(verdict: dict) -> float | None:
    """What the run cost at list prices, or None if a failed run left its usage unknown."""
    route = verdict.get("route") or [verdict.get("model")]
    usages = [verdict.get("first_usage"), verdict.get("usage")] if len(route) == 2 else [verdict.get("usage")]
    if None in route or any(u is None for u in usages):
        return None
    return round(sum(bench.cost_usd(u, model) for u, model in zip(usages, route)), 6)


def live_run(doc: dict, route: str) -> dict:
    """The agent on the document by the code path of POST /api/investigate: its request model, its client
    settings and model mapping, the truth event dropped and the blast radius added. Only the HTTP layer and
    the per-visitor limit are left out."""
    body = app.InvestigateRequest.model_validate_json(json.dumps({"incident": doc, "model": route}))
    incident = byo.to_incident(body.incident)
    started = time.monotonic()
    events = investigate.incident_events(incident, client=app._live_client(),
                                         model=investigate.MODELS.get(body.model, investigate.AUTO))
    events = list(app._enrich((e for e in events if e["type"] != "truth"), incident))
    verdict = next(e for e in reversed(events) if e["type"] == "verdict")
    return {"cost_usd": run_cost(verdict), "seconds": round(time.monotonic() - started, 1),
            "errors": [e["message"] for e in events if e["type"] == "error"],
            "events": [e for e in events if e["type"] != "telemetry"]}  # the document already holds the telemetry


def _ledger(out: Path, entry: dict) -> None:
    with _write, (out / "spend.jsonl").open("a") as f:
        f.write(json.dumps(entry) + "\n")


def spent_so_far(out: Path) -> float:
    return sum(e["charged_usd"] for e in bench.load(out / "spend.jsonl"))


def top_suspect(run: dict | None) -> str | None:
    """The run's first suspect, whether it answered or escalated; None if it named nobody."""
    verdict = next((e for e in reversed((run or {}).get("events", [])) if e["type"] == "verdict"), {})
    causes = verdict.get("causes") or []
    return causes[0]["service"] if causes else None


# --- one outage ---------------------------------------------------------------------------------

def _record_window(bed: Bed, o: plan_.Outage, clock: Clock) -> list[dict]:
    actions = {o.onset: [lambda: bed.control(o.service, fault=o.fault)]}
    if o.decoy:
        actions.setdefault(o.decoy_onset, []).append(
            lambda: bed.control(o.decoy, decoy=o.decoy_kind, level=o.decoy_level))
    return collect(bed, clock, sim.WINDOW_TICKS, actions)


def _investigate(bed: Bed, clock: Clock, doc: dict, o: plan_.Outage, budget: Budget, out: Path) -> tuple:
    """Both live routes at once while the system keeps running with its fault; their runs, the routes skipped
    and why, and the telemetry recorded meanwhile."""
    runs, skipped, during, allowed = {}, {}, [], []
    for route in ROUTES:
        if budget.hold(route):
            allowed.append(route)
        else:
            skipped[route] = "spend cap"
    with ThreadPoolExecutor(len(ROUTES)) as pool:
        futures = {r: pool.submit(live_run, doc, r) for r in allowed}
        while not all(f.done() for f in futures.values()):
            during += collect(bed, clock, 1)
    for route, future in futures.items():
        try:
            runs[route] = future.result()
            cost = runs[route]["cost_usd"]
        except Exception as exc:  # recorded, not fatal: the outage still gets its no-AI checks and rollback
            runs[route] = {"cost_usd": None, "errors": [f"{type(exc).__name__}: {exc}"], "events": []}
            cost = None
        charged = budget.settle(route, cost)
        _ledger(out, {"outage": o.id, "route": route, "cost_usd": cost, "charged_usd": charged, "at": time.time()})
    return runs, skipped, during


def _rollbacks(bed: Bed, clock: Clock, doc: dict, o: plan_.Outage, suspects: set[str]) -> dict:
    """Roll back each suspect and judge the next 2 minutes. The culprit goes last, so every other suspect is
    tested with the fault still on; a rolled-back decoy is restored before the next test."""
    tests = {}
    for service in sorted(suspects, key=lambda s: (s == o.service, s)):
        bed.control(service, rollback=True)
        series, filled = plan_.series_of(collect(bed, clock, plan_.ROLLBACK_TICKS))
        tests[service] = {**plan_.rollback_decision(doc, series), "filled_readings": filled, "after": series}
        if service == o.decoy:
            bed.control(o.decoy, decoy=o.decoy_kind, level=o.decoy_level)
            collect(bed, clock, plan_.SETTLE_TICKS)
    return tests


def run_outage(bed: Bed, o: plan_.Outage, budget: Budget, live: bool, tick_seconds: float, out: Path) -> dict:
    started = time.time()
    bed.start()
    try:
        time.sleep(WARMUP_S)
        clock = Clock(tick_seconds)
        bed.sample()  # the counters the first tick is measured from
        window = _record_window(bed, o, clock)
        doc, filled = plan_.to_document(window, bed.logs(), clock.wall, tick_seconds)
        inc = plan_.incident(doc)
        text = json.dumps(doc)
        (out / "docs").mkdir(parents=True, exist_ok=True)
        (out / "docs" / f"{o.id}.json").write_text(text)
        triage, graph = byo.triage(inc), investigate.graph_ranking(inc)
        runs, skipped, during = ({}, {r: "not live" for r in ROUTES}, [])
        if live and inc.alarm is None:
            skipped = {r: "no alarm: the server refuses to investigate" for r in ROUTES}
        elif live:
            runs, skipped, during = _investigate(bed, clock, doc, o, budget, out)
        tops = {"zscore": triage["zscore_ranking"][0]["service"], "robust": triage["robust_ranking"][0],
                "graph": graph[0], **{r: top_suspect(runs.get(r)) for r in ROUTES}}
        suspects = {s for method, s in tops.items() if s and method != "robust"}
        rollbacks = _rollbacks(bed, clock, doc, o, suspects)
    finally:
        bed.stop()
    return {
        "id": o.id, "hard": o.hard, "bed": bed.index, "truth": o.truth(), "tick_seconds": tick_seconds,
        "doc": f"docs/{o.id}.json", "doc_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "window_start": clock.wall, "alarm": inc.alarm, "now": inc.now, "filled_readings": filled,
        "alarm_before_onset": inc.alarm is not None and inc.alarm < o.onset,
        "triage": triage, "graph_ranking": graph, "tops": tops, "runs": runs, "skipped": skipped,
        "rollbacks": rollbacks, "during": plan_.series_of(during)[0] if during else None,
        "started_at": started, "finished_at": time.time(),
    }


def _line(r: dict) -> str:
    t = r["truth"]
    picks = " ".join(f"{m}={s}{'*' if s == t['service'] else ''}" for m, s in r["tops"].items())
    proofs = " ".join(f"{s}:{'proves' if x['proves'] else 'no'}" for s, x in r["rollbacks"].items())
    cost = sum(x.get("cost_usd") or 0 for x in r["runs"].values())
    return (f"{r['id']} bed{r['bed']} {t['service']}/{t['fault']}@{t['onset']} decoy={t['decoy']} "
            f"alarm={r['alarm']} | {picks} | rollback {proofs} | ${cost:.3f}")


def bed_loop(bed: Bed, todo: queue.Queue, budget: Budget, args) -> None:
    time.sleep(bed.index * args.stagger)
    while True:
        try:
            o = todo.get_nowait()
        except queue.Empty:
            return
        try:
            record = run_outage(bed, o, budget, args.live, args.tick_seconds, args.out)
        except Exception:  # left out of the records, so the next run retries it
            print(f"{o.id} bed{bed.index} FAILED\n{traceback.format_exc()}", flush=True)
            continue
        with _write, (args.out / "outages.jsonl").open("a") as f:
            f.write(json.dumps(record) + "\n")
        print(f"{_line(record)} | spent ${budget.spent:.3f}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--normal", type=int, default=20, help="normal outages")
    parser.add_argument("--hard", type=int, default=10, help="hard outages: with a decoy")
    parser.add_argument("--beds", type=int, default=3, help="copies of the system running at once (ports apart)")
    parser.add_argument("--live", action="store_true", help="run the agent too (needs ANTHROPIC_API_KEY)")
    parser.add_argument("--cap", type=float, default=CAP_USD, help="total live spend allowed, US$")
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--work", type=Path, help="where the services keep logs and data (default: a temp dir)")
    parser.add_argument("--stagger", type=float, default=STAGGER_S, help="seconds between the beds' starts")
    parser.add_argument("--tick-seconds", type=float, default=sim.TICK_SECONDS, help=argparse.SUPPRESS)  # dev only
    args = parser.parse_args()
    if args.tick_seconds != sim.TICK_SECONDS and args.out.resolve() == OUT.resolve():
        parser.error(f"a tick other than {sim.TICK_SECONDS} s is for trying the testbed out: give another --out")
    if args.live and not app._live_enabled():
        parser.error("--live needs ANTHROPIC_API_KEY")
    args.out.mkdir(parents=True, exist_ok=True)
    work = args.work or Path(tempfile.mkdtemp(prefix="sherlog-testbed-"))
    done = {r["id"] for r in bench.load(args.out / "outages.jsonl")}
    todo: queue.Queue = queue.Queue()
    for o in plan_.plan(args.normal, args.hard):
        if o.id not in done:
            todo.put(o)
    budget = Budget(args.cap, spent_so_far(args.out), todo.qsize())
    print(f"{todo.qsize()} outages to run on {args.beds} beds, {len(done)} done; spent ${budget.spent:.3f} "
          f"of ${args.cap:g}; work dir {work}", flush=True)
    beds = [Bed(i, work) for i in range(args.beds)]

    def shut_down(*_) -> None:
        for bed in beds:
            bed.stop()
        os._exit(1)

    signal.signal(signal.SIGTERM, shut_down)
    signal.signal(signal.SIGINT, shut_down)
    threads = [threading.Thread(target=bed_loop, args=(bed, todo, budget, args), daemon=True) for bed in beds]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for bed in beds:
        bed.stop()
    print(f"finished; spent ${budget.spent:.3f}", flush=True)


if __name__ == "__main__":
    main()
