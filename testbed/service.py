"""One testbed service: a real HTTP server in its own OS process.

    python -m testbed.service store --bed 0 --dir /tmp/bed0

GET /rpc does the service's own work, then calls its callees over HTTP, one after another, each with a timeout.
A failed call fails the request (502), and the caller logs a WARN naming the callee and its RPC; so does a slow
one. GET /_stats returns cumulative counters for the collector. POST /_control injects or rolls back a fault
or a decoy. Log lines go to <dir>/<service>.log as "<unix time> <LEVEL> <text>". Stdlib only.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import json
import logging
import os
import random
import resource
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from testbed.system import CALLS, HOST, RPC, port

CALL_TIMEOUT_S = 2.0
SLOW_CALL_MS = 250
LATENCY_FAULT_S = 0.4         # latency: every request waits this much longer
ERROR_FAULT_SHARE = 0.3       # errors: this share of requests raise a real exception
CPU_FAULT_THREADS = 3         # cpu: busy-loop threads inside the service, fighting its request threads for the GIL
LEAK_MB_PER_TICK = 6          # memory: the leak grows this much every 5 s ...
LEAK_STEP_S = 0.5
LEAK_CAP_MB = 300             # ... up to here, so a long outage cannot swap the laptop
WARMUP_STEPS = 8              # the memory decoy loads its cache in this many 1 s steps, then holds it
PAGE = 16384                  # the leak and the cache are read a byte per page every second, as a cache that is
TOUCH_EVERY_S = 1.0           # still used would be, so the OS keeps them resident instead of compressing them
LOG_EVERY_S = 5               # at most one line per kind of complaint per 5 s, as log throttling does
STATS_LOG_EVERY_S = 60
MEMORY_WARN_MB = 64           # the memory watchdog warns once the process has grown this much and is still growing
IDLE_CONNECTION_S = 60
MB = 2**20
# a bad deploy: real exceptions from real bugs
BUGS = (lambda: {}["request_id"], lambda: [][0], lambda: int("n/a"))
# the CPU decoy: a batch job in a child process, so it burns a core of its own and slows no request down
BATCH_JOB = """import os, time
os.nice(5)
duty = {duty}
while True:
    t = time.perf_counter()
    while time.perf_counter() - t < 0.1 * duty:
        pass
    time.sleep(0.1 * (1 - duty))
