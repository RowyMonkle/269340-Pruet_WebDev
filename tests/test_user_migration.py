"""CP2 users.full_name -> first_name + last_name: name rules and verification file.

No database needed. Covers:
1. The split / join / write-mode rules the dual-write and backfill must agree on.
2. Backward compatibility of UserCreate (legacy payload, split payload, neither).
3. scripts/sql/verify_users_migration.sql is parseable and every stage has checks.
"""
import importlib.util
import os

import pytest
from pydantic import ValidationError

from app.schemas.user import UserCreate
from app.services.user_service import join_names, resolve_user_names, split_full_name

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_user(**names) -> UserCreate:
    return UserCreate(email="t@example.com", username="tester", password="secret1", **names)


def test_split_full_name_two_words():
    assert split_full_name("John Smith") == ("John", "Smith")


def test_split_full_name_single_word_has_empty_last_name():
    assert split_full_name("Cher") == ("Cher", "")


def test_split_full_name_keeps_everything_after_first_word():
    assert split_full_name("  Anna Maria Lopez ") == ("Anna", "Maria Lopez")


def test_join_names_ignores_blank_parts():
    assert join_names("John", "") == "John"
    assert join_names(" John ", " Smith ") == "John Smith"


@pytest.mark.parametrize(
    "mode, expected",
    [
        ("legacy", ("John Smith", None, None)),
        ("dual", ("John Smith", "John", "Smith")),
        ("new", (None, "John", "Smith")),
    ],
)
def test_resolve_user_names_legacy_payload(mode, expected):
    assert resolve_user_names(make_user(full_name="John Smith"), mode) == expected


@pytest.mark.parametrize(
    "mode, expected",
    [
        ("legacy", ("John Smith", None, None)),
        ("dual", ("John Smith", "John", "Smith")),
        ("new", (None, "John", "Smith")),
    ],
)
def test_resolve_user_names_split_payload(mode, expected):
    assert resolve_user_names(make_user(first_name="John", last_name="Smith"), mode) == expected


def test_dual_write_fills_both_sides_for_single_word_full_name():
    assert resolve_user_names(make_user(full_name="Cher"), "dual") == ("Cher", "Cher", "")


@pytest.mark.parametrize("mode", ["dual", "new"])
def test_single_first_name_stores_empty_last_name_not_null(mode):
    # NULL would leave the user "unmigrated" forever and block the Phase 5 guard
    _, first, last = resolve_user_names(make_user(first_name="Cher"), mode)
    assert (first, last) == ("Cher", "")


def test_first_name_with_matching_full_name_takes_last_name_from_it():
    assert resolve_user_names(make_user(full_name="John Smith", first_name="John"), "dual") == (
        "John Smith", "John", "Smith",
    )


def test_user_create_accepts_legacy_and_split_payloads():
    assert make_user(full_name="John Smith").full_name == "John Smith"
    assert make_user(first_name="John", last_name="Smith").first_name == "John"


def test_user_create_rejects_payload_without_any_name():
    with pytest.raises(ValidationError):
        make_user()


def _load_verify_module():
    spec = importlib.util.spec_from_file_location(
        "verify_migration", os.path.join(ROOT, "scripts", "verify_migration.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_verification_sql_is_parseable_and_covers_every_stage():
    verify = _load_verify_module()
    checks = verify.parse_checks(verify.SQL_FILE)

    names = [check.name for check in checks]
    assert len(names) == len(set(names)), "duplicate check names"
    for check in checks:
        assert check.sql.lstrip().upper().startswith("SELECT"), check.name
        assert check.expect == "info" or check.expect.isdigit(), check.name
        assert all(stage in verify.STAGES or stage == "-" for stage in check.stages), check.name

    for stage in verify.STAGES:
        enforced = [c for c in checks if stage in c.stages and c.expect != "info"]
        assert enforced, f"stage {stage} has no pass/fail check"

    # the snapshot helpers the runner looks up by name
    for needed in ("snapshot_max_id", "snapshot_checksum", "snapshot_count"):
        assert needed in names


def test_contract_checks_only_run_after_contract():
    verify = _load_verify_module()
    checks = {c.name: c for c in verify.parse_checks(verify.SQL_FILE)}
    # these query information_schema for full_name, so they are meaningless earlier
    assert checks["full_name_column_dropped"].stages == ["after-contract"]
    # these read users.full_name, so they must never run once it is dropped
    assert "after-contract" not in checks["legacy_new_disagree"].stages
    assert "after-contract" not in checks["users_without_any_name"].stages


def test_legacy_new_check_is_skipped_once_legacy_writes_stop():
    verify = _load_verify_module()
    checks = {c.name: c for c in verify.parse_checks(verify.SQL_FILE)}
    # in write mode "new" PUT no longer maintains full_name, so a mismatch is expected there
    assert checks["legacy_new_disagree"].skip_when == "user_name_write_mode=new"
    assert checks["not_migrated"].skip_when == ""
