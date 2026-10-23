#!/usr/bin/env python3
"""Resilience lab: break one thing at a time on a running stack and measure what the rest of the API does.

Needs only Python 3 and the docker CLI on the host (standard library, no packages). Run it against a
disposable stack, NOT against data you want to keep (it stops databases and inserts test rows):

    STACK_PREFIX=rehearsal API_IMAGE=rehearsal_api:local NGINX_PORT=18000 ... docker compose -p rehearsal up --build -d --wait
    docker compose -p rehearsal exec api python seed.py
    python scripts/resilience_lab.py --prefix rehearsal --url http://localhost:18000 \
        --network rehearsal_default --image rehearsal_api:local all

Scenarios (each prints one block of numbers):
  lock_pile         one row stays locked for 25 s and 120 clients try to pay that order; a probe watches an
                    UNRELATED endpoint the whole time (before: it stops answering; after: it keeps answering)
  pool_exhaustion   a replica with ONE database connection whose only connection is stuck: how long does the
                    next request wait, and what does it get (before: 12 s then 200; after: 503 in pool_timeout)
  sweep             6000 expired holds (12,000 tickets) are released by the background worker while a probe
                    watches the API
  mongo_outage      MongoDB stopped for 25 s: do PostgreSQL-only endpoints stay up, how fast do the others answer
  postgres_outage   the same for PostgreSQL (what happens once, when pg_stat_statements is switched on)

A "probe" is one client sending ~20 GET/s to one endpoint; a request that takes > 500 ms counts as a stall.
"""
import argparse
import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import Counter


