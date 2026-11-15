"""One testbed outage seen the way a team running Prometheus would feed it to Sherlog. See docs/real-system.md.

    uv run --env-file .env --with-requirements requirements.txt --with psutil python -m testbed.prom --live

Every service also serves the Prometheus text format on GET /metrics. No Prometheus server runs here: each 5 s
tick this scrapes all eight /metrics endpoints, as a scrape job with a 5 s interval would, and keeps the raw
samples. They are turned into the `query_range` answers (step 5 s) that these queries return, computed in the
way `irate` computes them, from each sample and the one before it:

    latency_ms  1000 * irate(testbed_request_duration_seconds_sum[10s])
                       / irate(testbed_request_duration_seconds_count[10s])
    error_rate  irate(testbed_request_errors_total[10s]) / irate(testbed_requests_total[10s])
    cpu_pct     100 * irate(process_cpu_seconds_total[10s])
    memory_mb   process_resident_memory_bytes / 2^20

scripts/prom_to_sherlog.py, unchanged, turns those answers into the document. The usual collector records the
same window on the same running system, so the two documents can be compared.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import http.client
import json
import math
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from testbed.system import GRAPH, HOST, SERVICES, port

ROOT = Path(__file__).resolve().parent.parent
QUERIES = {  # what the answers below are computed as, for the record
    "latency_ms": "1000 * irate(testbed_request_duration_seconds_sum[10s]) / "
                  "irate(testbed_request_duration_seconds_count[10s])",
    "error_rate": "irate(testbed_request_errors_total[10s]) / irate(testbed_requests_total[10s])",
    "cpu_pct": "100 * irate(process_cpu_seconds_total[10s])",
    "memory_mb": "process_resident_memory_bytes / 2^20",
}
SCRAPE_TIMEOUT_S = 2.0
OUTAGE_SEED = "prom"
Scrape = tuple[float, dict[str, dict[str, float]]]  # (unix time, service -> metric name -> value)


def parse(text: str) -> dict[str, float]:
    """The text format's samples, by metric name; labels are not used, the scrape job adds `service`."""
    out = {}
    for line in text.splitlines():
        if line.strip() and not line.startswith("#"):
            name, _, value = line.rpartition(" ")
            out[name.partition("{")[0].strip()] = float(value)
    return out


def scrape(bed: int) -> Scrape:
    def one(service: str) -> dict[str, float]:
        conn = http.client.HTTPConnection(HOST, port(bed, service), timeout=SCRAPE_TIMEOUT_S)
        try:
            conn.request("GET", "/metrics")
            response = conn.getresponse()
            body = response.read().decode()
            if response.status != 200:
                raise RuntimeError(f"{service} /metrics answered {response.status}: {body[:200]}")
            return parse(body)
        finally:
            conn.close()

    return time.time(), {s: one(s) for s in SERVICES}


def _ratio(num: float, den: float) -> float:
    return num / den if den else math.nan  # 0/0 is NaN in PromQL too; the converter treats it as missing


def answers(scrapes: list[Scrape]) -> dict[str, dict]:
    """metric -> its query_range answer, one series per service, a sample at each scrape after the first."""
    values: dict[str, dict[str, list]] = {m: {s: [] for s in SERVICES} for m in QUERIES}
    for (t0, before), (t1, now) in zip(scrapes, scrapes[1:]):
        for s in SERVICES:
            d = {name: now[s][name] - before[s].get(name, 0.0) for name in now[s]}
            point = {
                "latency_ms": 1000 * _ratio(d["testbed_request_duration_seconds_sum"],
                                            d["testbed_request_duration_seconds_count"]),
                "error_rate": _ratio(d["testbed_request_errors_total"], d["testbed_requests_total"]),
                "cpu_pct": 100 * d["process_cpu_seconds_total"] / (t1 - t0),
                "memory_mb": now[s]["process_resident_memory_bytes"] / 2**20,
            }
            for m, v in point.items():
                values[m][s].append([round(t1, 3), "NaN" if math.isnan(v) else repr(v)])
    return {m: {"status": "success", "data": {"resultType": "matrix", "result": [
        {"metric": {"service": s}, "values": values[m][s]} for s in SERVICES]}} for m in QUERIES}


