"""Protect disposable integration databases and fail closed in CI."""

import os

import pytest
from sqlalchemy.engine import make_url


def pytest_sessionstart(session):
    dsn = os.getenv("TEST_DATABASE_URL")
    if dsn and not (make_url(dsn).database or "").endswith("_test"):
        raise pytest.UsageError("TEST_DATABASE_URL must name a disposable *_test database")
    if os.getenv("REQUIRE_TEST_DATABASE") == "1" and not dsn:
        raise pytest.UsageError("REQUIRE_TEST_DATABASE requires explicit TEST_DATABASE_URL")


def pytest_sessionfinish(session, exitstatus):
    if os.getenv("REQUIRE_TEST_DATABASE") == "1":
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if any(
            "no test database" in str(report.longrepr)
            for report in reporter.stats.get("skipped", [])
        ):
            session.exitstatus = 1
