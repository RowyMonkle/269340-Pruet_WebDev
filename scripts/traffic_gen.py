import time
import random
import requests
from concurrent.futures import ThreadPoolExecutor

# Ingress runs via Nginx on port 8000 as stated in README
BASE_URL = "http://localhost:8000/api/v1"
TARGET_RPS = 25       # >= 20 req/s required by CP2 spec
INTERVAL = 1.0 / TARGET_RPS

stats = {
    "get_success": 0,
    "post_success": 0,
    "client_errors": 0,   # 4xx (e.g., 404 user not found, 409 conflict)
    "server_errors": 0,   # 5xx (Zero-downtime violation)
    "latencies": []
}

def send_request():
    start = time.time()
    is_write = random.random() < 0.35  # ~35% write traffic, 65% read traffic

    try:
        if is_write:
            # Users endpoint requires full_name and standard registration fields
            user_num = random.randint(10000, 999999)
            payload = {
                "full_name": f"Tester_{user_num} CMU",
                "email": f"test_{user_num}@cmu.ac.th",
                "password": "Password123!"
            }
            res = requests.post(f"{BASE_URL}/users", json=payload, timeout=2.0)
            latency = (time.time() - start) * 1000

            if res.status_code == 201:
                stats["post_success"] += 1
            elif 400 <= res.status_code < 500:
                stats["client_errors"] += 1
            else:
                stats["server_errors"] += 1

        else:
            # Query either specific user or event catalog
            if random.random() < 0.5:
                uid = random.randint(1, 300)
                res = requests.get(f"{BASE_URL}/users/{uid}", timeout=2.0)
            else:
                res = requests.get(f"{BASE_URL}/events", timeout=2.0)

            latency = (time.time() - start) * 1000

            if res.status_code == 200:
                stats["get_success"] += 1
            elif 400 <= res.status_code < 500:
                stats["client_errors"] += 1
            else:
                stats["server_errors"] += 1

        stats["latencies"].append(latency)
        if len(stats["latencies"]) > 1000:
            stats["latencies"].pop(0)

    except Exception:
        stats["server_errors"] += 1

def main():
    print("=== Starting CP2 Continuous Traffic Generator (Target: >= 20 req/s) ===")
    print("Sending traffic via Nginx on port 8000. Press Ctrl+C to stop.\n")

    with ThreadPoolExecutor(max_workers=12) as executor:
        try:
            while True:
                tick = time.time()
                executor.submit(send_request)

                p95 = 0.0
                if stats["latencies"]:
                    sorted_lats = sorted(stats["latencies"])
                    p95 = sorted_lats[int(len(sorted_lats) * 0.95)]

                print(
                    f"Traffic Stats -> GET: {stats['get_success']} | "
                    f"POST: {stats['post_success']} | "
                    f"4xx: {stats['client_errors']} | "
                    f"5xx Server Errors: {stats['server_errors']} | "
                    f"p95: {p95:.1f}ms",
                    end="\r"
                )

                elapsed = time.time() - tick
                sleep_duration = max(0.0, INTERVAL - elapsed)
                time.sleep(sleep_duration)

        except KeyboardInterrupt:
            print("\n\nStopping traffic generator...")
            print(f"Final Report: GET={stats['get_success']}, POST={stats['post_success']}, "
                  f"4xx={stats['client_errors']}, 5xx Errors={stats['server_errors']}")

if __name__ == "__main__":
    main()