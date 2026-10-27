"""Lệnh `proofline`."""

from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import Annotated, TextIO, cast

import typer

from proofline import __version__, knowledge
from proofline.assess import AssessContext, Assessment, assess, save_snapshot
from proofline.autonomy import effective_level
from proofline.config import (
    CONFIG_FILENAME,
    ConfigError,
    ProoflineConfig,
    load_config,
    resolve_model,
)
from proofline.levels import READINESS_LEVELS, ReadinessLevel
from proofline.state import SchemaVersionError, init_db, record_human_decision, table_names
from proofline.targets import AddedTarget, add_target, is_git_url
from proofline.verify import (
    JourneyApproval,
    VerifyContext,
    blob_of,
    chore_manifest,
    git,
    load_manifest,
    repo_root,
    save_manifest,
    verify,
    write_report,
)
from proofline.workspace import WorkspaceExistsError, init_workspace

app = typer.Typer(help="Proofline: xưởng phần mềm nơi mọi thay đổi đều có bằng chứng.")
config_app = typer.Typer(help="Kiểm tra và tra cứu config.")
autonomy_app = typer.Typer(help="Xem mức tự chủ hiệu lực.")
db_app = typer.Typer(help="Quản lý state/proofline.db.")
workspace_app = typer.Typer(help="Tạo workspace (config, state, playbook) tách khỏi công cụ.")
target_app = typer.Typer(help="Thêm và xem repo đích.")
knowledge_app = typer.Typer(help="Kho tri thức theo domain (docs/08).")
spec_app = typer.Typer(help="Spec và acceptance journey của một task.")
app.add_typer(target_app, name="target")
app.add_typer(spec_app, name="spec")
app.add_typer(knowledge_app, name="knowledge")
app.add_typer(config_app, name="config")
app.add_typer(autonomy_app, name="autonomy")
app.add_typer(db_app, name="db")
app.add_typer(workspace_app, name="workspace")

ConfigOption = Annotated[
    Path | None,
    typer.Option(
        "--config",
        "-c",
        help="File proofline.yaml (mặc định: $PROOFLINE_CONFIG hoặc ./proofline.yaml).",
    ),
]


def ensure_utf8(stream: TextIO) -> None:
    """Chuyển stream sang UTF-8; console Windows (cp1252) không in được tiếng Việt."""
    encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
    if encoding != "utf8" and isinstance(stream, io.TextIOWrapper):
        stream.reconfigure(encoding="utf-8", errors="replace")


def run() -> None:
    """Điểm vào của lệnh `proofline` (khai báo trong pyproject.toml)."""
    ensure_utf8(sys.stdout)
    ensure_utf8(sys.stderr)
    app()


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"proofline {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="In phiên bản."),
    ] = False,
) -> None:
    """Proofline CLI."""


def _load(config: Path | None) -> ProoflineConfig:
    try:
        return load_config(config)
    except ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


