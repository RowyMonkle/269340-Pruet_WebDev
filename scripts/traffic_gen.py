"""CP2 continuous traffic generator: >= 20 req/s of POST + PUT + GET against the Web API.

Keeps a fixed request rate (open loop, so a slow server does not lower the load),
prints live success / error / latency numbers once a second, and exits non-zero if a
CP2 criterion is broken:
  * any HTTP 5xx or connection failure          (zero downtime)
  * any unexpected 4xx                          (valid requests must be accepted)
  * a response with a missing field or a name that does not match first + last
  * p99 latency >= 500 ms
  * average rate below 20 req/s

Payloads alternate between the legacy shape (full_name) and the split shape
(first_name / last_name), so old and new clients are both exercised while the
migration phases change.

Usage:
  python scripts/traffic_gen.py                        # forever, Ctrl+C prints the report
  python scripts/traffic_gen.py --duration 120         # run 2 minutes, then report
  docker compose exec api python scripts/traffic_gen.py --base-url http://nginx/api/v1
"""
import argparse
import random
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

import httpx

MIN_RPS = 20.0
P99_LIMIT_MS = 500.0
REQUIRED_FIELDS = ("id", "email", "username", "role", "created_at")


class Stats:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.ok = {"POST": 0, "PUT": 0, "GET": 0}
        self.client_errors = 0   # unexpected 4xx
        self.server_errors = 0   # 5xx
        self.network_errors = 0  # timeout / connection refused / reset
        self.data_errors = 0     # 2xx body with a missing field or inconsistent name
        self.latencies: List[float] = []
        self.samples: List[str] = []  # first few failures, for the report

    def record(self, method: str, status: Optional[int], latency_ms: float, problem: Optional[str]) -> None:
        with self.lock:
            self.latencies.append(latency_ms)
            if status is None:
                self.network_errors += 1
            elif status >= 500:
                self.server_errors += 1
            elif status >= 400:
                self.client_errors += 1
            elif problem:
                self.data_errors += 1
            else:
                self.ok[method] += 1
            bad = status is None or status >= 400 or problem
            if bad and len(self.samples) < 8:
                self.samples.append(f"{method} -> {status if status is not None else 'no response'} {problem or ''}".strip())

    def snapshot(self):
        with self.lock:
            return (
                sum(self.ok.values()), self.client_errors, self.server_errors,
                self.network_errors, self.data_errors, list(self.latencies),
            )


def percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * pct / 100))]


def normalise(text: Optional[str]) -> str:
    return " ".join((text or "").split())


def check_user_body(body: Dict) -> Optional[str]:
    """Return a description of the problem, or None when the response is sound."""
    missing = [f for f in REQUIRED_FIELDS if body.get(f) in (None, "")]
    if missing:
        return f"missing field(s) {missing}"
    first, last, full = body.get("first_name"), body.get("last_name"), body.get("full_name")
    if full is None or first is None:
        return "name fields missing (full_name/first_name is null)"
    if normalise(full) != normalise(f"{first} {last or ''}"):
        return f"full_name {full!r} does not match first/last {first!r}/{last!r}"
    return None


class Generator:
    def __init__(self, base_url: str, stats: Stats, timeout: float, post_ratio: float, put_ratio: float) -> None:
        self.base = base_url.rstrip("/")
        self.stats = stats
        self.post_ratio = post_ratio
        self.put_ratio = put_ratio
        self.user_ids: List[int] = []
        self.ids_lock = threading.Lock()
        self.client = httpx.Client(
            timeout=timeout,
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        )

    def seed_ids(self) -> int:
        try:
            res = self.client.get(f"{self.base}/users", params={"page": 1, "page_size": 100})
            if res.status_code == 200:
                self.user_ids = [u["id"] for u in res.json()["items"]]
        except Exception as exc:  # noqa: BLE001 - startup probe only
            print(f"[traffic] could not list users at start: {exc}")
        return len(self.user_ids)

    def remember(self, user_id: int) -> None:
        with self.ids_lock:
            self.user_ids.append(user_id)

    def pick_id(self) -> Optional[int]:
        with self.ids_lock:
            return random.choice(self.user_ids) if self.user_ids else None

    def names(self) -> Dict[str, str]:
        tag = uuid.uuid4().hex[:8]
        shape = random.choice(("legacy", "split", "first_only"))
        if shape == "legacy":
            return {"full_name": f"Legacy{tag} Client"}
        if shape == "split":
            return {"first_name": f"Split{tag}", "last_name": "Client"}
        return {"first_name": f"Solo{tag}"}

    def do_post(self):
        tag = uuid.uuid4().hex[:16]
        payload = {
            "email": f"cp2_{tag}@example.com",
            "username": f"cp2_{tag}",
            "password": "Password123!",
            **self.names(),
        }
        res = self.client.post(f"{self.base}/users", json=payload)
        problem = None
        if res.status_code == 201:
            body = res.json()
            problem = check_user_body(body)
            self.remember(body["id"])
        return res.status_code, problem

    def do_put(self):
        user_id = self.pick_id()
        if user_id is None:
            return self.do_post()
        body = random.choice((
            {"full_name": f"Renamed{uuid.uuid4().hex[:6]} Person"},
            {"first_name": f"First{uuid.uuid4().hex[:6]}", "last_name": "Updated"},
            {"last_name": f"Last{uuid.uuid4().hex[:6]}"},
            {"email": f"cp2_{uuid.uuid4().hex[:16]}@example.com"},
        ))
        res = self.client.put(f"{self.base}/users/{user_id}", json=body)
        return res.status_code, (check_user_body(res.json()) if res.status_code == 200 else None)

    def do_get(self):
        if random.random() < 0.15:
            res = self.client.get(f"{self.base}/users", params={"page": random.randint(1, 3), "page_size": 20})
            if res.status_code != 200:
                return res.status_code, None
            for item in res.json()["items"]:
                problem = check_user_body(item)
                if problem:
                    return res.status_code, f"list item {item.get('id')}: {problem}"
            return res.status_code, None
        user_id = self.pick_id()
        if user_id is None:
            return self.do_post()
        res = self.client.get(f"{self.base}/users/{user_id}")
        return res.status_code, (check_user_body(res.json()) if res.status_code == 200 else None)

    def one_request(self) -> None:
        roll = random.random()
        method, action = (
            ("POST", self.do_post) if roll < self.post_ratio
            else ("PUT", self.do_put) if roll < self.post_ratio + self.put_ratio
            else ("GET", self.do_get)
        )
        start = time.perf_counter()
        status, problem = None, None
        try:
            status, problem = action()
        except Exception as exc:  # noqa: BLE001 - any transport failure is a failed request
            problem = type(exc).__name__
        self.stats.record(method, status, (time.perf_counter() - start) * 1000, problem)