"""
DECOY_LINES = {"cpu": "nightly batch job running: reindexing, worker pool busy",
               "memory": "cache warm-up: loading entries into memory"}

logging.addLevelName(logging.WARNING, "WARN")
log = logging.getLogger("service")


class Upstream(Exception):
    """A callee failed, so this request fails too."""


def _peak_rss_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / MB if sys.platform == "darwin" else peak / 1024  # bytes on macOS, KiB on Linux


class Service:
    def __init__(self, name: str, bed: int, workdir: Path):
        self.name, self.bed, self.workdir = name, bed, workdir
        self.lock = threading.Lock()
        self.requests = self.errors = 0
        self.latency_ms = 0.0
        self.fault: str | None = None
        self.decoy: str | None = None
        self.leak: list[bytes] = []
        self.cache: list[bytes] = []
        self.batch: subprocess.Popen | None = None
        self.logged: dict[tuple, int] = {}
        self.local = threading.local()
        if name == "store":
            self._init_store()

    # --- the work --------------------------------------------------------------------------

    def handle(self, query: str) -> dict:
        if self.fault == "latency":
            time.sleep(LATENCY_FAULT_S)
        if self.fault == "errors" and random.random() < ERROR_FAULT_SHARE:
            random.choice(BUGS)()
        out = self._own_work(query)
        for callee, share in CALLS[self.name].items():
            if random.random() < share:
                self.call(callee)
        return out

    def _own_work(self, query: str) -> dict:
        if self.name == "auth":
            token = os.urandom(32)
            digest = token
            for _ in range(50):
                digest = hmac.new(b"testbed-secret", digest, hashlib.sha256).digest()
            return {"ok": hmac.compare_digest(digest, digest)}
        if self.name == "catalog":
            products = sorted(({"sku": random.randrange(1000), "price": random.randrange(100, 9999)}
                               for _ in range(40)), key=lambda p: p["price"])
            return {"products": len(json.dumps(products))}
        if self.name == "store":
            return self._query("reserve" if "caller=inventory" in query else "charge")
        if self.name == "ledger":
            with self.lock, (self.workdir / "ledger.book").open("a") as book:
                book.write(f"{time.time():.3f} entry {random.randrange(10**9)}\n")
            return {"ok": True}
        return {"ok": True}

    def _init_store(self) -> None:
        db = self._db()
        db.execute("CREATE TABLE IF NOT EXISTS stock (sku INTEGER PRIMARY KEY, qty INTEGER NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS charges (id INTEGER PRIMARY KEY, sku INTEGER, amount INTEGER, at REAL)")
        db.executemany("INSERT OR IGNORE INTO stock VALUES (?, 1000)", [(sku,) for sku in range(1000)])
        db.commit()

    def _db(self) -> sqlite3.Connection:
        db = getattr(self.local, "db", None)
        if db is None:  # one connection per thread: sqlite connections are not shared across threads
            db = sqlite3.connect(self.workdir / "store.db", timeout=CALL_TIMEOUT_S)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            self.local.db = db
        return db

    def _query(self, op: str) -> dict:
        db, sku = self._db(), random.randrange(1000)
        if op == "reserve":
            db.execute("UPDATE stock SET qty = CASE WHEN qty > 0 THEN qty - 1 ELSE 1000 END WHERE sku = ?", (sku,))
        else:
            db.execute("INSERT INTO charges (sku, amount, at) VALUES (?, ?, ?)", (sku, random.randrange(100, 9999),
                                                                                time.time()))
        qty = db.execute("SELECT qty FROM stock WHERE sku = ?", (sku,)).fetchone()[0]
        db.commit()
        return {"sku": sku, "qty": qty}

    # --- calls to callees ------------------------------------------------------------------

    def call(self, callee: str) -> None:
        rpc = f"rpc {callee}.{RPC[callee]}"
        start = time.perf_counter()
        try:
            status = self._request(callee)
        except TimeoutError:
            self.throttled(("failed", callee), logging.WARNING, f"{rpc} failed: timed out after {CALL_TIMEOUT_S:g} s")
            raise Upstream(f"{callee} timed out") from None
        except (OSError, http.client.HTTPException) as exc:
            self.throttled(("failed", callee), logging.WARNING, f"{rpc} failed: {type(exc).__name__}")
            raise Upstream(f"{callee} unreachable") from None
        ms = (time.perf_counter() - start) * 1000
        if status >= 500:
            self.throttled(("failed", callee), logging.WARNING, f"{rpc} failed: HTTP {status}")
            raise Upstream(f"{callee} answered {status}")
        if ms > SLOW_CALL_MS:
            self.throttled(("slow", callee), logging.WARNING, f"{rpc} slow: {ms:.0f} ms (threshold {SLOW_CALL_MS} ms)")

    def _request(self, callee: str) -> int:
        """One GET over this thread's keep-alive connection to the callee; a stale one is replaced once."""
        conns = self.local.__dict__.setdefault("conns", {})
        for attempt in range(2):
            reused = callee in conns
            conn = conns.get(callee) or http.client.HTTPConnection(HOST, port(self.bed, callee), timeout=CALL_TIMEOUT_S)
            conns[callee] = conn
            try:
                conn.request("GET", f"/rpc?caller={self.name}")
                response = conn.getresponse()
                response.read()
                return response.status
            except (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError):
                conns.pop(callee).close()
                if not reused or attempt:
                    raise
            except BaseException:
                conns.pop(callee).close()
                raise
        raise AssertionError("unreachable")

    # --- bookkeeping -----------------------------------------------------------------------

    def count(self, ms: float, failed: bool) -> None:
        with self.lock:
            self.requests += 1
            self.errors += failed
            self.latency_ms += ms

    def stats(self) -> dict:
        with self.lock:
            return {"requests": self.requests, "errors": self.errors, "latency_ms": round(self.latency_ms, 3),
                    "fault": self.fault, "decoy": self.decoy, "pid": os.getpid()}

    def throttled(self, key: tuple, level: int, text: str, every: float = LOG_EVERY_S) -> None:
        slot = int(time.time() // every)
        with self.lock:
            if self.logged.get(key) == slot:
                return
            self.logged[key] = slot
        log.log(level, text)

    def housekeeping(self) -> None:
        """Every 5 s: the memory watchdog; every minute: a stats line, and the decoy's own honest line."""
        start_peak = last_peak = _peak_rss_mb()
        last_info, seen = time.time(), (0, 0)
        while True:
            time.sleep(LOG_EVERY_S)
            peak = _peak_rss_mb()
            if peak - start_peak > MEMORY_WARN_MB and peak > last_peak:
                self.throttled(("memory",), logging.WARNING, f"memory up to {peak:.0f} MB and still growing, "
                               f"{peak - start_peak:.0f} MB above start", every=3 * LOG_EVERY_S)
            last_peak = peak
            if time.time() - last_info >= STATS_LOG_EVERY_S:
                with self.lock:
                    now = (self.requests, self.errors)
                log.info(f"{RPC[self.name]}: {now[0] - seen[0]} requests, {now[1] - seen[1]} failed in the last 60 s")
                if self.decoy:
                    log.info(DECOY_LINES[self.decoy])
                last_info, seen = time.time(), now

    # --- faults and decoys -----------------------------------------------------------------

    def control(self, body: dict) -> None:
        """{"fault": kind} or {"decoy": kind, "level": x} injects; {"rollback": true} removes this service's
        fault and decoy, as rolling it back would. A service with neither has nothing to roll back."""
        if body.get("rollback"):
            self.fault = None
            self._stop_decoy()
            self.leak.clear()
            log.info(f"rolled back to the previous version of {RPC[self.name]}")
        if body.get("fault"):
            self.fault = body["fault"]
            if self.fault == "cpu":
                for _ in range(CPU_FAULT_THREADS):
                    threading.Thread(target=self._spin, daemon=True).start()
            elif self.fault == "memory":
                threading.Thread(target=self._grow_leak, daemon=True).start()
        if body.get("decoy"):
            self._start_decoy(body["decoy"], float(body["level"]))

    def _spin(self) -> None:
        while self.fault == "cpu":
            sum(i * i for i in range(2000))

    def _grow_leak(self) -> None:
        step = int(LEAK_MB_PER_TICK * MB * LEAK_STEP_S / LOG_EVERY_S)
        while self.fault == "memory":
            if len(self.leak) * step < LEAK_CAP_MB * MB:
                self.leak.append(os.urandom(step))  # random bytes: the OS cannot compress them away
            time.sleep(LEAK_STEP_S)

    def _touch(self) -> None:
        while True:
            for chunk in [*self.leak, *self.cache]:
                chunk[::PAGE]
            time.sleep(TOUCH_EVERY_S)

    def _start_decoy(self, kind: str, level: float) -> None:
        self.decoy = kind
        log.info(DECOY_LINES[kind])
        if kind == "cpu":
            self.batch = subprocess.Popen([sys.executable, "-c", BATCH_JOB.format(duty=level)])
            return
        threading.Thread(target=self._warm_cache, args=(int(level * MB / WARMUP_STEPS),), daemon=True).start()

    def _warm_cache(self, step: int) -> None:
        for _ in range(WARMUP_STEPS):
            if self.decoy != "memory":
                return
            self.cache.append(os.urandom(step))
            time.sleep(1)

    def _stop_decoy(self) -> None:
        self.decoy = None
        self.cache.clear()
        if self.batch is not None:
            self.batch.terminate()
            self.batch.wait()
            self.batch = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive, so callers reuse their connections
    timeout = IDLE_CONNECTION_S
    service: Service

    def log_message(self, *args) -> None:  # no access log; the collector reads /_stats
        pass

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        path, _, query = self.path.partition("?")
        if path == "/_stats":
            return self._send(200, self.service.stats())
        if path != "/rpc":
            return self._send(404, {"error": "not found"})
        start, status = time.perf_counter(), 200
        try:
            body = self.service.handle(query)
        except Upstream as exc:
            status, body = 502, {"error": str(exc)}
        except Exception as exc:  # the bad deploy's bugs: logged like an unhandled exception, answered 500
            self.service.throttled(("bug", type(exc).__name__), logging.ERROR,
                                   f"unhandled {type(exc).__name__}: {exc} in {RPC[self.service.name]}")
            status, body = 500, {"error": "internal error"}
        self._send(status, body)
        self.service.count((time.perf_counter() - start) * 1000, status >= 500)

    def do_POST(self) -> None:
        if self.path != "/_control":
            return self._send(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        self.service.control(json.loads(self.rfile.read(length) or b"{}"))
        self._send(200, self.service.stats())


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128  # the load generator opens its connections all at once


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("name", choices=CALLS)
    parser.add_argument("--bed", type=int, default=0)
    parser.add_argument("--dir", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(filename=args.dir / f"{args.name}.log", level=logging.INFO,
                        format="%(created).3f %(levelname)s %(message)s")
    service = Service(args.name, args.bed, args.dir)
    Handler.service = service

    def stop(*_) -> None:
        service._stop_decoy()
        os._exit(0)

    signal.signal(signal.SIGTERM, stop)
    for task in (service.housekeeping, service._touch):
        threading.Thread(target=task, daemon=True).start()
    log.info(f"{RPC[args.name]} listening")
    Server((HOST, port(args.bed, args.name)), Handler).serve_forever()


if __name__ == "__main__":
    main()
