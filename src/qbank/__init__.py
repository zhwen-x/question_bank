"""qbank —— 跨平台题库管理系统。

分层约定（务必遵守，这是后续迁移到 Web 端的前提）：

* ``qbank.core``     领域模型 / 存储 / 内容渲染 / 查重 / 组卷。**零 UI、零 Web 依赖**
* ``qbank.exchange`` 导入器 / 导出器 / pandoc 封装
* ``qbank.web``      FastAPI + Jinja2 页面（无前端框架）
* ``qbank.cli``      Typer 命令行

``core`` 与 ``exchange`` 不得 import ``web``；该约束由
``tests/unit/test_layering.py`` 用 AST 强制检查，是「后续可部署为 Web 端」
唯一的技术保障。

尚未实现的部分（``.qbz`` 交换包、``qbank.desktop`` 桌面外壳）见
``docs/设计说明.md``。
"""

__version__ = "0.1.0"
