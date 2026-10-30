"""qbank 命令行入口。

CLI 不只是给人用的——它同时是集成测试的驱动入口，M3 的图形界面上线前
全链路（建课 → 导入 → 检索 → 组卷 → 导出）都靠它跑通。
"""

from __future__ import annotations

import typer

from qbank import __version__
from qbank.doctor import Level, Status, run_checks

app = typer.Typer(
    name="qbank",
    help="题库管理系统：多课程题目管理、批量导入、自动组卷与多格式导出。",
    no_args_is_help=True,
    add_completion=False,
)

_STATUS_MARK = {Status.OK: "✓", Status.DEGRADED: "!", Status.MISSING: "✗"}
_STATUS_COLOR = {
    Status.OK: typer.colors.GREEN,
    Status.DEGRADED: typer.colors.YELLOW,
    Status.MISSING: typer.colors.RED,
}


@app.command()
def version() -> None:
    """打印版本号。"""
    typer.echo(f"qbank {__version__}")


@app.command()
def doctor() -> None:
    """环境自检：探测存储、中文分词、pandoc、PDF、Web 各项能力。

    必需项缺失时退出码为 1；可选项缺失只提示降级路径，退出码仍为 0，
    以便 CI 和未装可选依赖的用户都能正常通过。
    """
    blocking: list[str] = []
    degraded: list[str] = []

    for check in run_checks():
        mark = _STATUS_MARK[check.status]
        color = _STATUS_COLOR[check.status]
        tag = "必需" if check.level is Level.REQUIRED else "可选"
        typer.secho(f"{mark} [{tag}] {check.name}: {check.detail}", fg=color)
        if check.remedy:
            typer.secho(f"    → {check.remedy}", fg=typer.colors.BRIGHT_BLACK)
        if check.is_blocking:
            blocking.append(check.name)
        elif check.status is not Status.OK:
            degraded.append(check.name)

    typer.echo("")
    if blocking:
        typer.secho(f"存在阻断性问题：{', '.join(blocking)}", fg=typer.colors.RED, bold=True)
        raise typer.Exit(code=1)
    if degraded:
        typer.secho(
            f"核心功能可用；以下能力处于降级状态：{', '.join(degraded)}",
            fg=typer.colors.YELLOW,
        )
    else:
        typer.secho("全部能力可用。", fg=typer.colors.GREEN, bold=True)


if __name__ == "__main__":  # pragma: no cover
    app()
