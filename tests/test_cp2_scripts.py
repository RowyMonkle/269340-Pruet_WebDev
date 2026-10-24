"""Unit tests for the CP2 scripts (no database or running API needed).

- scripts/backfill.py: the target-name rule must match the API and never lose a stored part.
- scripts/traffic_gen.py: response checks and the PASS/FAIL decision.
"""
import importlib.util
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "scripts", f"{name}.py"))
    assert spec is not None and spec.loader is not None  # spec_from_file_location may return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backfill = load("backfill")
traffic = load("traffic_gen")


@pytest.mark.parametrize(
    "full, first, last, expected",
    [
        ("John Smith", None, None, ("John", "Smith")),
        ("Cher", None, None, ("Cher", "")),
        ("  Anna  Maria   Lopez ", None, None, ("Anna", "Maria   Lopez")),  # same rule as the API, no leading space
        (None, "Cher", None, ("Cher", "")),            # new-mode row: must not be blanked out
        (None, "Jane", "Doe", ("Jane", "Doe")),        # already migrated: unchanged
        ("John Smith", "John", None, ("John", "Smith")),  # first stored, last missing
        ("John Smith", None, "Smith", ("John", "Smith")),  # last stored, first missing
        ("Old Name", "New", None, ("New", "")),        # stored first wins over a stale full_name
    ],
)
def test_backfill_names_for(full, first, last, expected):
    assert backfill.names_for(full, first, last) == expected


def test_backfill_never_returns_null():
    for args in [(None, None, None), ("", None, None), ("Solo", None, None)]:
        first, last = backfill.names_for(*args)
        assert first is not None and last is not None


def user_body(**over):
    body = {
        "id": 1, "email": "a@example.com", "username": "a", "role": "fan",
        "created_at": "2026-10-11T00:00:00Z", "full_name": "John Smith",
        "first_name": "John", "last_name": "Smith",
    }
    body.update(over)
    return body


def test_traffic_accepts_consistent_body():
    assert traffic.check_user_body(user_body()) is None
    assert traffic.check_user_body(user_body(full_name="Cher", first_name="Cher", last_name="")) is None


def test_traffic_flags_missing_and_inconsistent_fields():
    assert "missing" in traffic.check_user_body(user_body(email=None))
    assert "name fields missing" in traffic.check_user_body(user_body(first_name=None))
    assert "does not match" in traffic.check_user_body(user_body(last_name="Jones"))


def test_traffic_percentile():
    values = [float(i) for i in range(1, 101)]
    assert traffic.percentile(values, 50) == 51.0
    assert traffic.percentile(values, 99) == 100.0
    assert traffic.percentile([], 99) == 0.0


def make_stats(ok=0, status_codes=()):
    stats = traffic.Stats()
    for _ in range(ok):
        stats.record("GET", 200, 10.0, None)
    for code in status_codes:
        stats.record("POST", code, 10.0, None)
    return stats


def test_report_passes_on_clean_run(capsys):
    assert traffic.report(make_stats(ok=100), elapsed=4.0, min_rps=20) == 0


@pytest.mark.parametrize("bad", [[500], [503], [404], [None]])
def test_report_fails_on_any_error(bad, capsys):
    assert traffic.report(make_stats(ok=100, status_codes=bad), elapsed=4.0, min_rps=20) == 1


def test_report_fails_when_rate_too_low_or_p99_too_high(capsys):
    assert traffic.report(make_stats(ok=100), elapsed=100.0, min_rps=20) == 1
    slow = traffic.Stats()
    for _ in range(100):
        slow.record("GET", 200, 900.0, None)
    assert traffic.report(slow, elapsed=4.0, min_rps=20) == 1


def test_report_fails_on_data_error(capsys):
    stats = make_stats(ok=100)
    stats.record("GET", 200, 10.0, "missing field(s) ['id']")
    assert traffic.report(stats, elapsed=4.0, min_rps=20) == 1
