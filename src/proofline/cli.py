"""Lệnh `proofline`."""

from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import Annotated, TextIO, cast

import typer

from proofline import __version__
from proofline.assess import AssessContext, Assessment, assess, save_snapshot
from proofline.autonomy import effective_level
from proofline.config import ConfigError, ProoflineConfig, load_config, resolve_model
from proofline.levels import READINESS_LEVELS, ReadinessLevel
from proofline.state import SchemaVersionError, init_db, table_names

app = typer.Typer(help="Proofline: xưởng phần mềm nơi mọi thay đổi đều có bằng chứng.")
config_app = typer.Typer(help="Kiểm tra và tra cứu config.")
autonomy_app = typer.Typer(help="Xem mức tự chủ hiệu lực.")
db_app = typer.Typer(help="Quản lý state/proofline.db.")
app.add_typer(config_app, name="config")
app.add_typer(autonomy_app, name="autonomy")
app.add_typer(db_app, name="db")

ConfigOption = Annotated[
    Path | None,
    typer.Option(
        "--config",
        "-c",
        help="Đường dẫn config (mặc định: $PROOFLINE_CONFIG hoặc config/proofline.yaml).",
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
    db_path = path if path is not None else Path(_load(config).factory.state_db)
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
    cfg = _load(config)
    try:
        target = cfg.target_repo(repo)
    except ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    workspace = Path.cwd() / cfg.factory.workspace_root
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