@workspace_app.command("init")
def workspace_init(
    path: Annotated[Path, typer.Argument(help="Thư mục workspace (tạo mới nếu chưa có).")] = Path(
        "."
    ),
) -> None:
    """Tạo workspace từ bản mẫu: proofline.yaml, playbooks/, state/, targets/, evals/."""
    try:
        result = init_workspace(path)
    except WorkspaceExistsError as exc:
        typer.secho(f"{exc}; không ghi đè workspace có sẵn.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.secho(f"Đã tạo workspace: {path.resolve()}", fg=typer.colors.GREEN)
    for rel in result.created:
        typer.echo(f"  + {rel}")
    for rel in result.kept:
        typer.echo(f"  = {rel} (đã có, giữ nguyên)")
    typer.echo(
        "\nTiếp theo: khai báo repo đích trong target_repos của proofline.yaml,"
        " rồi chạy `proofline assess <tên repo>` trong thư mục workspace."
    )


@config_app.command("check")
def config_check(config: ConfigOption = None) -> None:
    """Kiểm tra config hợp lệ và in tóm tắt."""
    cfg = _load(config)
    typer.secho(f"Config hợp lệ (version {cfg.version}).", fg=typer.colors.GREEN)
    typer.echo(f"Repo đích: {', '.join(r.name for r in cfg.target_repos) or '(chưa có)'}")
    typer.echo(f"Vai trò model: {', '.join(cfg.models.roles)}")
    typer.echo(f"Loại quyết định: {len(cfg.autonomy.decisions)}")
    if cfg.autonomy.kill_switch:
        typer.secho("Cảnh báo: kill switch đang bật.", fg=typer.colors.YELLOW)


@config_app.command("model")
def config_model(
    role: Annotated[str, typer.Argument(help="Tên vai trò, vd: heavy_coding.")],
    repo: Annotated[str | None, typer.Option(help="Tên repo đích để áp override.")] = None,
    config: ConfigOption = None,
) -> None:
    """In model sẽ dùng cho một vai trò, kèm nguồn quyết định (env/repo/default)."""
    cfg = _load(config)
    try:
        resolved = resolve_model(cfg, role, repo=repo)
    except ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    fallback = ", ".join(resolved.fallback) or "-"
    typer.echo(
        f"{resolved.role}: {resolved.provider}/{resolved.model} "
        f"(nguồn: {resolved.source}; fallback: {fallback})"
    )


@autonomy_app.command("show")
def autonomy_show(
    readiness: Annotated[str, typer.Option(help="Mức sẵn sàng của repo (R0–R4).")] = "R0",
    config: ConfigOption = None,
) -> None:
    """Bảng mức tự chủ hiệu lực của mọi loại quyết định ở một mức sẵn sàng."""
    if readiness not in READINESS_LEVELS:
        typer.secho(f"Mức sẵn sàng không hợp lệ: {readiness}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    cfg = _load(config)
    width = max(len(name) for name in cfg.autonomy.decisions)
    for name in cfg.autonomy.decisions:
        result = effective_level(cfg.autonomy, name, readiness=cast(ReadinessLevel, readiness))
        typer.echo(f"{name:<{width}}  {result.level}  ({'; '.join(result.reasons)})")


@db_app.command("init")
def db_init(
    path: Annotated[
        Path | None, typer.Option(help="Đường dẫn database (mặc định theo config).")
    ] = None,
    config: ConfigOption = None,
) -> None:
    """Tạo state/proofline.db và áp schema (gọi lại nhiều lần vẫn an toàn)."""
    if path is None:
        cfg = _load(config)
        path = cfg.workspace / cfg.factory.state_db
    db_path = path
    try:
        conn = init_db(db_path)
    except SchemaVersionError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    with conn:
        tables = table_names(conn)
    conn.close()
    typer.secho(
        f"Đã sẵn sàng: {db_path} ({len(tables)} bảng: {', '.join(tables)})", fg=typer.colors.GREEN
    )


STATUS_MARK = {"pass": "[PASS]", "fail": "[FAIL]", "unknown": "[ ?? ]"}
STATUS_COLOR = {
    "pass": typer.colors.GREEN,
    "fail": typer.colors.RED,
    "unknown": typer.colors.YELLOW,
}


@app.command("assess")
def assess_cmd(
    repo: Annotated[str, typer.Argument(help="Tên repo đích trong target_repos.")],
    run: Annotated[
        bool,
        typer.Option(
            "--run",
            help="Chạy lệnh của repo (build container, cài đặt, test, khởi chạy app) và hỏi CI.",
        ),
    ] = False,
    save: Annotated[
        bool, typer.Option("--save/--no-save", help="Lưu snapshot vào state DB.")
    ] = True,
    config: ConfigOption = None,
) -> None:
    """Đo mức sẵn sàng R0–R4 của repo đích và việc cần làm để lên mức tiếp theo."""
    _run_assess(_load(config), repo, run=run, save=save)


def _run_assess(cfg: ProoflineConfig, repo: str, *, run: bool, save: bool) -> None:
    try:
        target = cfg.target_repo(repo)
    except ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    workspace = cfg.workspace
    ctx = AssessContext(
        config=cfg,
        repo=target,
        repo_path=(workspace / target.local_path).resolve(),
        workspace=workspace,
        run_commands=run,
    )
    if run:
        typer.echo("Đang chạy lệnh của repo, có thể mất vài phút...")
    result = assess(ctx)
    _print_assessment(result)
    if save:
        db_path = workspace / cfg.factory.state_db
        snapshot_id = save_snapshot(db_path, result)
        typer.echo(f"\nĐã lưu snapshot {snapshot_id[:8]} vào {cfg.factory.state_db}")
        if run:
            changes = knowledge.record_assess(
                cfg, target, result, ctx.repo_path, evidence=f"readiness snapshot {snapshot_id}"
            )
            _print_knowledge_changes(cfg, target.name, changes)


def _print_assessment(result: Assessment) -> None:
    typer.secho(f"Repo {result.repo}: mức sẵn sàng {result.level}", bold=True)
    for level in READINESS_LEVELS[1:]:
        items = [c for c in result.criteria if c.level == level]
        if not items:
            continue
        typer.echo(f"\n{level}")
        for c in items:
            typer.secho(f"  {STATUS_MARK[c.status]} ", fg=STATUS_COLOR[c.status], nl=False)
            typer.echo(f"{c.key}: {c.detail}")
    target = result.next_level()
    missing = result.missing_for_next_level()
    if target and missing:
        typer.secho(f"\nĐể lên {target}:", bold=True)
        for c in missing:
            typer.echo(f"  - {c.action or c.detail}")


NameOption = Annotated[
    str | None, typer.Option("--name", help="Tên repo đích (mặc định: lấy từ URL/thư mục).")
]
BranchOption = Annotated[
    str | None, typer.Option("--branch", help="Nhánh chính (mặc định: nhánh mặc định của repo).")
]
SourceArgument = Annotated[str, typer.Argument(help="Git URL (https/ssh) hoặc đường dẫn repo.")]


def _add(cfg: ProoflineConfig, source: str, name: str | None, branch: str | None) -> AddedTarget:
    if is_git_url(source):
        typer.echo(f"Đang clone {source} ...")
    try:
        added = add_target(cfg, source, name=name, branch=branch)
    except ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    det = added.detection
    typer.secho(f"Đã thêm repo đích '{added.name}' ({det.stack})", fg=typer.colors.GREEN)
    typer.echo(f"  Repo:   {added.repo_path}{' (đã clone)' if added.cloned else ''}")
    typer.echo(f"  Config: {added.target_file}")
    for key, value in det.commands.items():
        typer.echo(f"  {key}: {value}")
    if det.app_runtime.get("enabled"):
        runtime = det.app_runtime
        typer.echo(f"  app: {runtime['start_command']} → {runtime['healthcheck_url']}")
    for note in det.notes:
        typer.secho(f"  Lưu ý: {note}", fg=typer.colors.YELLOW)
    typer.echo("Hãy kiểm tra file config trên trước khi chạy `proofline assess --run`.")
    return added


@target_app.command("add")
def target_add(
    source: SourceArgument,
    name: NameOption = None,
    branch: BranchOption = None,
    config: ConfigOption = None,
) -> None:
    """Thêm repo đích: clone nếu là URL, tự dò stack và lệnh, ghi targets/<tên>/target.yaml."""
    _add(_load(config), source, name, branch)


@target_app.command("list")
def target_list(config: ConfigOption = None) -> None:
    """Liệt kê repo đích trong workspace."""
    cfg = _load(config)
    if not cfg.target_repos:
        typer.echo("Chưa có repo đích. Thêm bằng `proofline target add <git-url>`.")
        return
    for repo in cfg.target_repos:
        path = (cfg.workspace / repo.local_path).resolve()
        typer.echo(f"{repo.name}  {path}  (nhánh {repo.default_branch})")


def _print_knowledge_changes(
    cfg: ProoflineConfig, repo: str, changes: list[knowledge.Change]
) -> None:
    if not changes:
        return
    path = knowledge.facts_path(cfg, repo)
    typer.secho(f"\nKho tri thức ({_relative(path, cfg.workspace)}):", bold=True)
    for line in knowledge.describe_changes(changes):
        typer.echo(f"  - {line}")


def _relative(path: Path, base: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return str(path)


FACT_MARK = {"active": "●", "expired": "○", "retired": "×"}


@knowledge_app.command("show")
def knowledge_show(
    repo: Annotated[str | None, typer.Argument(help="Tên repo đích (mặc định: tất cả).")] = None,
    all_: Annotated[
        bool, typer.Option("--all", help="Hiện cả sự thật đã hết hạn hoặc đã rút.")
    ] = False,
    config: ConfigOption = None,
) -> None:
    """Sự thật đo được về repo đích (từ verify và assess --run), kèm trạng thái hết hạn."""
    cfg = _load(config)
    try:
        targets = [cfg.target_repo(repo)] if repo else cfg.target_repos
    except ConfigError as exc:
        raise _fail(exc) from exc
    if not targets:
        typer.echo("Chưa có repo đích.")
        return
    for target in targets:
        facts = knowledge.current_facts(cfg, target)
        shown = [f for f in facts.facts if all_ or f.status == "active"]
        hidden = len(facts.facts) - len(shown)
        typer.secho(f"{target.name}: {len(shown)} sự thật", bold=True, nl=False)
        typer.echo(f" ({hidden} đã hết hạn/rút, xem bằng --all)" if hidden else "")
        if not facts.facts:
            typer.echo("  Chưa có. Chạy `proofline verify` hoặc `proofline assess --run`.")
        for fact in shown:
            typer.echo(f"  {FACT_MARK[fact.status]} {fact.statement}")
            typer.echo(
                f"    {fact.source} · {fact.scope.ref} @ {fact.scope.commit[:7]}"
                f" · đo {fact.measurements} lần, gần nhất {fact.last_measured}"
                + (f" · {fact.evidence}" if fact.evidence else "")
            )
            if fact.status != "active":
                typer.echo(f"    {fact.status}: {fact.status_reason}")


@app.command("start")
def start(
    source: SourceArgument,
    workspace: Annotated[
        Path, typer.Option("--workspace", "-w", help="Thư mục workspace (tạo nếu chưa có).")
    ] = Path("."),
    name: NameOption = None,
    branch: BranchOption = None,
    run: Annotated[
        bool, typer.Option("--run", help="Chạy luôn assess đầy đủ (cài đặt, test, khởi chạy app).")
    ] = False,
) -> None:
    """Bắt đầu nhanh: tạo workspace nếu cần, thêm repo đích từ git URL, rồi đo mức sẵn sàng."""
    config_path = workspace / CONFIG_FILENAME
    if not config_path.exists():
        init_workspace(workspace)
        typer.secho(f"Đã tạo workspace: {workspace.resolve()}", fg=typer.colors.GREEN)
    added = _add(_load(config_path), source, name, branch)
    typer.echo("")
    _run_assess(_load(config_path), added.name, run=run, save=True)
    typer.echo(
        f"\nLần sau, chạy các lệnh trong {workspace.resolve()}"
        f" (vd `proofline assess {added.name} --run`)."
    )


SpecArgument = Annotated[
    Path, typer.Argument(help="Manifest của spec, vd .proofline/specs/<tên>.yaml.")
]
PathOption = Annotated[
    Path | None,
    typer.Option("--path", help="Checkout của nhánh cần kiểm (mặc định: repo chứa manifest)."),
]


def _fail(exc: Exception) -> typer.Exit:
    typer.secho(str(exc), fg=typer.colors.RED, err=True)
    return typer.Exit(code=2)


def _target_for(cfg: ProoflineConfig, candidate: Path, name: str | None):
    """Repo đích của một checkout: theo --repo, hoặc cùng kho git (worktree cũng tính)."""
    if name:
        return cfg.target_repo(name)
    common = Path(
        git(["rev-parse", "--path-format=absolute", "--git-common-dir"], candidate)
    ).resolve()
    for target in cfg.target_repos:
        local = (cfg.workspace / target.local_path).resolve()
        if not local.is_dir():
            continue
        try:
            other = git(["rev-parse", "--path-format=absolute", "--git-common-dir"], local)
        except ConfigError:
            continue
        if Path(other).resolve() == common:
            return target
    if len(cfg.target_repos) == 1:
        return cfg.target_repos[0]
    raise ConfigError(f"không xác định được repo đích của {candidate}; dùng --repo")


@spec_app.command("approve")
def spec_approve(
    spec: SpecArgument,
    note: Annotated[str, typer.Option(help="Ghi chú của người duyệt.")] = "",
    path: PathOption = None,
    config: ConfigOption = None,
) -> None:
    """Ghi nhận người duyệt journey hiện tại: lưu git blob vào manifest và vào bảng decisions."""
    try:
        manifest = load_manifest(spec)
        candidate = repo_root(path or spec.parent)
        blob = blob_of(candidate, manifest.journey)
    except ConfigError as exc:
        raise _fail(exc) from exc
    if manifest.approved_blob == blob:
        typer.echo(f"Journey đã được duyệt ở bản này ({blob[:7]}).")
        return
    from datetime import UTC, datetime

    manifest.journey_approvals.append(
        JourneyApproval(
            blob=blob, approved_at=datetime.now(UTC).isoformat(timespec="seconds"), note=note
        )
    )
    save_manifest(spec, manifest)
    typer.secho(f"Đã duyệt journey {manifest.journey} ({blob[:7]}).", fg=typer.colors.GREEN)
    try:
        cfg = load_config(config)
    except ConfigError:
        typer.echo("(Không tìm thấy workspace: chưa ghi vào bảng decisions.)")
        return
    record_human_decision(
        cfg.workspace / cfg.factory.state_db,
        "acceptance_approval",
        f"approved {manifest.name} {blob[:7]}",
        note,
    )


@app.command("verify")
def verify_cmd(
    spec: Annotated[
        Path | None,
        typer.Argument(help="Manifest của spec, vd .proofline/specs/<tên>.yaml (trừ --chore)."),
    ] = None,
    chore: Annotated[
        bool,
        typer.Option(
            "--chore",
            help="Chế độ nhẹ (docs/09 mục 5): thay đổi nhỏ, không spec/journey; đi từ --path.",
        ),
    ] = False,
    base: Annotated[
        str | None, typer.Option(help="Nhánh base cho --chore (mặc định: default_branch).")
    ] = None,
    path: PathOption = None,
    repo: Annotated[
        str | None, typer.Option(help="Tên repo đích (mặc định: tự nhận theo kho git).")
    ] = None,
    runs: Annotated[
        int | None,
        typer.Option(
            min=1, max=5, help="Số lượt chạy test mỗi bên (mặc định 2; chore dùng lại base: 1)."
        ),
    ] = None,
    install: Annotated[
        bool, typer.Option("--install/--no-install", help="Chạy commands.install trước.")
    ] = True,
    config: ConfigOption = None,
) -> None:
    """Chạy các gate của một task trên nhánh, so với base; ghi báo cáo vào thư mục artifacts."""
    cfg = _load(config)
    if (spec is None) == (not chore):
        raise _fail(ConfigError("cần đúng một trong hai: file manifest của spec, hoặc --chore"))
    try:
        candidate = repo_root(path or (spec.parent if spec else Path.cwd()))
        target = _target_for(cfg, candidate, repo)
        if spec is not None:
            manifest = load_manifest(spec)
        else:
            manifest = chore_manifest(candidate, base or target.default_branch)
        cache = knowledge.base_test_cache(cfg, target.name)
        if runs is None:
            reuse = manifest.task_type == "chore" and cache(
                manifest.base, git(["rev-parse", f"{manifest.base}^{{commit}}"], candidate)
            )
            runs = 1 if reuse else 2
        ctx = VerifyContext(
            config=cfg,
            target=target,
            candidate=candidate,
            manifest=manifest,
            runs=runs,
            install=install,
            log=lambda msg: typer.echo(f"· {msg}"),
            base_cache=cache,
        )
        report = verify(ctx)
    except ConfigError as exc:
        raise _fail(exc) from exc
    typer.echo("")
    typer.echo(report.to_markdown())
    md, js = write_report(report, target.artifacts_dir(candidate, cfg.workspace) / "verify")
    typer.echo(f"Báo cáo: {md}\n        {js}")
    _print_knowledge_changes(cfg, target.name, knowledge.record_verify(cfg, target, report, js))
    if report.status == "fail":
        raise typer.Exit(code=1)


@app.command("acp")
def acp_cmd(
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Thư mục workspace (chứa proofline.yaml)."),
    ] = None,
    config: ConfigOption = None,
) -> None:
    """Chạy Proofline như một agent ACP qua stdio (cho PandaCode và các client ACP khác)."""
    from proofline.acp import serve_stdio

    serve_stdio(config or (workspace / CONFIG_FILENAME if workspace else None))
