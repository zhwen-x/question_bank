"""强制分层约束。

这是需求 7（后续拓展到 Web 端）唯一的技术保障：只要 ``core`` 和 ``exchange``
里没有 UI/Web 依赖，换一个前端或换一个部署形态就不需要动业务代码。
人靠自觉守不住这条线，所以用测试守。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import qbank

SRC = Path(qbank.__file__).parent

#: 包 -> 禁止出现的导入前缀
FORBIDDEN_INTERNAL: dict[str, tuple[str, ...]] = {
    "core": ("qbank.exchange", "qbank.web", "qbank.desktop", "qbank.cli"),
    "exchange": ("qbank.web", "qbank.desktop", "qbank.cli"),
}

#: 包 -> 禁止出现的第三方库（UI / Web 框架）
FORBIDDEN_THIRD_PARTY: dict[str, tuple[str, ...]] = {
    "core": ("fastapi", "starlette", "uvicorn", "webview", "typer", "click", "pypandoc"),
    "exchange": ("fastapi", "starlette", "uvicorn", "webview", "typer", "click"),
}


def _modules(package: str) -> list[Path]:
    return sorted((SRC / package).rglob("*.py"))


def _imported_names(path: Path) -> set[str]:
    """收集模块中所有被导入的顶层名字（含相对导入还原后的绝对名）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    package_parts = path.relative_to(SRC.parent).with_suffix("").parts
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # 相对导入：还原成绝对模块名
                base = package_parts[: len(package_parts) - node.level]
                names.add(".".join((*base, node.module or "")).rstrip("."))
            elif node.module:
                names.add(node.module)
    return names


@pytest.mark.parametrize("package", sorted(FORBIDDEN_INTERNAL))
def test_no_forbidden_imports(package: str) -> None:
    violations: list[str] = []
    for path in _modules(package):
        forbidden = FORBIDDEN_INTERNAL[package] + FORBIDDEN_THIRD_PARTY[package]
        for name in _imported_names(path):
            hit = next((f for f in forbidden if name == f or name.startswith(f + ".")), None)
            if hit:
                violations.append(f"{path.relative_to(SRC)} 导入了 {name}")
    assert not violations, "分层被破坏：\n  " + "\n  ".join(violations)


def test_packages_are_non_empty() -> None:
    """防止某次重构把包整个删空却没人注意到。"""
    for package in FORBIDDEN_INTERNAL:
        assert _modules(package), f"qbank.{package} 下没有任何模块"


def test_core_is_importable_without_optional_deps() -> None:
    """``core`` 必须在只装了核心依赖的环境里能 import。"""
    import importlib

    for module in ("qbank.config", "qbank.core.content.segment"):
        importlib.import_module(module)


def test_builtin_templates_live_inside_the_package() -> None:
    """内置导出模板必须在 ``qbank`` 包目录里，否则打进 wheel 的时候会漏掉。

    漏掉的症状是安装后一导出就报「找不到模板 standard」——而开发机上一切正常，
    因为源码树里它们就在那儿。
    """
    from qbank.exchange.exporters.templates import BUILTIN_DIR, discover

    assert BUILTIN_DIR.is_dir(), f"内置模板目录不存在：{BUILTIN_DIR}"
    assert SRC in BUILTIN_DIR.parents, f"{BUILTIN_DIR} 不在 qbank 包内，打包会漏"
    assert sorted(key for key, item in discover().items() if item.builtin) == [
        "compact",
        "exam",
        "standard",
    ]
