"""qbank —— 跨平台题库管理系统。

分层约定（务必遵守，这是后续迁移到 Web 端的前提）：

* ``qbank.core``     领域模型 / 存储 / 内容渲染 / 查重 / 组卷算法。**零 UI、零 Web 依赖**
* ``qbank.exchange`` 导入器 / 导出器 / pandoc 封装 / ``.qbz`` 交换包
* ``qbank.web``      FastAPI + Jinja2 + HTMX 路由与页面
* ``qbank.desktop``  pywebview 外壳
* ``qbank.cli``      Typer 命令行

``core`` 与 ``exchange`` 不得 import ``web`` 或 ``desktop``；该约束由
``tests/unit/test_layering.py`` 强制检查。
"""

__version__ = "0.1.0"
