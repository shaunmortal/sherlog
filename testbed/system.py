"""The testbed's topology: eight services, who calls whom, their RPC names and their ports. Stdlib only, so the
service and load processes can import it without the app's dependencies."""
from __future__ import annotations

# caller -> {callee: share of the caller's requests that make this call}; calls are made one after another
CALLS: dict[str, dict[str, float]] = {
    "gateway": {"auth": 1.0, "catalog": 0.7, "orders": 0.3},
    "auth": {},
    "catalog": {},
    "orders": {"inventory": 1.0, "payments": 0.6},
    "inventory": {"store": 1.0},
    "payments": {"store": 1.0, "ledger": 1.0},
    "store": {},
    "ledger": {},
}
SERVICES = tuple(CALLS)
GRAPH = {s: list(callees) for s, callees in CALLS.items()}  # the upload format's `calls`
RPC = {"gateway": "Route", "auth": "Verify", "catalog": "ListProducts", "orders": "PlaceOrder",
       "inventory": "Reserve", "payments": "Charge", "store": "Query", "ledger": "Append"}
ENTRY = "gateway"

BASE_PORT = 19100
PORTS_PER_BED = 100  # bed b's services listen on BASE_PORT + b * PORTS_PER_BED + their index
HOST = "127.0.0.1"


def port(bed: int, service: str) -> int:
    return BASE_PORT + bed * PORTS_PER_BED + SERVICES.index(service)


def callers_of(service: str) -> set[str]:
    """Everything that calls the service, directly or through others: where its trouble can show."""
    reach, todo = set(), [service]
    while todo:
        target = todo.pop()
        for caller, callees in CALLS.items():
            if target in callees and caller not in reach:
                reach.add(caller)
                todo.append(caller)
    return reach