# ----------------------------------------------------------------------------------------------- helpers
def sh(*cmd, timeout=300):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def call(base, method, path, body=None, timeout=40.0):
    """One HTTP request. Returns (status or exception name, seconds, body bytes)."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, time.perf_counter() - started, response.read()
    except urllib.error.HTTPError as error:
        return error.code, time.perf_counter() - started, error.read()
    except Exception as error:  # noqa: BLE001 - timeouts and refused connections are results here
        return type(error).__name__, time.perf_counter() - started, b""


class Probe(threading.Thread):
    """~20 sequential GET/s on one endpoint until stop() is called."""

    def __init__(self, base, path, label):
        super().__init__(daemon=True)
        self.base, self.path, self.label = base, path, label
        self.rows, self._stop_flag, self.t0 = [], threading.Event(), time.time()

    def run(self):
        while not self._stop_flag.is_set():
            began = time.perf_counter()
            status, seconds, _ = call(self.base, "GET", self.path, timeout=12)
            self.rows.append((time.time() - self.t0, seconds * 1000, status))
            time.sleep(max(0.0, 0.05 - (time.perf_counter() - began)))

    def stop(self):
        self._stop_flag.set()
        self.join(timeout=20)

    def report(self):
        rows = self.rows
        bad = [r for r in rows if r[2] != 200]
        slow = [r for r in rows if r[1] > 500]
        latencies = sorted(r[1] for r in rows)
        line = (f"  {self.label:<34} {len(rows):>5} requests | errors {len(bad):>3} | stalls >500 ms {len(slow):>3} | "
                f"p50 {latencies[len(latencies) // 2]:.0f} ms | p99 {latencies[int(len(latencies) * 0.99)]:.0f} ms | "
                f"max {latencies[-1]:.0f} ms")
        if bad:
            line += f"\n  {'':<34} errors from t={bad[0][0]:.0f}s to t={bad[-1][0]:.0f}s, codes {sorted({str(r[2]) for r in bad})}"
        return line


class Lab:
    def __init__(self, args):
        self.args = args
        self.base = args.url.rstrip("/")
        self.pg = f"{args.prefix}_postgres"
        self.mongo = f"{args.prefix}_mongo"

    def psql(self, sql, detach=False):
        cmd = ["docker", "exec"] + (["-d"] if detach else []) + [self.pg, "psql", "-U", "dev_user", "-d", "main_db", "-tAq", "-c", sql]
        return sh(*cmd)

    def scalar(self, sql):
        return self.psql(sql).stdout.strip()

    def make_event(self, capacity=1000, zone="Z"):
        body = {"title": "Lab Event", "category": "Concert", "artist": {"name": "A", "genre": "G"},
                "venue": {"name": "V", "city": "C", "capacity": 10}, "date_time": "2030-01-01T00:00:00Z",
                "zones": [{"name": zone, "price": 10, "capacity": capacity}]}
        status, _, payload = call(self.base, "POST", "/api/v1/events", body)
        assert status == 201, f"could not create an event: {status} {payload[:120]}"
        return json.loads(payload)["id"]

    def make_order(self, event_id, zone="Z"):
        status, _, payload = call(self.base, "POST", "/api/v1/orders", {"user_id": 1, "items": [{"event_id": event_id, "seat_zone": zone}]})
        assert status == 201, f"could not create an order (is the stack seeded?): {status} {payload[:120]}"
        return json.loads(payload)

    def wait_replicas(self):
        for name in (f"{self.args.prefix}_api_1", f"{self.args.prefix}_api_2"):
            for _ in range(60):
                state = sh("docker", "inspect", "-f", "{{.State.Health.Status}}", name).stdout.strip()
                if state == "healthy":
                    break
                time.sleep(2)
        time.sleep(5)

    # ------------------------------------------------------------------------------------------ scenarios
    def lock_pile(self):
        print("lock_pile: order row locked for 25 s by another session, 120 clients try to pay it; probe on GET /users/1")
        event_id = self.make_event()
        order = self.make_order(event_id)
        probe = Probe(self.base, "/api/v1/users/1", "unrelated endpoint GET /users/1")
        probe.start()
        time.sleep(5)
        self.psql(f"BEGIN; SELECT id FROM orders WHERE id={order['id']} FOR UPDATE; SELECT pg_sleep(25); COMMIT;", detach=True)
        time.sleep(1)
        results = []

        def pay():
            results.append(call(self.base, "POST", f"/api/v1/orders/{order['id']}/payments",
                                {"amount": order["total_amount"], "provider": "promptpay"}, timeout=40))

        threads = [threading.Thread(target=pay) for _ in range(120)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        time.sleep(3)
        probe.stop()
        print(probe.report())
        print(f"  120 payments ended with {dict(Counter(str(r[0]) for r in results))}, slowest {max(r[1] for r in results):.1f} s")

    def pool_exhaustion(self):
        print("pool_exhaustion: replica with 1 connection (POSTGRES_POOL_SIZE=1, POOL_TIMEOUT=2), that connection waits on a table lock")
        args = self.args
        sh("docker", "rm", "-f", "lab_pool")
        run = sh("docker", "run", "-d", "--name", "lab_pool", "--network", args.network,
                 "-e", "POSTGRES_HOST=postgres_db", "-e", "MONGO_HOST=mongo_db", "-e", "POSTGRES_POOL_SIZE=1",
                 "-e", "POSTGRES_MAX_OVERFLOW=0", "-e", "POSTGRES_POOL_TIMEOUT=2", "-e", "FLAG_CACHE_SECONDS=3600", args.image)
        if run.returncode:
            print("  could not start the throwaway replica:", run.stderr.strip()[:200])
            return
        try:
            for _ in range(40):
                if sh("docker", "exec", "lab_pool", "curl", "-fs", "http://localhost:8000/health").returncode == 0:
                    break
                time.sleep(1)
            fetch = ("import time,urllib.request,urllib.error\n"
                     "t=time.time()\n"
                     "try:\n    r=urllib.request.urlopen('http://localhost:8000%s',timeout=40);c=r.status;h=''\n"
                     "except urllib.error.HTTPError as e:\n    c=e.code;h=e.headers.get('Retry-After')\n"
                     "print(c,round(time.time()-t,1),h)\n")
            out = {}
            # warm up: load the feature-flag cache once (a cold replica spends an extra pool wait on it)
            sh("docker", "exec", "lab_pool", "python", "-c", fetch % "/api/v1/users/1", timeout=60)
            self.psql("BEGIN; LOCK TABLE orders IN ACCESS EXCLUSIVE MODE; SELECT pg_sleep(12); COMMIT;", detach=True)
            time.sleep(1)

            def get(key, path):
                result = sh("docker", "exec", "lab_pool", "python", "-c", fetch % path, timeout=60)
                out[key] = result.stdout.strip() or result.stderr.strip()[:80]

            first = threading.Thread(target=get, args=("holder", "/api/v1/orders/summary?user_id=1"))
            first.start()
            time.sleep(1)
            second = threading.Thread(target=get, args=("waiter", "/api/v1/users/1"))
            second.start()
            first.join()
            second.join()
            print(f"  request 1 (holds the only connection, waits on the table lock): [status, seconds, Retry-After] = {out.get('holder')}")
            print(f"  request 2 (no free connection):                                   [status, seconds, Retry-After] = {out.get('waiter')}")
        finally:
            sh("docker", "rm", "-f", "lab_pool")

    def sweep(self, holds=6000, per_order=2):
        print(f"sweep: {holds} expired holds x {per_order} tickets released by the background worker; probe on GET /users/1")
        event_id = self.make_event(capacity=10 ** 7, zone="Sweep")
        sh("docker", "exec", self.mongo, "mongosh", "--quiet", "main_db", "--eval",
           f"db.events.updateOne({{_id: ObjectId('{event_id}')}}, {{$set: {{'zones.0.booked_count': {holds * per_order}}}}})")
        prefix = f"LAB{int(time.time())}-"
        self.psql(f"INSERT INTO orders (order_number,user_id,total_amount,status,payment_method,expires_at) "
                  f"SELECT '{prefix}'||g,1,{10 * per_order},'pending','promptpay', now() - interval '20 minutes' FROM generate_series(1,{holds}) g")
        self.psql(f"INSERT INTO tickets (order_id,event_id,ticket_code,seat_zone,price,status) "
                  f"SELECT o.id,'{event_id}','{prefix}'||o.id||'-'||k,'Sweep',10,'held' FROM orders o, generate_series(1,{per_order}) k "
                  f"WHERE o.order_number LIKE '{prefix}%'")
        probe = Probe(self.base, "/api/v1/users/1", "GET /users/1 during the sweep")
        probe.start()
        began = time.time()
        released_after = None
        while time.time() - began < 60:
            if int(self.scalar(f"SELECT count(*) FROM orders WHERE order_number LIKE '{prefix}%' AND status='pending'") or 1) == 0:
                released_after = time.time() - began
                break
            time.sleep(1)
        time.sleep(5)
        probe.stop()
        counter = sh("docker", "exec", self.mongo, "mongosh", "--quiet", "main_db", "--eval",
                     f"print(db.events.findOne({{_id: ObjectId('{event_id}')}}).zones[0].booked_count)").stdout.strip()
        print(probe.report())
        print(f"  all {holds} holds expired after {released_after:.0f} s" if released_after else "  holds NOT all expired within 60 s")
        print(f"  MongoDB booked_count afterwards: {counter} (must be 0)")

    def _outage(self, container, label):
        print(f"{label}: {container} stopped at t=10 s, started again at t=35 s; probes on a PostgreSQL-only and a MongoDB-only endpoint")
        users = Probe(self.base, "/api/v1/users/1", "PostgreSQL-only  GET /users/1")
        events = Probe(self.base, "/api/v1/events?page_size=1", "MongoDB-only  GET /events")
        users.start()
        events.start()
        time.sleep(10)
        sh("docker", "stop", "-t", "2", container)
        time.sleep(25)
        sh("docker", "start", container)
        time.sleep(40)
        users.stop()
        events.stop()
        print(users.report())
        print(events.report())
        self.wait_replicas()
        again = [call(self.base, "GET", p)[0] for p in ("/api/v1/users/1", "/api/v1/events?page_size=1")]
        print(f"  afterwards, with no restart of the API: GET /users/1 -> {again[0]}, GET /events -> {again[1]}")

    def mongo_outage(self):
        self._outage(self.mongo, "mongo_outage")

    def postgres_outage(self):
        self._outage(self.pg, "postgres_outage")


SCENARIOS = ["lock_pile", "pool_exhaustion", "sweep", "mongo_outage", "postgres_outage"]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenarios", nargs="+", choices=SCENARIOS + ["all"])
    parser.add_argument("--prefix", default="ticweb", help="container name prefix = STACK_PREFIX of the stack (default ticweb)")
    parser.add_argument("--url", default="http://localhost:8000", help="the nginx gateway of that stack")
    parser.add_argument("--network", default="269340-pruet_ticweb_default", help="docker network of that stack (pool_exhaustion)")
    parser.add_argument("--image", default="ticweb_api:local", help="API image of that stack (pool_exhaustion)")
    args = parser.parse_args()
    lab = Lab(args)
    for name in (SCENARIOS if "all" in args.scenarios else args.scenarios):
        getattr(lab, name)()
        lab.wait_replicas()
        print()


if __name__ == "__main__":
    main()
