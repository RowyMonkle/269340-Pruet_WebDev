"""Charts and per-endpoint numbers from the raw k6 points of scripts/k6_script.js (docs/perf/k6_points.csv).

Run it from the repository root, after the benchmark (see the header of scripts/k6_script.js). A throw-away container
is enough, nothing is installed on the host:

  docker run --rm -v "$PWD/docs/perf:/out" python:3.11-slim python /out/make_k6_charts.py

It writes k6_throughput.png, k6_latency.png, k6_endpoints.png, k6_outcomes.png and k6_stats.json next to the points file.
"""
import csv
import json
import statistics
import subprocess
import sys
from collections import Counter, defaultdict

subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check", "matplotlib"], check=True,
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

SRC, OUT = "/out/k6_points.csv", "/out"
durations = []            # (second, ms, endpoint)
requests_per_second = Counter()
vus = {}
status_by_endpoint = defaultdict(Counter)
expected_by_endpoint = defaultdict(Counter)

with open(SRC, newline="", encoding="utf-8") as fh:
    for row in csv.DictReader(fh):
        metric, ts = row["metric_name"], int(float(row["timestamp"]))
        if metric == "http_req_duration":
            ep = row["name"] or row["url"]
            durations.append((ts, float(row["metric_value"]), ep))
            status_by_endpoint[ep][row["status"]] += 1
            expected_by_endpoint[ep][row["expected_response"]] += 1
        elif metric == "http_reqs":
            requests_per_second[ts] += 1
        elif metric == "vus":
            vus[ts] = float(row["metric_value"])

t0 = min(d[0] for d in durations)
t1 = max(d[0] for d in durations)
seconds = list(range(t0, t1 + 1))


