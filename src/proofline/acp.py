"""`proofline acp`: Proofline là một agent ACP (Agent Client Protocol) qua stdio.

Thiết kế: docs/07-acp-pandacode.md. Client (vd PandaCode) gửi JSON-RPC, mỗi thông điệp một dòng.
Bước hiện tại (kế hoạch thử, bước 1–2): khung giao thức và luồng chỉ đọc (`/assess`, `/status`).
Luồng có duyệt (`session/request_permission` + Decision Guard) là bước 3.

Client mở một tiến trình mỗi lượt, nên phiên được lưu trong bảng `acp_sessions` và nạp lại được.
Không bao giờ in gì ra stdout ngoài giao thức; log đi stderr.
"""

from __future__ import annotations

import json
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from proofline import __version__
from proofline.assess import AssessContext, assess, find_source_feature_docs, save_snapshot
from proofline.config import ConfigError, ProoflineConfig, TargetRepo, load_config
from proofline.state import connect, init_db
from proofline.verify import git, load_manifest

PROTOCOL_VERSION = 1
MODES = [
    {"id": "ask", "name": "Standard", "description": "Mọi quyết định của người đều hỏi trước."},
    {
        "id": "plan",
        "name": "Plan",
        "description": "Chỉ đọc và lập kế hoạch, không chạy lệnh của repo.",
    },
]
COMMANDS = [
    {"name": "assess", "description": "Đo mức sẵn sàng R0–R4 của repo này (kiểm tra tĩnh)."},
    {"name": "status", "description": "Repo, mức sẵn sàng gần nhất, các spec và kết quả verify."},
    {"name": "help", "description": "Các lệnh Proofline dùng được ở đây."},
]
STATUS_ICON = {"pass": "✅", "fail": "❌", "unknown": "⚠️"}
TOOL_STATUS = {"pass": "completed", "fail": "failed", "unknown": "completed"}


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Session:
    session_id: str
    cwd: Path
    repo: str | None
    mode: str = "ask"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class AcpServer:
    def __init__(
        self, reader: TextIO, writer: TextIO, config_loader: Callable[[], ProoflineConfig]
    ):
        self.reader = reader
        self.writer = writer
        self.config_loader = config_loader
        self._config: ProoflineConfig | None = None
        self.cancelled: set[str] = set()
        # Work to do right after a response is sent (e.g. announce commands once the client
        # knows the new session id).
        self._after_response: Callable[[], None] | None = None

    # ---- transport ----

    def send(self, message: dict[str, Any]) -> None:
        self.writer.write(json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=False) + "\n")
        self.writer.flush()

    def notify(self, session_id: str, update: dict[str, Any]) -> None:
        self.send(
            {"method": "session/update", "params": {"sessionId": session_id, "update": update}}
        )

    def say(self, session_id: str, text: str) -> None:
        self.notify(
            session_id,
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}},
        )

    def serve(self) -> None:
        for line in self.reader:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self.send({"id": None, "error": {"code": -32700, "message": "Parse error"}})
                continue
            self.handle(message)

    def handle(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        msg_id = message.get("id")
        if method is None:
            return  # a response to a request of ours; none are sent yet in this step
        handler = getattr(self, "rpc_" + method.replace("/", "_"), None)
        if handler is None:
            if msg_id is not None:
                self.send(
                    {
                        "id": msg_id,
                        "error": {"code": -32601, "message": f"Method not found: {method}"},
                    }
                )
            return
        self._after_response = None
        try:
            result = handler(message.get("params") or {})
        except RpcError as exc:
            if msg_id is not None:
                self.send({"id": msg_id, "error": {"code": exc.code, "message": exc.message}})
            return
        except Exception as exc:  # never kill the agent on one bad request
            print(f"[proofline acp] {method} failed: {exc!r}", file=sys.stderr)
            if msg_id is not None:
                self.send({"id": msg_id, "error": {"code": -32603, "message": str(exc)}})
            return
        if msg_id is not None:
            self.send({"id": msg_id, "result": result})
        if self._after_response is not None:
            self._after_response()
            self._after_response = None

    # ---- state ----

    @property
    def config(self) -> ProoflineConfig:
        if self._config is None:
            try:
                self._config = self.config_loader()
            except ConfigError as exc:
                raise RpcError(-32000, str(exc)) from exc
        return self._config

    def _db(self):
        return init_db(self.config.workspace / self.config.factory.state_db)

    def _save(self, session: Session) -> None:
        conn = self._db()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO acp_sessions (session_id, cwd, repo, mode, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET"
                    " cwd = excluded.cwd, repo = excluded.repo, mode = excluded.mode,"
                    " updated_at = excluded.updated_at",
                    (
                        session.session_id,
                        str(session.cwd),
                        session.repo,
                        session.mode,
                        _now(),
                        _now(),
                    ),
                )
        finally:
            conn.close()

    def _load(self, session_id: str) -> Session:
        conn = self._db()
        try:
            row = conn.execute(
                "SELECT * FROM acp_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise RpcError(-32002, f"Unknown session: {session_id}")
        return Session(row["session_id"], Path(row["cwd"]), row["repo"], row["mode"])

    def target_for(self, cwd: Path) -> TargetRepo | None:
        """Repo đích của thư mục phiên: cùng kho git (worktree cũng tính) với một local_path."""
        try:
            common = Path(
                git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd)
            ).resolve()
        except ConfigError:
            return None
        for target in self.config.target_repos:
            local = (self.config.workspace / target.local_path).resolve()
            if not local.is_dir():
                continue
            try:
                other = git(["rev-parse", "--path-format=absolute", "--git-common-dir"], local)
            except ConfigError:
                continue
            if Path(other).resolve() == common:
                return target
        return None

    # ---- ACP methods ----

    def rpc_initialize(self, params: dict) -> dict:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "agentCapabilities": {
                "loadSession": True,
                "promptCapabilities": {"image": False, "audio": False, "embeddedContext": False},
            },
            "authMethods": [],
            "agentInfo": {"name": "proofline", "version": __version__},
        }

    def _modes(self, current: str) -> dict:
        return {"currentModeId": current, "availableModes": MODES}

    def _announce_commands(self, session_id: str) -> None:
        self.notify(
            session_id,
            {"sessionUpdate": "available_commands_update", "availableCommands": COMMANDS},
        )

    def rpc_session_new(self, params: dict) -> dict:
        cwd = Path(params.get("cwd") or ".").resolve()
        target = self.target_for(cwd)
        session = Session(uuid.uuid4().hex, cwd, target.name if target else None)
        self._save(session)
        self._after_response = lambda: self._announce_commands(session.session_id)
        return {"sessionId": session.session_id, "modes": self._modes(session.mode)}

    def rpc_session_load(self, params: dict) -> None:
        session = self._load(params.get("sessionId", ""))
        if params.get("cwd"):
            session.cwd = Path(params["cwd"]).resolve()
            target = self.target_for(session.cwd)
            session.repo = target.name if target else session.repo
            self._save(session)
        self._after_response = lambda: self._announce_commands(session.session_id)
        return None

    def rpc_session_set_mode(self, params: dict) -> dict:
        session = self._load(params.get("sessionId", ""))
        mode = params.get("modeId")
        if mode not in {m["id"] for m in MODES}:
            raise RpcError(-32602, f"Unknown mode: {mode}")
        session.mode = mode
        self._save(session)
        return {}

    def rpc_session_cancel(self, params: dict) -> None:
        self.cancelled.add(params.get("sessionId", ""))

    def rpc_session_prompt(self, params: dict) -> dict:
        session = self._load(params.get("sessionId", ""))
        self.cancelled.discard(session.session_id)
        text = " ".join(
            block.get("text", "")
            for block in params.get("prompt") or []
            if block.get("type") == "text"
        ).strip()
        command, _, rest = text.partition(" ")
        if command in ("/assess", "assess"):
            self.cmd_assess(session)
        elif command in ("/status", "status"):
            self.cmd_status(session)
        elif command in ("/help", "help"):
            self.cmd_help(session)
        else:
            self.say(
                session.session_id,
                "Proofline ở bước này mới có luồng chỉ đọc. Dùng `/assess` để đo mức sẵn sàng"
                " của repo này, `/status` để xem trạng thái. Nhận việc, viết spec và chạy gate"
                " qua đây là các bước tiếp theo "
                "(docs/07-acp-pandacode.md).",
            )
        stop = "cancelled" if session.session_id in self.cancelled else "end_turn"
        return {"stopReason": stop}

    # ---- commands ----

    def _require_repo(self, session: Session) -> TargetRepo | None:
        if session.repo:
            try:
                return self.config.target_repo(session.repo)
            except ConfigError:
                pass
        self.say(
            session.session_id,
            f"Thư mục `{session.cwd}` chưa thuộc repo đích nào của workspace. "
            f"Thêm bằng: `proofline target add {session.cwd}`.",
        )
        return None

    def cmd_help(self, session: Session) -> None:
        lines = ["Lệnh Proofline:"] + [f"- `/{c['name']}`: {c['description']}" for c in COMMANDS]
        self.say(session.session_id, "\n".join(lines))

    def cmd_assess(self, session: Session) -> None:
        target = self._require_repo(session)
        if target is None:
            return
        sid = session.session_id
        ctx = AssessContext(
            config=self.config, repo=target, repo_path=session.cwd, workspace=self.config.workspace
        )
        call_id = f"assess-{uuid.uuid4().hex[:8]}"
        self.notify(
            sid,
            {
                "sessionUpdate": "tool_call",
                "toolCallId": call_id,
                "title": f"Assess {target.name}",
                "kind": "read",
                "status": "in_progress",
            },
        )
        result = assess(ctx)
        save_snapshot(self.config.workspace / self.config.factory.state_db, result)
        for c in result.criteria:
            self.notify(
                sid,
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": f"{call_id}-{c.key}",
                    "title": f"{c.level} · {c.key}",
                    "kind": "read",
                    "status": TOOL_STATUS[c.status],
                    "content": [
                        {
                            "type": "content",
                            "content": {
                                "type": "text",
                                "text": f"{STATUS_ICON[c.status]} {c.detail}",
                            },
                        }
                    ],
                },
            )
        self.notify(
            sid, {"sessionUpdate": "tool_call_update", "toolCallId": call_id, "status": "completed"}
        )
        missing = result.missing_for_next_level()
        lines = [f"**{target.name}: mức sẵn sàng {result.level}**"]
        if missing and result.next_level():
            lines.append(f"\nĐể lên {result.next_level()}:")
            lines += [f"- {c.action or c.detail}" for c in missing]
        lines.append("\n(Kiểm tra tĩnh; chạy lệnh của repo bằng `proofline assess --run` ở CLI.)")
        self.say(sid, "\n".join(lines))

    def cmd_status(self, session: Session) -> None:
        target = self._require_repo(session)
        if target is None:
            return
        sid = session.session_id
        lines = [
            f"**Repo `{target.name}`** · nhánh chính `{target.default_branch}`"
            f" · thư mục `{session.cwd}`"
        ]
        conn = connect(self.config.workspace / self.config.factory.state_db)
        try:
            row = conn.execute(
                "SELECT level, assessed_at FROM readiness_snapshots WHERE repo = ?"
                " ORDER BY assessed_at DESC LIMIT 1",
                (target.name,),
            ).fetchone()
        finally:
            conn.close()
        lines.append(
            f"- Mức sẵn sàng gần nhất: {row['level']} ({row['assessed_at']})"
            if row
            else "- Chưa assess lần nào: dùng `/assess`."
        )
        artifacts = target.artifacts_dir(session.cwd, self.config.workspace)
        specs = (
            sorted((artifacts / "specs").glob("*.yaml")) if (artifacts / "specs").is_dir() else []
        )
        if not specs:
            sources = find_source_feature_docs(session.cwd)
            lines.append(
                "- Chưa có spec nào."
                + (f" Tài liệu tính năng có sẵn: {', '.join(sources)}." if sources else "")
            )
        for spec in specs:
            try:
                manifest = load_manifest(spec)
            except ConfigError:
                lines.append(f"- Spec `{spec.name}`: manifest không hợp lệ")
                continue
            approved = (
                f"journey đã duyệt ({manifest.approved_blob[:7]})"
                if manifest.approved_blob
                else "journey **chưa duyệt**"
            )
            report = artifacts / "verify" / f"{manifest.name}.json"
            verdict = "chưa verify"
            if report.is_file():
                data = json.loads(report.read_text(encoding="utf-8"))
                status = data.get("status")
                verdict = (
                    f"verify {STATUS_ICON.get(status, '')} {status} ({data.get('verified_at')})"
                )
            lines.append(
                f"- Spec `{manifest.name}` ({manifest.task_type}, base `{manifest.base}`):"
                f" {approved}; {verdict}"
            )
        self.say(sid, "\n".join(lines))


def serve_stdio(config_path: Path | None) -> None:
    """Điểm vào của `proofline acp`: stdin/stdout UTF-8, xuống dòng `\\n` (kể cả trên Windows)."""
    sys.stdin.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[attr-defined]
    AcpServer(sys.stdin, sys.stdout, lambda: load_config(config_path)).serve()
