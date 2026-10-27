"""Paced HTTP intake benchmark. Use only a disposable environment.

OPSTRAIL_TEST_TOKEN=... python deploy/load_test.py --url http://127.0.0.1:18000 \
    --events-per-second 100 --batch-size 10 --duration 600 --output load-results/run.json
"""

import argparse
import asyncio
import json
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx


def percentile(samples, pct):
    ordered = sorted(samples)
    return ordered[min(int(len(ordered) * pct), len(ordered) - 1)] if ordered else None


async def run(args):
    if not args.url.startswith(("http://127.0.0.1:", "http://localhost:")):
        raise SystemExit("benchmark requires a localhost disposable API (use a tunnel if needed)")
    token = os.environ["OPSTRAIL_TEST_TOKEN"]
    run_id = uuid.uuid4().hex[:12]
    starts = datetime.now(UTC)
    latencies, errors = [], []
    counters = {"requests": 0, "events": 0, "queued": 0}
    semaphore = asyncio.Semaphore(args.concurrency)
    # Container startup is not part of the offered load. Wait for DB readiness
    # before starting the schedule so cold-start ConnectErrors are not mixed
    # into throughput measurements.
    async with asyncio.timeout(30), httpx.AsyncClient(timeout=3) as probe:
        while True:
            try:
                if (await probe.get(args.url.rstrip("/") + "/readyz")).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.25)
    began = time.perf_counter()
    tasks = set()
    max_lag = 0.0
    expected_deliveries = 0
    expected_alerts = 0

    async with httpx.AsyncClient(
        timeout=30,
        limits=httpx.Limits(
            max_connections=args.concurrency,
            max_keepalive_connections=args.concurrency,
        ),
    ) as client:

        async def send(batch):
            async with semaphore:
                start = time.perf_counter()
                try:
                    response = await client.post(
                        args.url.rstrip("/") + "/api/v1/webhooks/alertmanager",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"receiver": "benchmark", "alerts": batch},
                    )
                    if response.status_code != 202:
                        errors.append({"status": response.status_code})
                    else:
                        counters["events"] += response.json()["received"]
                        counters["queued"] += response.json()["queued"]
                except httpx.HTTPError as exc:
                    errors.append({"type": type(exc).__name__})
                counters["requests"] += 1
                latencies.append((time.perf_counter() - start) * 1000)

        total_events = int(args.events_per_second * args.duration)
        for offset in range(0, total_events, args.batch_size):
            due = began + offset / args.events_per_second
            await asyncio.sleep(max(0, due - time.perf_counter()))
            max_lag = max(max_lag, time.perf_counter() - due)
            # New episodes, repeats, then transitions, keyed identically within each triplet.
            batch = []
            for index in range(offset, min(offset + args.batch_size, total_events)):
                episode, phase = divmod(index - offset, 3)
                expected_alerts += phase == 0
                expected_deliveries += phase in (0, 2)
                status = "resolved" if phase == 2 else "firing"
                batch.append(
                    {
                        "fingerprint": f"load-{run_id}-{offset}-{episode}",
                        "status": status,
                        "startsAt": starts.isoformat(),
                        "endsAt": (starts + timedelta(minutes=1)).isoformat()
                        if status == "resolved"
                        else None,
                        "labels": {
                            "alertname": "LoadTest",
                            "severity": "warning",
                            "environment": "disposable-test",
                            "run_id": run_id,
                        },
                        "annotations": {"summary": "Disposable intake benchmark"},
                    }
                )
            task = asyncio.create_task(send(batch))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
            # Bound queued producers too: report schedule lag rather than consume unbounded RAM.
            if len(tasks) >= args.concurrency * 2:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if tasks:
            await asyncio.gather(*tasks)
    elapsed = time.perf_counter() - began
    result = {
        "run_id": run_id,
        "started_at": starts.isoformat(),
        "duration_seconds": elapsed,
        "offered_event_rate": args.events_per_second,
        "batch_size": args.batch_size,
        "expected_alerts": expected_alerts,
        "expected_deliveries": expected_deliveries,
        "concurrency": args.concurrency,
        **counters,
        "errors": len(errors),
        "error_samples": errors[:5],
        "accepted_events_per_second": counters["events"] / elapsed,
        "request_latency_ms": {
            "p50": percentile(latencies, 0.50),
            "p95": percentile(latencies, 0.95),
            "p99": percentile(latencies, 0.99),
        },
        "max_schedule_lag_seconds": max_lag,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    if errors or counters["events"] != total_events or counters["queued"] != expected_deliveries:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:18000")
    parser.add_argument("--events-per-second", type=int, default=100)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--duration", type=int, default=600)
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--output", type=Path, default=Path("load-results/run.json"))
    args = parser.parse_args()
    if min(args.events_per_second, args.batch_size, args.duration, args.concurrency) <= 0:
        parser.error("load parameters must be positive")
    asyncio.run(run(args))
