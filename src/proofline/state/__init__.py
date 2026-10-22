"""Kho trạng thái SQLite (`state/proofline.db`)."""

from __future__ import annotations

import sqlite3
from importlib.resources import files
from pathlib import Path

SCHEMA_VERSION = 2


class SchemaVersionError(Exception):
    """Database được tạo bởi phiên bản schema mới hơn code đang chạy."""


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: str | Path) -> sqlite3.Connection:
    """Tạo (hoặc mở) database và áp schema. Gọi lại nhiều lần vẫn an toàn."""
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        conn.close()
        raise SchemaVersionError(
            f"{db_path} dùng schema v{version}, code chỉ hỗ trợ tới v{SCHEMA_VERSION}"
        )
    schema = files("proofline.state").joinpath("schema.sql").read_text(encoding="utf-8")
    with conn:
        conn.executescript(schema)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return conn


def table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    return [row["name"] for row in rows]


def record_human_decision(
    db_path: str | Path,
    decision_type: str,
    choice: str,
    reason: str = "",
    autonomy_level: str = "L1",
) -> str:
    """Ghi một quyết định do người đưa ra (vd duyệt journey) vào bảng decisions."""
    import uuid
    from datetime import UTC, datetime

    decision_id = uuid.uuid4().hex
    conn = init_db(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO decisions (decision_id, decision_type, autonomy_level, human_choice,"
                " human_reason, decided_by, created_at) VALUES (?, ?, ?, ?, ?, 'human', ?)",
                (
                    decision_id,
                    decision_type,
                    autonomy_level,
                    choice,
                    reason,
                    datetime.now(UTC).isoformat(timespec="seconds"),
                ),
            )
    finally:
        conn.close()
    return decision_id