def print_live(stats: Stats, started: float) -> None:
    ok, c4, s5, net, data, lats = stats.snapshot()
    total = ok + c4 + s5 + net + data
    elapsed = max(time.time() - started, 1e-9)
    recent = lats[-1000:]
    print(
        f"\r[{elapsed:6.0f}s] sent={total} ok={ok} 4xx={c4} 5xx={s5} net={net} data={data} "
        f"| {total / elapsed:5.1f} req/s | p50={percentile(recent, 50):5.0f}ms p99={percentile(recent, 99):5.0f}ms   ",
        end="", flush=True,
    )


def report(stats: Stats, elapsed: float, min_rps: float) -> int:
    ok, c4, s5, net, data, lats = stats.snapshot()
    total = ok + c4 + s5 + net + data
    rps = total / elapsed if elapsed else 0.0
    p99 = percentile(lats, 99)
    checks = [
        ("0 HTTP 5xx", s5 == 0, s5),
        ("0 connection failures", net == 0, net),
        ("0 unexpected 4xx", c4 == 0, c4),
        ("0 data errors", data == 0, data),
        (f"p99 < {P99_LIMIT_MS:.0f} ms", p99 < P99_LIMIT_MS, f"{p99:.0f} ms"),
        (f"rate >= {min_rps:.0f} req/s", rps >= min_rps, f"{rps:.1f}"),
    ]
    print("\n\n=== Traffic report ===")
    print(f"requests {total} in {elapsed:.0f}s | POST ok {stats.ok['POST']} PUT ok {stats.ok['PUT']} GET ok {stats.ok['GET']}")
    print(f"latency p50 {percentile(lats, 50):.0f} ms | p95 {percentile(lats, 95):.0f} ms | p99 {p99:.0f} ms | max {max(lats, default=0):.0f} ms")
    for label, passed, value in checks:
        print(f"  {'PASS' if passed else 'FAIL'}  {label:<24} {value}")
    for sample in stats.samples:
        print(f"  first failures: {sample}")
    failed = [c for c in checks if not c[1]]
    print("RESULT:", "PASS" if not failed else f"FAIL ({len(failed)} criteria)")
    return 0 if not failed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://localhost:8000/api/v1")
    parser.add_argument("--rps", type=float, default=25.0, help="target requests per second (CP2 needs >= 20)")
    parser.add_argument("--duration", type=float, default=0, help="seconds to run, 0 = until Ctrl+C")
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--post-ratio", type=float, default=0.30)
    parser.add_argument("--put-ratio", type=float, default=0.10)
    args = parser.parse_args()

    stats = Stats()
    gen = Generator(args.base_url, stats, args.timeout, args.post_ratio, args.put_ratio)
    print(f"[traffic] {args.rps:g} req/s to {args.base_url} "
          f"(POST {args.post_ratio:.0%}, PUT {args.put_ratio:.0%}, GET rest). Ctrl+C to stop.")
    print(f"[traffic] {gen.seed_ids()} existing users known for GET/PUT")

    started = time.time()
    interval = 1.0 / args.rps
    sent = 0
    next_report = started + 1
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            while True:
                now = time.time()
                if args.duration and now - started >= args.duration:
                    break
                # schedule by the clock, so a slow response never lowers the offered rate
                due = int((now - started) / interval) + 1
                while sent < due:
                    pool.submit(gen.one_request)
                    sent += 1
                if now >= next_report:
                    print_live(stats, started)
                    next_report += 1
                time.sleep(0.002)
    except KeyboardInterrupt:
        print("\n[traffic] stopping...")
    elapsed = time.time() - started
    gen.client.close()
    return report(stats, elapsed, MIN_RPS)


if __name__ == "__main__":
    sys.exit(main())
