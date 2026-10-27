# Checkpoint — OpsTrail hardening (WIP)

Paused at the user's request on 2026-10-27 (Asia/Ho_Chi_Minh). Resume in the same chat/repository. This branch is work in progress and must not be deployed yet.

## User decisions and authorization

- Internal use; Docker Compose; PostgreSQL managed by internal team.
- Peak below 100 messages/s. Benchmark currently assumes alert events/s, not webhook requests/s.
- Aggregate old notifications after outage; harden OpsTrail before new features.
- User authorized implementing the full hardening sequence and pushing changes to Git.
- User is shutting down the computer; do not keep running tasks or schedule an automatic wakeup.

## Implemented so far

- Immutable Jinja sandbox, removed globals, restricted callable access, bounded template/context/output and some expensive operations; viewer preview requires editor role. Default card and guide examples updated to avoid mutation.
- Intake serializes alert identities using advisory transaction locks; notification snapshots are queued inside the same transaction. Snapshots captured per transition, including multiple transitions in one payload.
- Destination leases/tokens, completion fencing, per-destination throttle, retry jitter/Retry-After, persistent backlog digest membership and frozen payload. Legacy payloads are captured at first claim and do not reconstruct historic state.
- Dedicated worker, maintenance owner lock, worker heartbeat, readiness and authenticated metrics.
- Retention keeps firing alerts and pending work; resolved-age predicate uses ends_at/updated_at and rechecks on DELETE.
- Production config checks, browser Origin check, non-root Dockerfile, pinned dependency files, production Compose, backup/restore-test scripts, nginx/Prometheus examples and CI scaffold.
- Operator CLI requeue for failed deliveries (most recent addition, not yet integration-tested).
- Migration ab1200261027 and real PostgreSQL concurrency/fault tests; safe test database suffix guard.

## Verified

- Existing 346 tests ran against isolated PostgreSQL 16: all passed, no skips.
- With 15 new tests: 361 passed, 1 existing Starlette deprecation warning. Later small changes (CLI requeue, stricter config and log sanitization) need another full run.
- Latest lint: `ruff check app/ tests/ deploy/load_test.py` passes. Shell scripts pass `sh -n`.
- Migration from empty DB, downgrade to previous revision, upgrade again and Alembic schema drift check passed on disposable database.
- Production Compose structure validates with example env paths.
- First 30-second benchmark accepted 3,000 events with zero HTTP errors, ~99.96 events/s, p95 ~105.5ms, p99 ~198.5ms. Its original cross-request episode generator allowed out-of-order transitions (2,001 rather than 2,000 deliveries). Generator now keeps each episode within its own request and validates expected counts; re-run benchmark.

## Incomplete / next steps

1. Review new worker/digest code carefully, including lost lease during in-flight HTTP, ordering, concurrent claim/cancellation, bounded payload size, target fairness and legacy deliveries. A remote endpoint may accept a request before process death: this is still at-least-once/ambiguous, not exactly-once.
2. Finish tests for production config, Origin protection, readiness/metrics, expired pending retention, digest team separation/size, manual requeue, unexpected outbound error isolation, fresh resolved alerts with old starts_at. Confirm template resource limits are adequate for trusted internal editors; sandbox alone is not a complete resource isolation guarantee.
3. Runtime lock was derived from installed versions; opentelemetry-api was added after the first Docker build started. Validate the lock using a clean image and `pip check`; consider hashes/base-image digest. An attempted uv installation is not required by the project.
4. Docker build was deliberately interrupted while downloading packages. Build and run the final image, smoke-test API + worker + migration with production-like env and read-only/non-root Compose settings.
5. Run backup and restore drill on disposable data; capture counts, schema version and elapsed time. Review backup ownership/off-host storage and monitoring; no real internal DB/Teams was contacted.
6. Add a production operations/pilot runbook, update README/config/architecture docs to actual behavior, and record results. Review existing docs for obsolete 60-minute retry assumptions; worker now uses BACKLOG_RETENTION_HOURS (default proposal: 168h).
7. Re-run final full suite, migration/schema check, load tests (batch 1/10/100, sustained 100 events/s, outbound down/healthy), outage/restart local pilot and record limitations. The 10-minute run was stopped for shutdown, so it is NOT a successful completed test.
8. Pilot against the actual internal host/Teams and 7-day monitoring requires deployment details/user participation. Do not mark this complete from local tests.
9. Commit final fixes and push this branch. A WIP checkpoint push was requested before shutdown; main is unchanged.

## Local disposable resources

- Docker context: **desktop-linux**. Default context is unavailable; do not change the global context.
- Container: `opstrail-review-postgres`, PostgreSQL 16, loopback port **55432**, user/password `opstrail` (disposable test only). It was stopped, not deleted, to preserve evidence.
- Databases: `opstrail_test` (test fixtures drop/recreate tables), `opstrail_migration`, `opstrail_load_test`.
- Restart: `docker --context desktop-linux start opstrail-review-postgres`.
- Full suite: `TEST_DATABASE_URL=postgresql+asyncpg://opstrail:opstrail@127.0.0.1:55432/opstrail_test REQUIRE_TEST_DATABASE=1 .venv/bin/python -m pytest tests/ -q`.
- API port18000, worker, benchmark and Docker build were explicitly stopped for shutdown. Logs: `/tmp/opstrail-load-api.log`, `/tmp/opstrail-load-worker.log`. Partial load data remains in disposable DB; `load-results/` is ignored.
- Do not commit `.env`, `.env.local`, `.env.production`, `.env.backup` or dumps.

## Preserve unrelated user work

Existing modifications to `docs/opstrail-overview.html`, untracked `docs/opstrail-overview_old.html` and `plan_v2/` predate implementation and must remain outside this commit/branch's staged changes.
