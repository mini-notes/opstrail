"""Low-cardinality intake metrics for a single API process per container.

Counters reset on process restart. Prometheus rate() handles resets; use one
uvicorn process per API container so all scrapes observe the same counters.
"""

from collections import Counter

BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
requests = Counter()
buckets = Counter()
duration_sum = 0.0


def observe(status_code: int, duration: float) -> None:
    global duration_sum
    requests[f"{status_code // 100}xx"] += 1
    duration_sum += duration
    for bound in BUCKETS:
        if duration <= bound:
            buckets[bound] += 1


def exposition() -> list[str]:
    total = sum(requests.values())
    lines = ["# TYPE opstrail_intake_requests_total counter"]
    for status in ("2xx", "3xx", "4xx", "5xx"):
        lines.append(
            f'opstrail_intake_requests_total{{status_class="{status}"}} {requests[status]}'
        )
    lines.append("# TYPE opstrail_intake_duration_seconds histogram")
    for bound in BUCKETS:
        lines.append(f'opstrail_intake_duration_seconds_bucket{{le="{bound}"}} {buckets[bound]}')
    lines += [
        f'opstrail_intake_duration_seconds_bucket{{le="+Inf"}} {total}',
        f"opstrail_intake_duration_seconds_count {total}",
        f"opstrail_intake_duration_seconds_sum {duration_sum}",
    ]
    return lines
