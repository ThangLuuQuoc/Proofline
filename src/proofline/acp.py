"""`proofline acp`: Proofline là một agent ACP (Agent Client Protocol) qua stdio.

Thiết kế: docs/07-acp-pandacode.md. Client (vd PandaCode) gửi JSON-RPC, mỗi thông điệp một dòng.

- Bước 1–2: khung giao thức, luồng chỉ đọc (`/assess`, `/status`).
- Bước 3: luồng có duyệt. Quyết định của người đi qua `session/request_permission` với **hai**
  lựa chọn `allow_once` (để client không tự chọn hộ ở chế độ Full access), không bao giờ có
  `allow_always`; Decision Guard không tin câu trả lời đến quá nhanh. `/approve`, `/reject` là
  đường bằng chữ. `/verify` chạy lệnh của repo nên cũng phải được người cho phép.

Client mở một tiến trình mỗi lượt, nên phiên (`acp_sessions`) và câu hỏi đang chờ (`acp_pending`)
được lưu trong DB. Không in gì ra stdout ngoài giao thức; log đi stderr.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from proofline import __version__, knowledge
from proofline.assess import AssessContext, assess, find_source_feature_docs, save_snapshot
from proofline.config import ConfigError, ProoflineConfig, TargetRepo, load_config
from proofline.state import append_decision_reason, connect, init_db, record_decision
from proofline.targets import add_target
from proofline.verify import (
    JourneyApproval,
    SpecManifest,
    VerifyContext,
    blob_of,
    git,
    load_manifest,
    save_manifest,
    verify,
    write_report,
)

PROTOCOL_VERSION = 1
MAX_FACTS_IN_STATUS = 5
DEFAULT_GUARD_SECONDS = 2.0
MODES = [
    {"id": "default", "name": "Standard", "description": "Mọi quyết định của người đều hỏi trước."},
    {
        "id": "plan",
        "name": "Plan",
        "description": "Chỉ đọc và lập kế hoạch, không chạy lệnh của repo.",
    },
]
COMMANDS = [
    {
        "name": "init",
        "description": "Đăng ký thư mục này làm repo đích (chỉ ghi vào workspace), rồi assess.",
        "input": {"hint": "[tên repo] [--git-init]"},
    },
    {"name": "assess", "description": "Đo mức sẵn sàng R0–R4 của repo này (kiểm tra tĩnh)."},
    {"name": "status", "description": "Repo, mức sẵn sàng gần nhất, các spec và kết quả verify."},
    {
        "name": "spec",
        "description": "Xem spec và duyệt acceptance journey.",
        "input": {"hint": "[tên spec]"},
    },
    {
        "name": "approve",
        "description": "Duyệt journey bằng chữ.",
        "input": {"hint": "[tên spec] [góp ý]"},
    },
    {
        "name": "reject",
        "description": "Từ chối journey, kèm lý do.",
        "input": {"hint": "[tên spec] <lý do>"},
    },
    {
        "name": "verify",
        "description": "Chạy các gate của task (chạy lệnh của repo; cần bạn cho phép).",
        "input": {"hint": "[tên spec] [--runs N] [--no-install]"},
    },
    {"name": "help", "description": "Các lệnh Proofline dùng được ở đây."},
]
STATUS_ICON = {"pass": "✅", "fail": "❌", "unknown": "⚠️"}
TOOL_STATUS = {"pass": "completed", "fail": "failed", "unknown": "completed"}

# Mọi quyết định của người: hai lựa chọn allow_once + từ chối; không có allow_always.
APPROVE_OPTIONS = [
    {"optionId": "approve", "name": "Duyệt", "kind": "allow_once"},
    {"optionId": "approve_note", "name": "Duyệt, có góp ý", "kind": "allow_once"},
    {"optionId": "reject", "name": "Từ chối", "kind": "reject_once"},
]
GIT_INIT_OPTIONS = [
    {"optionId": "git_init", "name": "Khởi tạo git (nhánh main)", "kind": "allow_once"},
    {
        "optionId": "git_init_commit",
        "name": "Khởi tạo git + commit rỗng đầu tiên",
        "kind": "allow_once",
    },
    {"optionId": "cancel", "name": "Không", "kind": "reject_once"},
]
# Tin nhắn client tự gửi như prompt thường (PandaCode turn checks: src/turn-checks.ts).
CLIENT_AUTOMATED_PREFIXES = ("Project checks failed after your last turn",)
# Thư mục chỉ có các file này vẫn coi là trống.
IGNORABLE_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}
RUN_OPTIONS = [
    {"optionId": "run", "name": "Chạy verify", "kind": "allow_once"},
    {"optionId": "run_quick", "name": "Chạy nhanh (1 lượt, không cài lại)", "kind": "allow_once"},
    {"optionId": "cancel", "name": "Không chạy", "kind": "reject_once"},
]


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
    mode: str = "default"


@dataclass
class Decision:
    """Kết quả một yêu cầu duyệt sau Decision Guard."""

    option: str | None  # None = huỷ / không trả lời
    seconds: float
    suspected_auto: bool = False

    @property
    def allowed(self) -> bool:
        return (
            self.option is not None
            and not self.suspected_auto
            and not self.option.startswith(("reject", "cancel"))
        )


def _blob_text(repo: Path, blob: str | None) -> str | None:
    """Nội dung bản journey đã duyệt (để client hiện diff); None nếu chưa có hoặc không đọc được."""
    if not blob:
        return None
    out = subprocess.run(["git", "cat-file", "-p", blob], cwd=repo, capture_output=True)
    return out.stdout.decode("utf-8", errors="replace") if out.returncode == 0 else None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class AcpServer:
    def __init__(
        self,
        reader: TextIO,
        writer: TextIO,
        config_loader: Callable[[], ProoflineConfig],
        *,
        clock: Callable[[], float] = time.monotonic,
        guard_seconds: float | None = None,
    ):
        self.reader = reader
        self.writer = writer
        self.config_loader = config_loader
        self.clock = clock
        self._guard_seconds = guard_seconds
        self._config: ProoflineConfig | None = None
        self.cancelled: set[str] = set()
        self._backlog: list[dict[str, Any]] = []
        self._request_seq = 0
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

    def _read(self) -> dict[str, Any] | None:
        while True:
            line = self.reader.readline()
            if not line:
                return None
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                self.send({"id": None, "error": {"code": -32700, "message": "Parse error"}})

    def serve(self) -> None:
        while True:
            message = self._backlog.pop(0) if self._backlog else self._read()
            if message is None:
                return
            self.handle(message)

    def request(self, method: str, params: dict[str, Any]) -> tuple[dict[str, Any] | None, float]:
        """Gửi một yêu cầu tới client và chờ phản hồi; trong lúc chờ vẫn nhận `session/cancel`,
        các thông điệp khác để xử lý sau. Trả về (phản hồi, số giây chờ)."""
        self._request_seq += 1
        request_id = f"proofline-{self._request_seq}"
        sent = self.clock()
        self.send({"id": request_id, "method": method, "params": params})
        while True:
            message = self._read()
            if message is None:
                return None, self.clock() - sent
            if message.get("id") == request_id and "method" not in message:
                return message, self.clock() - sent
            if message.get("method") == "session/cancel":
                self.rpc_session_cancel(message.get("params") or {})
                if (message.get("params") or {}).get("sessionId") == params.get("sessionId"):
                    return None, self.clock() - sent
                continue
            self._backlog.append(message)

    def handle(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        msg_id = message.get("id")
        if method is None:
            return  # a late response to a request we no longer wait for
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

    @property
    def guard_seconds(self) -> float:
        if self._guard_seconds is not None:
            return self._guard_seconds
        value = self.config.extra_value("acp", "decision_guard_seconds", default=None)
        return float(value) if value is not None else DEFAULT_GUARD_SECONDS

    @property
    def db_path(self) -> Path:
        return self.config.workspace / self.config.factory.state_db

    def _db(self):
        return init_db(self.db_path)

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

    def _set_pending(self, session_id: str, kind: str, payload: dict[str, Any]) -> None:
        conn = self._db()
        try:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO acp_pending (session_id, kind, payload, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (session_id, kind, json.dumps(payload, ensure_ascii=False), _now()),
                )
        finally:
            conn.close()

    def _take_pending(self, session_id: str) -> tuple[str, dict[str, Any]] | None:
        conn = self._db()
        try:
            with conn:
                row = conn.execute(
                    "SELECT kind, payload FROM acp_pending WHERE session_id = ?", (session_id,)
                ).fetchone()
                conn.execute("DELETE FROM acp_pending WHERE session_id = ?", (session_id,))
        finally:
            conn.close()
        return (row["kind"], json.loads(row["payload"])) if row else None

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
        if text.startswith(CLIENT_AUTOMATED_PREFIXES):
            # Sent by the client, not typed by a person: never an answer to a pending question.
            self.say(
                session.session_id,
                "Tin nhắn này do PandaCode tự gửi (turn checks), không phải câu trả lời của bạn;"
                " Proofline bỏ qua. Gate thật chạy bằng `/verify`. Khi dùng Proofline, tắt"
                " Settings → Agents → turn checks.",
            )
            return {"stopReason": "end_turn"}
        pending = self._take_pending(session.session_id)
        command, _, rest = text.partition(" ")
        rest = rest.strip()
        handlers = {
            "init": lambda: self.cmd_init(session, rest),
            "assess": lambda: self.cmd_assess(session),
            "status": lambda: self.cmd_status(session),
            "spec": lambda: self.cmd_spec(session, rest),
            "approve": lambda: self.cmd_approve(session, rest),
            "reject": lambda: self.cmd_reject(session, rest),
            "verify": lambda: self.cmd_verify(session, rest),
            "help": lambda: self.cmd_help(session),
        }
        name = command.removeprefix("/")
        if command.startswith("/") or name in handlers:
            handler = handlers.get(name)
            if handler is None:
                self.say(session.session_id, f"Không có lệnh `{command}`. Gõ `/help`.")
            else:
                handler()
        elif pending is not None:
            self.answer_pending(session, pending, text)
        else:
            self.say(
                session.session_id,
                "Proofline nhận lệnh bắt đầu bằng `/`. Gõ `/help` để xem các lệnh."
                " (Nhận việc bằng lời và viết spec qua đây là bước tiếp theo, docs/07.)",
            )
        stop = "cancelled" if session.session_id in self.cancelled else "end_turn"
        return {"stopReason": stop}

    # ---- decisions ----

    def ask(
        self, session: Session, title: str, content: list[dict[str, Any]], options: list[dict]
    ) -> Decision:
        """Một quyết định của người qua `session/request_permission`, đi qua Decision Guard."""
        call_id = f"decision-{uuid.uuid4().hex[:8]}"
        self.notify(
            session.session_id,
            {
                "sessionUpdate": "tool_call",
                "toolCallId": call_id,
                "title": title,
                "kind": "other",
                "status": "pending",
                "content": content,
            },
        )
        response, seconds = self.request(
            "session/request_permission",
            {
                "sessionId": session.session_id,
                "toolCall": {
                    "toolCallId": call_id,
                    "title": title,
                    "kind": "other",
                    "status": "pending",
                },
                "options": options,
            },
        )
        outcome = ((response or {}).get("result") or {}).get("outcome") or {}
        option = outcome.get("optionId") if outcome.get("outcome") == "selected" else None
        kinds = {o["optionId"]: o["kind"] for o in options}
        decision = Decision(option, seconds)
        if (
            option is not None
            and kinds.get(option, "").startswith("allow")
            and seconds < self.guard_seconds
        ):
            decision.suspected_auto = True
        status = "completed" if decision.allowed else "failed"
        self.notify(
            session.session_id,
            {"sessionUpdate": "tool_call_update", "toolCallId": call_id, "status": status},
        )
        return decision

    def _guard_refused(
        self, session: Session, decision_type: str, decision: Decision, text_command: str
    ) -> None:
        record_decision(
            self.db_path,
            decision_type,
            decided_by="agent",
            agent_choice="client_auto_suspected",
            agent_rationale=(
                f"{decision.option} sau {decision.seconds:.2f}s (< {self.guard_seconds:.1f}s)"
            ),
        )
        self.say(
            session.session_id,
            f'⚠️ Lựa chọn "{decision.option}" đến sau {decision.seconds:.2f} giây, nhanh hơn mức'
            f" một người đọc kịp ({self.guard_seconds:.0f} giây). Có thể client đã tự chọn hộ"
            " (chế độ Full access). **Chưa áp dụng.** Nếu đúng là bạn quyết định, gõ"
            f" `{text_command}`.",
        )

    # ---- specs ----

    def _specs(self, target: TargetRepo, cwd: Path) -> list[tuple[Path, SpecManifest]]:
        folder = target.artifacts_dir(cwd, self.config.workspace) / "specs"
        found = []
        for path in sorted(folder.glob("*.yaml")) if folder.is_dir() else []:
            try:
                found.append((path, load_manifest(path)))
            except ConfigError:
                continue
        return found

    def _pick_spec(
        self, session: Session, target: TargetRepo, args: str, *, unapproved_only: bool = False
    ) -> tuple[Path, SpecManifest, str] | None:
        """Chọn spec theo tên ở đầu `args` (phần còn lại trả về), hoặc spec duy nhất phù hợp."""
        specs = self._specs(target, session.cwd)
        first, _, remainder = args.partition(" ")
        for path, manifest in specs:
            if first and manifest.name == first:
                return path, manifest, remainder.strip()
        candidates = specs
        if unapproved_only:
            candidates = [
                (p, m) for p, m in specs if self._journey_blob(session, m) != m.approved_blob
            ]
        if len(candidates) == 1:
            return candidates[0][0], candidates[0][1], args
        names = ", ".join(f"`{m.name}`" for _, m in (candidates or specs)) or "(chưa có spec)"
        self.say(session.session_id, f"Chọn spec: {names}. Vd `/spec <tên>`.")
        return None

    def _journey_blob(self, session: Session, manifest: SpecManifest) -> str | None:
        try:
            return blob_of(session.cwd, manifest.journey)
        except ConfigError:
            return None

    def _approve(
        self,
        session: Session,
        path: Path,
        manifest: SpecManifest,
        blob: str,
        note: str,
        channel: str,
    ) -> str:
        manifest.journey_approvals.append(
            JourneyApproval(blob=blob, approved_at=_now(), note=note or f"qua ACP ({channel})")
        )
        save_manifest(path, manifest)
        decision_id = record_decision(
            self.db_path,
            "acceptance_approval",
            decided_by="human",
            human_choice=f"approved {manifest.name} {blob[:7]}",
            human_reason=" ".join(filter(None, [note, f"[{channel}]"])),
        )
        self.say(session.session_id, f"✅ Đã duyệt journey `{manifest.journey}` ({blob[:7]}).")
        return decision_id

    def _reject(
        self, session: Session, manifest: SpecManifest, blob: str, reason: str, channel: str
    ) -> str:
        decision_id = record_decision(
            self.db_path,
            "acceptance_approval",
            decided_by="human",
            human_choice=f"rejected {manifest.name} {blob[:7]}",
            human_reason=" ".join(filter(None, [reason, f"[{channel}]"])),
        )
        return decision_id

    def cmd_spec(self, session: Session, args: str) -> None:
        target = self._require_repo(session)
        if target is None:
            return
        picked = self._pick_spec(session, target, args)
        if picked is None:
            return
        path, manifest, _ = picked
        sid = session.session_id
        blob = self._journey_blob(session, manifest)
        history = ", ".join(
            f"{a.blob[:7]} ({a.approved_at[:10]})" for a in manifest.journey_approvals
        )
        lines = [
            f"**Spec `{manifest.name}`** ({manifest.task_type}, base `{manifest.base}`)",
            f"- Tài liệu: `{manifest.spec or '-'}` · journey: `{manifest.journey}`",
            f"- Các lần duyệt: {history or 'chưa có'}",
        ]
        if blob is None:
            self.say(
                sid, "\n".join(lines + [f"- ❌ Không thấy file journey `{manifest.journey}`."])
            )
            return
        if blob == manifest.approved_blob:
            self.say(
                sid, "\n".join(lines + [f"- ✅ Journey hiện tại đúng bản đã duyệt ({blob[:7]})."])
            )
            return
        lines.append(f"- Journey hiện tại ({blob[:7]}) **chưa được duyệt**. Gửi yêu cầu duyệt…")
        self.say(sid, "\n".join(lines))
        new_text = (session.cwd / manifest.journey).read_text(encoding="utf-8", errors="replace")
        old_text = _blob_text(session.cwd, manifest.approved_blob)
        content = [
            {
                "type": "content",
                "content": {
                    "type": "text",
                    "text": (
                        "Duyệt acceptance journey này làm tiêu chí nghiệm thu của task. Sau khi"
                        " duyệt, agent không được sửa journey; muốn đổi phải xin duyệt lại."
                    ),
                },
            },
            {
                "type": "diff",
                "path": str(session.cwd / manifest.journey),
                "oldText": old_text,
                "newText": new_text,
            },
        ]
        decision = self.ask(session, f"Duyệt journey: {manifest.name}", content, APPROVE_OPTIONS)
        if decision.option is None:
            self.say(
                sid,
                "Chưa có quyết định (đã huỷ). Gõ `/approve` hoặc `/reject <lý do>` khi sẵn sàng.",
            )
        elif decision.suspected_auto:
            self._guard_refused(
                session, "acceptance_approval", decision, f"/approve {manifest.name}"
            )
        elif decision.option == "approve":
            self._approve(session, path, manifest, blob, "", "thanh duyệt")
        elif decision.option == "approve_note":
            decision_id = self._approve(session, path, manifest, blob, "", "thanh duyệt")
            self._set_pending(sid, "approval_note", {"decision_id": decision_id})
            self.say(sid, "Góp ý của bạn là gì? Trả lời ở tin nhắn tiếp theo.")
        else:
            decision_id = self._reject(session, manifest, blob, "", "thanh duyệt")
            self._set_pending(sid, "reject_reason", {"decision_id": decision_id})
            self.say(
                sid,
                "Đã ghi: từ chối journey. Vì sao? (vd sai hướng, quá rộng, sai mục tiêu người dùng)"
                " Trả lời ở tin nhắn tiếp theo.",
            )

    def cmd_approve(self, session: Session, args: str) -> None:
        target = self._require_repo(session)
        if target is None:
            return
        picked = self._pick_spec(session, target, args, unapproved_only=True)
        if picked is None:
            return
        path, manifest, note = picked
        blob = self._journey_blob(session, manifest)
        if blob is None:
            self.say(session.session_id, f"❌ Không thấy file journey `{manifest.journey}`.")
        elif blob == manifest.approved_blob:
            self.say(
                session.session_id,
                f"Journey `{manifest.name}` đã được duyệt ở bản này ({blob[:7]}).",
            )
        else:
            self._approve(session, path, manifest, blob, note, "lệnh /approve")

    def cmd_reject(self, session: Session, args: str) -> None:
        target = self._require_repo(session)
        if target is None:
            return
        picked = self._pick_spec(session, target, args, unapproved_only=True)
        if picked is None:
            return
        _, manifest, reason = picked
        blob = self._journey_blob(session, manifest) or "-"
        decision_id = self._reject(session, manifest, blob, reason, "lệnh /reject")
        if reason:
            self.say(
                session.session_id, f"Đã ghi: từ chối journey `{manifest.name}`. Lý do: {reason}"
            )
        else:
            self._set_pending(session.session_id, "reject_reason", {"decision_id": decision_id})
            self.say(session.session_id, "Đã ghi: từ chối. Vì sao? Trả lời ở tin nhắn tiếp theo.")

    def answer_pending(
        self, session: Session, pending: tuple[str, dict[str, Any]], text: str
    ) -> None:
        kind, payload = pending
        append_decision_reason(self.db_path, payload["decision_id"], text)
        label = "góp ý" if kind == "approval_note" else "lý do từ chối"
        self.say(session.session_id, f"Đã ghi {label}: {text}")

    # ---- verify ----

    def cmd_verify(self, session: Session, args: str) -> None:
        sid = session.session_id
        if session.mode == "plan":
            self.say(sid, "Chế độ Plan chỉ đọc: `/verify` chạy lệnh của repo nên không chạy ở đây.")
            return
        target = self._require_repo(session)
        if target is None:
            return
        tokens = shlex.split(args) if args else []
        runs, install, confirmed, names = 2, True, False, []
        while tokens:
            token = tokens.pop(0)
            if token == "--runs" and tokens:
                runs = max(1, min(5, int(tokens.pop(0))))
            elif token == "--no-install":
                install = False
            elif token == "--yes":
                confirmed = True
            else:
                names.append(token)
        picked = self._pick_spec(session, target, " ".join(names))
        if picked is None:
            return
        _, manifest, _ = picked
        if not confirmed:
            content = [
                {
                    "type": "content",
                    "content": {
                        "type": "text",
                        "text": (
                            f"Chạy các gate của `{manifest.name}` trên `{session.cwd}` so với"
                            f" `{manifest.base}`: cài đặt và chạy cả bộ test của repo"
                            f" {runs} lượt mỗi bên, typecheck, lint. Lệnh của repo chạy"
                            " **trực tiếp trên máy này** (chưa có sandbox), có thể mất nhiều phút."
                        ),
                    },
                }
            ]
            decision = self.ask(session, f"Chạy verify: {manifest.name}", content, RUN_OPTIONS)
            if decision.suspected_auto:
                self._guard_refused(
                    session, "run_repo_commands", decision, f"/verify {manifest.name} --yes"
                )
                return
            if not decision.allowed:
                self.say(sid, "Không chạy verify.")
                return
            if decision.option == "run_quick":
                runs, install = 1, False
            record_decision(
                self.db_path,
                "run_repo_commands",
                decided_by="human",
                human_choice=f"{decision.option} verify {manifest.name}",
                human_reason=f"[thanh duyệt, {decision.seconds:.1f}s]",
            )
        self._run_verify(session, target, manifest, runs, install)

    def _run_verify(
        self, session: Session, target: TargetRepo, manifest: SpecManifest, runs: int, install: bool
    ) -> None:
        sid = session.session_id
        state = {"call": None}

        def log(message: str) -> None:
            if state["call"]:
                self.notify(
                    sid,
                    {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": state["call"],
                        "status": "completed",
                    },
                )
            state["call"] = f"verify-step-{uuid.uuid4().hex[:8]}"
            self.notify(
                sid,
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": state["call"],
                    "title": message,
                    "kind": "execute",
                    "status": "in_progress",
                },
            )

        ctx = VerifyContext(
            config=self.config,
            target=target,
            candidate=session.cwd,
            manifest=manifest,
            runs=runs,
            install=install,
            log=log,
            base_cache=knowledge.base_test_cache(self.config, target.name),
        )
        try:
            report = verify(ctx)
        except ConfigError as exc:
            if state["call"]:
                self.notify(
                    sid,
                    {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": state["call"],
                        "status": "failed",
                    },
                )
            self.say(sid, f"❌ Không chạy được verify: {exc}")
            return
        if state["call"]:
            self.notify(
                sid,
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": state["call"],
                    "status": "completed",
                },
            )
        for gate in report.gates:
            self.notify(
                sid,
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": f"gate-{uuid.uuid4().hex[:8]}",
                    "title": f"Gate: {gate.key}",
                    "kind": "execute",
                    "status": TOOL_STATUS[gate.status],
                    "content": [
                        {
                            "type": "content",
                            "content": {
                                "type": "text",
                                "text": f"{STATUS_ICON[gate.status]} {gate.detail}",
                            },
                        }
                    ],
                },
            )
        md, js = write_report(
            report, target.artifacts_dir(session.cwd, self.config.workspace) / "verify"
        )
        changes = knowledge.record_verify(self.config, target, report, js)
        text = report.to_markdown() + f"\nBáo cáo: `{md}`"
        if changes:
            text += "\n\n**Kho tri thức** (sự thật đo được về base):\n" + "\n".join(
                f"- {line}" for line in knowledge.describe_changes(changes)
            )
        self.say(sid, text)

    # ---- init ----

    def cmd_init(self, session: Session, args: str) -> None:
        """Đăng ký thư mục của phiên làm repo đích: chỉ ghi `targets/<tên>/target.yaml` trong
        workspace. Thư mục trống thì hỏi `git init` trước (việc duy nhất chạm vào thư mục)."""
        sid = session.session_id
        words = args.split()
        typed_git_init = "--git-init" in words
        names = [w for w in words if not w.startswith("--")]
        existing = self._resolve_target(session)
        if existing is not None:
            self.say(
                sid,
                f"Thư mục này đã là repo đích `{existing.name}` của workspace. Dùng `/assess`"
                " hoặc `/status`.",
            )
            return
        branch = None
        try:
            root = Path(git(["rev-parse", "--show-toplevel"], session.cwd)).resolve()
        except ConfigError:
            root = None
        if root is None:
            entries = (
                [p for p in session.cwd.iterdir() if p.name not in IGNORABLE_FILES]
                if session.cwd.is_dir()
                else None
            )
            if entries is None:
                self.say(sid, f"❌ Không thấy thư mục `{session.cwd}`.")
                return
            if entries:
                self.say(
                    sid,
                    f"Thư mục `{session.cwd}` chưa phải repo git và đã có file. Proofline không tự"
                    " khởi tạo git trên thư mục đang có nội dung của bạn: chạy `git init` (và"
                    " commit nếu muốn) rồi gõ lại `/init`.",
                )
                return
            branch = self._git_init(session, typed=typed_git_init)
            if branch is None:
                return
            root = session.cwd
        self._register(session, root, names[0] if names else None, branch)

    def _git_init(self, session: Session, *, typed: bool = False) -> str | None:
        """Hỏi rồi `git init` thư mục trống; trả về tên nhánh, None nếu không khởi tạo.
        `typed`: người đã gõ `/init --git-init` nên không cần hỏi lại."""
        sid = session.session_id
        if session.mode == "plan":
            self.say(sid, "Chế độ Plan chỉ đọc: không khởi tạo git ở đây. Chuyển sang Standard.")
            return None
        if typed:
            return self._do_git_init(session, "git_init", "[lệnh /init --git-init]")
        content = [
            {
                "type": "content",
                "content": {
                    "type": "text",
                    "text": (
                        f"`{session.cwd}` đang trống và chưa phải repo git. Proofline làm việc"
                        " trên git (nhánh, worktree, so với base) nên cần `git init` ở đây. Đây"
                        " là luồng khởi đầu từ ý tưởng (mức R0). Không tạo file nào khác."
                    ),
                },
            }
        ]
        decision = self.ask(session, "Khởi tạo git cho project mới", content, GIT_INIT_OPTIONS)
        if decision.suspected_auto:
            self._guard_refused(session, "git_init", decision, "/init --git-init")
            return None
        if not decision.allowed:
            self.say(sid, "Không khởi tạo git; chưa đăng ký repo.")
            return None
        return self._do_git_init(
            session, decision.option, f"[thanh duyệt, {decision.seconds:.1f}s]"
        )

    def _do_git_init(self, session: Session, option: str, reason: str) -> str | None:
        sid = session.session_id
        record_decision(
            self.db_path,
            "git_init",
            decided_by="human",
            human_choice=f"{option} {session.cwd}",
            human_reason=reason,
        )
        try:
            git(["init", "-q", "-b", "main"], session.cwd)
        except ConfigError as exc:
            self.say(sid, f"❌ {exc}")
            return None
        lines = [f"✅ Đã `git init` (nhánh `main`) ở `{session.cwd}`."]
        if option == "git_init_commit":
            try:
                git(["commit", "-q", "--allow-empty", "-m", "Khởi tạo project"], session.cwd)
                lines.append("✅ Đã tạo commit rỗng đầu tiên.")
            except ConfigError as exc:
                lines.append(
                    f"⚠️ Chưa tạo được commit đầu tiên ({exc}). Thường do git chưa có"
                    " `user.name`/`user.email`; repo vẫn dùng được."
                )
        self.say(sid, "\n".join(lines))
        return "main"

    def _register(self, session: Session, root: Path, name: str | None, branch: str | None) -> None:
        sid = session.session_id
        try:
            added = add_target(self.config, str(root), name=name, branch=branch)
        except ConfigError as exc:
            self.say(sid, f"❌ Chưa đăng ký được: {exc}. Đặt tên khác bằng `/init <tên>`.")
            return
        record_decision(
            self.db_path,
            "register_target",
            decided_by="human",
            human_choice=f"init {added.name} {root}",
            human_reason="[lệnh /init]",
        )
        self._config = None  # đọc lại workspace để thấy repo vừa thêm
        session.repo = added.name
        self._save(session)
        detection = added.detection
        lines = [
            f"✅ Đã đăng ký repo đích `{added.name}` (stack {detection.stack}).",
            f"- Cấu hình: `{added.target_file}` (trong workspace; repo của bạn không bị sửa).",
        ]
        if detection.commands:
            lines.append("- Lệnh dò được: " + ", ".join(f"`{k}`" for k in detection.commands))
        lines += [f"- Lưu ý: {note}" for note in detection.notes]
        lines.append("Hãy xem lại file cấu hình trước khi chạy lệnh của repo. Giờ đo mức sẵn sàng:")
        self.say(sid, "\n".join(lines))
        self.cmd_assess(session)

    # ---- read-only commands ----

    def _resolve_target(self, session: Session) -> TargetRepo | None:
        if session.repo:
            try:
                return self.config.target_repo(session.repo)
            except ConfigError:
                pass
        # The repo may have been added after the session started (CLI, another session).
        target = self.target_for(session.cwd)
        if target is not None:
            session.repo = target.name
            self._save(session)
        return target

    def _require_repo(self, session: Session) -> TargetRepo | None:
        target = self._resolve_target(session)
        if target is None:
            self.say(
                session.session_id,
                f"Thư mục `{session.cwd}` chưa thuộc repo đích nào của workspace. Gõ `/init` để"
                " đăng ký (chỉ ghi vào workspace).",
            )
        return target

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
        save_snapshot(self.db_path, result)
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
        conn = connect(self.db_path)
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
        specs = self._specs(target, session.cwd)
        if not specs:
            sources = find_source_feature_docs(session.cwd)
            lines.append(
                "- Chưa có spec nào."
                + (f" Tài liệu tính năng có sẵn: {', '.join(sources)}." if sources else "")
            )
        for _, manifest in specs:
            blob = self._journey_blob(session, manifest)
            if not manifest.approved_blob:
                approved = "journey **chưa duyệt**"
            elif blob != manifest.approved_blob:
                approved = f"journey **đã đổi sau lần duyệt** ({manifest.approved_blob[:7]})"
            else:
                approved = f"journey đã duyệt ({manifest.approved_blob[:7]})"
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
        lines += self._facts_lines(target)
        self.say(sid, "\n".join(lines))

    def _facts_lines(self, target: TargetRepo) -> list[str]:
        facts = knowledge.current_facts(self.config, target).facts
        active = [f for f in facts if f.status == "active"]
        stale = len(facts) - len(active)
        if not facts:
            return ["- Kho tri thức: chưa có sự thật đo được (chạy `/verify`)."]
        lines = [
            f"- Kho tri thức: {len(active)} sự thật đang dùng"
            + (f", {stale} đã hết hạn/rút (`proofline knowledge show --all`)" if stale else "")
        ]
        lines += [f"  - {f.statement}" for f in active[:MAX_FACTS_IN_STATUS]]
        if len(active) > MAX_FACTS_IN_STATUS:
            lines.append(f"  - … và {len(active) - MAX_FACTS_IN_STATUS} sự thật khác")
        return lines


def serve_stdio(config_path: Path | None) -> None:
    """Điểm vào của `proofline acp`: stdin/stdout UTF-8, xuống dòng `\\n` (kể cả trên Windows)."""
    sys.stdin.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[attr-defined]
    AcpServer(sys.stdin, sys.stdout, lambda: load_config(config_path)).serve()
