# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包说明。三平台共用这一份。

    pyinstaller packaging/qbank.spec --noconfirm

产出在 `dist/`：macOS 是 `qbank.app`（可双击），Windows/Linux 是 `dist/qbank/`
目录，里面的 `qbank` 可执行文件就是入口。

捆了什么，没捆什么
    **捆 pandoc**（约 110 MB）。Word 的导入与导出都靠它，而目标用户手里的
    历年真题基本都是 .docx——少了它，这个工具对他们就没什么用了。

    **默认不捆 playwright**（约 140 MB）。它只服务于 PDF 导出，而 PDF 有现成的
    退路：导出 HTML 再用浏览器打印，版面完全一样。要捆的话设环境变量：

        QBANK_BUNDLE_PDF=1 pyinstaller packaging/qbank.spec --noconfirm

    注意即使捆了 playwright，浏览器内核也**不在**包里——运行时仍然要靠系统
    装的 Chrome，或者用户自己 `playwright install chromium`。

    **捆 migrations/**。少了它，打包版打开旧版本的题库时不会升级表结构，
    而且一声不吭（见 `qbank.app._project_root` 的说明）。
"""

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent
WITH_PDF = os.environ.get("QBANK_BUNDLE_PDF") == "1"

# 模板、静态文件、内置导出模板都是运行时按路径读的，必须作为数据捆进去
datas = collect_data_files("qbank", includes=["**/*.html", "**/*.j2", "**/*.css", "**/*.js", "**/*.txt"])
datas += collect_data_files("pypandoc")  # 自带的 pandoc 二进制

# 这几个包在运行时按路径读自己的数据文件，PyInstaller 默认只收 .py，
# 不显式列出来就会在**用到的那一刻**才炸。实测踩过：少了
# latex2mathml/unimathsymbols.txt，md 导出正常，一导 HTML 就
# FileNotFoundError——而构建过程一个警告都没有。
datas += collect_data_files("latex2mathml")   # unimathsymbols.txt，LaTeX→MathML 的符号表
datas += collect_data_files("webview")        # pywebview 注入窗口的 JS 垫片
datas += collect_data_files("alembic")        # 迁移模板
datas += collect_data_files("markdown_it")
datas += [
    (str(ROOT / "alembic.ini"), "."),
    (str(ROOT / "migrations"), "migrations"),
]

hiddenimports = collect_submodules("qbank")
# uvicorn 与 sqlalchemy 的方言是运行时按名字导入的，静态分析看不见
hiddenimports += [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "sqlalchemy.dialects.sqlite",
]

excludes = ["tkinter", "pytest", "mypy", "ruff", "IPython", "matplotlib", "numpy"]
if not WITH_PDF:
    excludes += ["playwright"]
else:
    datas += collect_data_files("playwright")
    hiddenimports += collect_submodules("playwright")

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="qbank",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # 不是 console 程序：macOS 上双击不该先弹一个终端窗口。
    # 命令行用法仍然可用——直接执行 .app 里的那个二进制即可。
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="qbank",
)
app = BUNDLE(
    coll,
    name="qbank.app",
    icon=None,
    bundle_identifier="dev.qbank.app",
    info_plist={
        "CFBundleName": "qbank",
        "CFBundleDisplayName": "qbank 题库",
        "NSHighResolutionCapable": True,
        # 窗口里是本地 http://127.0.0.1，不开这个 macOS 会直接拦掉
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
    },
)