def pct(values, q):
    values = sorted(values)
    if not values:
        return float("nan")
    k = (len(values) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


# ---- steady state = the seconds in which all 50 VUs were running
steady = [s for s in seconds if vus.get(s, 0) >= 50]
rps_steady = [requests_per_second[s] for s in steady[2:-2]]  # drop the edge seconds
stats = {
    "seconds_total": t1 - t0 + 1,
    "seconds_at_50_vus": len(steady),
    "rps_steady_mean": round(statistics.mean(rps_steady), 1),
    "rps_steady_min": min(rps_steady),
    "rps_steady_max": max(rps_steady),
    "rps_steady_stdev": round(statistics.pstdev(rps_steady), 1),
    "rps_steady_cv_percent": round(100 * statistics.pstdev(rps_steady) / statistics.mean(rps_steady), 1),
}
all_ms = [d[1] for d in durations]
steady_ms = [d[1] for d in durations if d[0] in set(steady)]
stats["overall"] = {"requests": len(all_ms), "p50": round(pct(all_ms, .5), 2), "p95": round(pct(all_ms, .95), 2),
                    "p99": round(pct(all_ms, .99), 2), "max": round(max(all_ms), 1)}
stats["steady"] = {"requests": len(steady_ms), "p50": round(pct(steady_ms, .5), 2), "p95": round(pct(steady_ms, .95), 2),
                   "p99": round(pct(steady_ms, .99), 2), "max": round(max(steady_ms), 1)}
per_ep = defaultdict(list)
for _, ms, ep in durations:
    per_ep[ep].append(ms)
stats["endpoints"] = {
    ep: {"requests": len(v), "p50": round(pct(v, .5), 1), "p95": round(pct(v, .95), 1), "p99": round(pct(v, .99), 1),
         "max": round(max(v), 1), "status": dict(status_by_endpoint[ep])}
    for ep, v in sorted(per_ep.items())
}
json.dump(stats, open(f"{OUT}/k6_stats.json", "w"), indent=2)

BLUE, ORANGE, RED, GREY, GREEN = "#1f77b4", "#ff7f0e", "#d62728", "#7f7f7f", "#2ca02c"

# ---- 1. throughput and VUs
fig, ax = plt.subplots(figsize=(10, 4.2))
x = [s - t0 for s in seconds]
ax.plot(x, [requests_per_second[s] for s in seconds], color=BLUE, lw=1.2, label="requests per second")
ax.axhline(stats["rps_steady_mean"], color=GREY, ls="--", lw=1, label=f"steady-state mean {stats['rps_steady_mean']} req/s")
ax.set_xlabel("seconds since the start of the test")
ax.set_ylabel("requests per second")
ax.set_ylim(0, max(requests_per_second.values()) * 1.25)
ax2 = ax.twinx()
ax2.step(x, [vus.get(s, 0) for s in seconds], color=ORANGE, lw=1.2, where="post", label="virtual users")
ax2.set_ylabel("virtual users")
ax2.set_ylim(0, 60)
h1, l1 = ax.get_legend_handles_labels()
h2, l2 = ax2.get_legend_handles_labels()
ax.legend(h1 + h2, l1 + l2, loc="lower right", fontsize=8)
ax.set_title("Throughput and virtual users (k6, 50 VUs for 3 minutes)")
ax.grid(alpha=.25)
fig.tight_layout()
fig.savefig(f"{OUT}/k6_throughput.png", dpi=110)
plt.close(fig)

# ---- 2. latency percentiles per 5 s window against the two thresholds
win = defaultdict(list)
for ts, ms, _ in durations:
    win[(ts - t0) // 5].append(ms)
wx = sorted(win)
fig, ax = plt.subplots(figsize=(10, 4.2))
for q, color, label in ((.5, GREEN, "p50"), (.95, BLUE, "p95"), (.99, ORANGE, "p99")):
    ax.plot([w * 5 + 2.5 for w in wx], [pct(win[w], q) for w in wx], color=color, lw=1.4, label=label)
ax.axhline(200, color=BLUE, ls=":", lw=1.2)
ax.text(1, 215, "p95 limit 200 ms", color=BLUE, fontsize=8)
ax.axhline(500, color=RED, ls=":", lw=1.2)
ax.text(1, 540, "p99 limit 500 ms", color=RED, fontsize=8)
ax.set_yscale("log")
ax.set_ylim(1, 1000)
ax.set_xlabel("seconds since the start of the test (5-second windows)")
ax.set_ylabel("response time (ms, log scale)")
ax.set_title("Latency over time against the CP3 thresholds")
ax.legend(loc="center right", fontsize=8)
ax.grid(alpha=.25, which="both")
fig.tight_layout()
fig.savefig(f"{OUT}/k6_latency.png", dpi=110)
plt.close(fig)

# ---- 3. p95 / p99 per endpoint
eps = sorted(stats["endpoints"], key=lambda e: stats["endpoints"][e]["p99"])
fig, ax = plt.subplots(figsize=(10, 4.6))
ypos = range(len(eps))
ax.barh([y + .2 for y in ypos], [stats["endpoints"][e]["p95"] for e in eps], height=.38, color=BLUE, label="p95")
ax.barh([y - .2 for y in ypos], [stats["endpoints"][e]["p99"] for e in eps], height=.38, color=ORANGE, label="p99")
for y, e in zip(ypos, eps):
    ax.text(stats["endpoints"][e]["p99"] + 1, y - .2, f"{stats['endpoints'][e]['requests']} req", va="center", fontsize=7)
ax.set_yticks(list(ypos))
ax.set_yticklabels(eps, fontsize=8)
ax.axvline(200, color=BLUE, ls=":", lw=1)
ax.set_xlabel("response time (ms)")
ax.set_title("Latency per endpoint (p95 and p99) with the number of requests")
ax.legend(fontsize=8, loc="lower right")
ax.grid(alpha=.25, axis="x")
fig.tight_layout()
fig.savefig(f"{OUT}/k6_endpoints.png", dpi=110)
plt.close(fig)

# ---- 4. outcome per endpoint: every status code, so a rejected request cannot hide
codes = sorted({c for ep in status_by_endpoint for c in status_by_endpoint[ep]})
fig, ax = plt.subplots(figsize=(10, 4.6))
left = [0] * len(eps)
palette = {"200": GREEN, "201": "#17becf", "409": ORANGE}
for c in codes:
    vals = [status_by_endpoint[e][c] for e in eps]
    ax.barh(list(ypos), vals, left=left, color=palette.get(c, RED), label=f"HTTP {c}")
    left = [l + v for l, v in zip(left, vals)]
for y, e in zip(ypos, eps):
    ax.text(sum(status_by_endpoint[e].values()) + 120, y, f"{sum(status_by_endpoint[e].values())}", va="center", fontsize=7)
ax.set_xlim(0, max(sum(status_by_endpoint[e].values()) for e in eps) * 1.12)
ax.set_yticks(list(ypos))
ax.set_yticklabels(eps, fontsize=8)
ax.set_xlabel("requests")
ax.set_title("Outcome of every request by endpoint (409 only where it is the correct answer)")
ax.legend(fontsize=8, loc="lower right")
ax.grid(alpha=.25, axis="x")
fig.tight_layout()
fig.savefig(f"{OUT}/k6_outcomes.png", dpi=110)
plt.close(fig)

print(json.dumps(stats, indent=2))
