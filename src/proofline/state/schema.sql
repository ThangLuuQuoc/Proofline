-- Schema state/proofline.db (phiên bản 3; v2 thêm acp_sessions, v3 thêm acp_pending; chỉ thêm bảng nên DB cũ nâng cấp tại chỗ).
-- Mô tả cột: docs/04-execution-plan.md (runs, tasks, decisions, readiness_snapshots)
-- và docs/06-the-outer-loop.md (signals, clusters).
-- Cột JSON lưu dạng TEXT; thời gian lưu ISO 8601 (UTC).

CREATE TABLE IF NOT EXISTS runs (
    run_id          TEXT PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN ('eval', 'production', 'shadow')),
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    harness_version TEXT,
    config_hash     TEXT,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id               TEXT PRIMARY KEY,
    run_id                TEXT NOT NULL REFERENCES runs (run_id),
    case_id               TEXT,
    case_source           TEXT CHECK (case_source IN ('history', 'synthetic', 'accumulated')),
    issue_ref             TEXT,
    source                TEXT CHECK (source IN
                              ('label', 'spec_backlog', 'ci_failure', 'staging_error',
                               'self_sourced', 'outer_loop', 'eval')),
    repo                  TEXT,
    task_type             TEXT,
    task_class            TEXT,
    size                  TEXT CHECK (size IN ('S', 'M', 'L')),
    attempt_no            INTEGER NOT NULL DEFAULT 1,
    status                TEXT NOT NULL CHECK (status IN ('running', 'passed', 'failed', 'aborted')),
    started_at            TEXT NOT NULL,
    finished_at           TEXT,
    duration_seconds      REAL,
    input_tokens          INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens         INTEGER NOT NULL DEFAULT 0,
    cost_usd              REAL NOT NULL DEFAULT 0,
    models_used           TEXT,
    verifier_gate_results TEXT,
    risk_score            INTEGER CHECK (risk_score BETWEEN 0 AND 10),
    hidden_tests_passed   INTEGER CHECK (hidden_tests_passed IN (0, 1)),
    failure_reason        TEXT,
    failure_detail        TEXT,
    branch                TEXT,
    pr_url                TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_run ON tasks (run_id);
CREATE INDEX IF NOT EXISTS idx_tasks_case ON tasks (case_id);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id        TEXT PRIMARY KEY,
    task_id            TEXT REFERENCES tasks (task_id),
    decision_type      TEXT NOT NULL,
    task_class         TEXT,
    autonomy_level     TEXT NOT NULL CHECK (autonomy_level IN ('L0', 'L1', 'L2', 'L3', 'L4')),
    agent_choice       TEXT,
    agent_confidence   REAL CHECK (agent_confidence BETWEEN 0 AND 1),
    agent_rationale    TEXT,
    human_choice       TEXT,
    human_reason       TEXT,
    decided_by         TEXT NOT NULL CHECK (decided_by IN ('agent', 'human')),
    audit_sampled      INTEGER NOT NULL DEFAULT 0 CHECK (audit_sampled IN (0, 1)),
    outcome            TEXT,
    outcome_checked_at TEXT,
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_type ON decisions (decision_type, task_class);

CREATE TABLE IF NOT EXISTS readiness_snapshots (
    snapshot_id     TEXT PRIMARY KEY,
    repo            TEXT NOT NULL,
    assessed_at     TEXT NOT NULL,
    level           TEXT NOT NULL CHECK (level IN ('R0', 'R1', 'R2', 'R3', 'R4')),
    criteria        TEXT NOT NULL,
    missing_actions TEXT
);
CREATE INDEX IF NOT EXISTS idx_readiness_repo ON readiness_snapshots (repo, assessed_at);

CREATE TABLE IF NOT EXISTS clusters (
    cluster_id            TEXT PRIMARY KEY,
    title                 TEXT NOT NULL,
    symptom               TEXT,
    feature_area          TEXT,
    first_seen            TEXT NOT NULL,
    last_seen             TEXT NOT NULL,
    frequency_window      INTEGER NOT NULL DEFAULT 0,
    users_affected_window INTEGER NOT NULL DEFAULT 0,
    priority_score        REAL,
    status                TEXT NOT NULL CHECK (status IN
                              ('new', 'needs_info', 'reproduced', 'issue_created',
                               'fixed_pending_release', 'released', 'resolved',
                               'partially_resolved', 'not_resolved', 'regressed', 'reopened')),
    repro_ref             TEXT,
    issue_ref             TEXT,
    pr_refs               TEXT,
    release_ref           TEXT,
    outcome               TEXT
);

-- Không có cột nội dung thô: chỉ lưu bản đã che dữ liệu cá nhân + link về nguồn.
CREATE TABLE IF NOT EXISTS signals (
    signal_id        TEXT PRIMARY KEY,
    source           TEXT NOT NULL CHECK (source IN
                         ('error_tracker', 'support_ticket', 'community_chat', 'team_discussion')),
    external_ref     TEXT NOT NULL,
    received_at      TEXT NOT NULL,
    redacted_summary TEXT,
    structured       TEXT,
    injection_flag   INTEGER NOT NULL DEFAULT 0 CHECK (injection_flag IN (0, 1)),
    cluster_id       TEXT REFERENCES clusters (cluster_id)
);
CREATE INDEX IF NOT EXISTS idx_signals_cluster ON signals (cluster_id);

-- v2: phiên ACP (docs/07-acp-pandacode.md). Client mở một tiến trình mỗi lượt, nên phiên phải
-- nạp lại được từ DB.
CREATE TABLE IF NOT EXISTS acp_sessions (
    session_id TEXT PRIMARY KEY,
    cwd        TEXT NOT NULL,
    repo       TEXT,
    mode       TEXT NOT NULL DEFAULT 'default',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- v3: câu hỏi Proofline đang chờ người trả lời bằng chữ ở lượt sau (góp ý khi duyệt, lý do từ chối).
CREATE TABLE IF NOT EXISTS acp_pending (
    session_id TEXT PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('approval_note', 'reject_reason')),
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
