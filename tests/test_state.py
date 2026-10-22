from __future__ import annotations

import sqlite3

import pytest

from proofline.state import SCHEMA_VERSION, SchemaVersionError, connect, init_db, table_names

EXPECTED_TABLES = {"runs", "tasks", "decisions", "readiness_snapshots", "signals", "clusters"}


def test_init_creates_all_tables(tmp_path):
    conn = init_db(tmp_path / "state" / "proofline.db")
    assert EXPECTED_TABLES <= set(table_names(conn))
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    conn.close()


def test_init_is_idempotent_and_keeps_data(tmp_path):
    path = tmp_path / "proofline.db"
    conn = init_db(path)
    with conn:
        conn.execute(
            "INSERT INTO runs (run_id, kind, started_at)"
            " VALUES ('r1', 'eval', '2026-10-21T00:00:00Z')"
        )
    conn.close()

    conn = init_db(path)
    assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
    conn.close()


def test_rejects_newer_schema(tmp_path):
    path = tmp_path / "proofline.db"
    conn = connect(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(SchemaVersionError):
        init_db(path)


def test_check_constraints(tmp_path):
    conn = init_db(tmp_path / "proofline.db")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO runs (run_id, kind, started_at) VALUES ('r1', 'bogus', '2026-10-21')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO decisions"
            " (decision_id, decision_type, autonomy_level, decided_by, created_at)"
            " VALUES ('d1', 'triage_accept', 'L9', 'agent', '2026-10-21')"
        )
    conn.close()


def test_foreign_keys_enforced(tmp_path):
    conn = init_db(tmp_path / "proofline.db")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO tasks (task_id, run_id, status, started_at)"
            " VALUES ('t1', 'missing-run', 'running', '2026-10-21')"
        )
    conn.close()


def test_signals_have_no_raw_content_column(tmp_path):
    conn = init_db(tmp_path / "proofline.db")
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(signals)")}
    assert "raw_content" not in columns
    assert {"redacted_summary", "external_ref", "injection_flag"} <= columns
    conn.close()
