"""Steady traffic to the testbed's gateway: --rps requests a second, sent on schedule by a pool of keep-alive
connections (open loop: a slow gateway does not slow the arrivals down until every connection is busy).

    python -m testbed.load --bed 0 --rps 20
"""
from __future__ import annotations

import argparse
import http.client
import queue
import threading
import time

from testbed.system import ENTRY, HOST, port

REQUEST_TIMEOUT_S = 5.0
MAX_BACKLOG = 1000  # requests waiting for a free connection; beyond this they are dropped, not queued forever


def _worker(bed: int, todo: queue.Queue) -> None:
    conn = None
    while True:
        todo.get()
        try:
            conn = conn or http.client.HTTPConnection(HOST, port(bed, ENTRY), timeout=REQUEST_TIMEOUT_S)
            conn.request("GET", "/rpc?caller=load")
            conn.getresponse().read()
        except (OSError, http.client.HTTPException):
            if conn is not None:
                conn.close()
            conn = None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bed", type=int, default=0)
    parser.add_argument("--rps", type=float, default=20.0)
    parser.add_argument("--workers", type=int, default=32)
    args = parser.parse_args()
    todo: queue.Queue = queue.Queue(maxsize=MAX_BACKLOG)
    for _ in range(args.workers):
        threading.Thread(target=_worker, args=(args.bed, todo), daemon=True).start()
    start, sent = time.monotonic(), 0
    while True:
        sent += 1
        time.sleep(max(0.0, start + sent / args.rps - time.monotonic()))
        try:
            todo.put_nowait(sent)
        except queue.Full:
            pass


if __name__ == "__main__":
    main()