def convert(answers_: dict[str, dict], where: Path) -> dict:
    """The answers through scripts/prom_to_sherlog.py, run as a team would run it, with the testbed's graph."""
    where.mkdir(parents=True, exist_ok=True)
    for m, answer in answers_.items():
        (where / f"{m}.json").write_text(json.dumps(answer))
    (where / "calls.json").write_text(json.dumps(GRAPH))
    doc = where / "incident.json"
    subprocess.run([sys.executable, str(ROOT / "scripts" / "prom_to_sherlog.py"),
                    *(f"{m}={where / f'{m}.json'}" for m in QUERIES), "--calls", str(where / "calls.json"),
                    "-o", str(doc)], check=True, capture_output=True, text=True)
    return json.loads(doc.read_text())


def run_one(live: bool, out: Path, work: Path) -> dict:
    import byo
    import investigate
    import sim
    from testbed import outage as plan_
    from testbed import run

    o = dataclasses.replace(plan_._outage(0, False, OUTAGE_SEED), id="prom-n00")
    bed = run.Bed(0, work)
    bed.start()
    try:
        time.sleep(run.WARMUP_S)
        clock = run.Clock(sim.TICK_SECONDS)
        bed.sample()
        scrapes, window = [scrape(bed.index)], []
        for k in range(sim.WINDOW_TICKS):
            if k == o.onset:
                bed.control(o.service, fault=o.fault)
            clock.wait()
            window.append(bed.sample())
            scrapes.append(scrape(bed.index))
        logs = bed.logs()
    finally:
        bed.stop()
    collected, _ = plan_.to_document(window, logs, clock.wall)
    prom_doc = convert(answers(scrapes), out / o.id)
    record = {"id": o.id, "truth": o.truth(), "queries": QUERIES, "window_start": clock.wall,
              "doc": f"{o.id}/incident.json", "collector_doc": f"{o.id}/collector.json", "runs": {}}
    (out / o.id / "collector.json").write_text(json.dumps(collected))
    for name, doc in (("prom", prom_doc), ("collector", collected)):
        inc = plan_.incident(doc)
        triage, graph = byo.triage(inc), investigate.graph_ranking(inc)
        record[name] = {"alarm": inc.alarm, "zscore": triage["zscore_ranking"][0]["service"],
                        "robust": triage["robust_ranking"][0], "graph": graph[0]}
    record["doc_sha256"] = hashlib.sha256((out / record["doc"]).read_bytes()).hexdigest()
    if live and record["prom"]["alarm"] is not None:
        budget = run.Budget(run.CAP_USD, run.spent_so_far(run.OUT), 0)
        for route in run.ROUTES:
            if not budget.hold(route):
                record["runs"][route] = {"skipped": "spend cap"}
                continue
            result = run.live_run(prom_doc, route)
            charged = budget.settle(route, result["cost_usd"])
            run._ledger(run.OUT, {"outage": o.id, "route": route, "cost_usd": result["cost_usd"],
                                  "charged_usd": charged, "at": time.time()})
            record["runs"][route] = {**result, "top": run.top_suspect(result)}
    return record


def main() -> None:
    from testbed import run

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true", help="run the agent on the converted document too")
    parser.add_argument("--out", type=Path, default=run.OUT / "prom")
    args = parser.parse_args()
    if args.live and not run.app._live_enabled():
        parser.error("--live needs ANTHROPIC_API_KEY")
    args.out.mkdir(parents=True, exist_ok=True)
    record = run_one(args.live, args.out, Path(tempfile.mkdtemp(prefix="sherlog-prom-")))
    (args.out / "outage.json").write_text(json.dumps(record, indent=1))
    t = record["truth"]
    print(f"{record['id']} {t['service']}/{t['fault']}@{t['onset']} | prometheus doc {record['prom']} | "
          f"collector doc {record['collector']} | agent "
          f"{ {r: x.get('top') for r, x in record['runs'].items()} } | "
          f"${sum(x.get('cost_usd') or 0 for x in record['runs'].values()):.3f}")


if __name__ == "__main__":
    main()
