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


# --------------------------------------------------- 打包（PyInstaller）
def test_spec_collects_the_data_files_that_are_read_at_runtime() -> None:
    """这些包在运行时按路径读自己的数据文件，spec 里漏一个就会在用到那一刻才炸。

    实测踩过：少了 ``latex2mathml`` 的符号表，Markdown 导出一切正常，一导 HTML
    就 FileNotFoundError——而构建过程一个警告都没有。
    """
    spec = (SRC.parent.parent / "packaging" / "qbank.spec").read_text("utf-8")
    for package in ("qbank", "pypandoc", "latex2mathml", "webview", "alembic"):
        # qbank 那一行带了 includes=，所以只比到左引号为止
        assert f'collect_data_files("{package}"' in spec, f"spec 没收 {package} 的数据文件"


def test_spec_bundles_migrations() -> None:
    """少了 migrations，打包版打开旧题库时不会升级表结构，而且一声不吭。"""
    spec = (SRC.parent.parent / "packaging" / "qbank.spec").read_text("utf-8")
    assert "migrations" in spec
    assert "alembic.ini" in spec


def test_frozen_paths_use_meipass() -> None:
    """打包后源码不在磁盘上，``__file__`` 推出来的路径指向一个不存在的目录。"""
    import inspect

    from qbank.app import _project_root

    assert "_MEIPASS" in inspect.getsource(_project_root)


# --------------------------------------------- 三处分层清单必须说同一件事
#: 分层清单写在三个地方：包文档、设计说明、README。
#:
#: 三处各写一份的下场在 M7 的文档审计里原样发生了：README 列了五个包，
#: 设计说明和 ``__init__.py`` 还停在四个——M6 新增的 ``qbank.desktop`` 哪边都没补。
#: 读者不知道该信哪份，而「分层」正是这个项目最该说清楚的东西。
_LAYER_SOURCES = {
    "qbank/__init__.py": SRC / "__init__.py",
    "docs/设计说明.md": SRC.parent.parent / "docs" / "设计说明.md",
    "README.md": SRC.parent.parent / "README.md",
}


def _declared_layers(path: Path) -> set[str]:
    """抽出文本里点名的 ``qbank.X`` 顶层包。"""
    import re

    text = path.read_text(encoding="utf-8")
    # README 与设计说明里还会在正文里顺带提到某个包，所以只认分层清单那一段：
    # 设计说明在围栏代码块里，README 在表格里，包文档在星号列表里——
    # 三者的共同点是「行首附近就是 qbank.X」
    return set(re.findall(r"^[\s|*`]*`{0,2}qbank\.(\w+)`{0,2}", text, re.M))


def test_the_three_layer_listings_agree() -> None:
    declared = {label: _declared_layers(path) for label, path in _LAYER_SOURCES.items()}
    for label, names in declared.items():
        assert len(names) >= 4, f"{label} 里只抽到 {names}，抽取逻辑可能失效了"

    reference = declared["qbank/__init__.py"]
    mismatched = {
        label: sorted(names ^ reference) for label, names in declared.items() if names != reference
    }
    assert not mismatched, "三处分层清单对不上（以包文档为准）：\n  " + "\n  ".join(
        f"{label} 多出或缺少：{diff}" for label, diff in mismatched.items()
    )


def test_every_declared_layer_really_exists() -> None:
    """清单里写的包必须真的在，否则读者会去找一个不存在的东西。"""
    for name in _declared_layers(SRC / "__init__.py"):
        module = SRC / name
        assert module.is_dir() or module.with_suffix(".py").is_file(), (
            f"包文档里写了 qbank.{name}，但它不存在"
        )
