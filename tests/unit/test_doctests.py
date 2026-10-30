"""把各模块的 doctest 纳入 pytest。

文档字符串里的例子是最容易腐烂的文档，让它们随测试一起跑。
"""

from __future__ import annotations

import doctest
import importlib

import pytest

MODULES = [
    "qbank.config",
    "qbank.core.enums",
    "qbank.core.content.segment",
    "qbank.core.content.math",
    "qbank.core.content.text",
    "qbank.core.content.render",
    "qbank.core.services.dedup",
    "qbank.core.services.asset",
    "qbank.exchange.pandoc",
    "qbank.exchange.importers.sections",
    "qbank.exchange.importers.template",
    "qbank.web.forms",
    "qbank.core.content.diff",
]


@pytest.mark.parametrize("module_name", MODULES)
def test_doctests(module_name: str) -> None:
    module = importlib.import_module(module_name)
    result = doctest.testmod(module, verbose=False, report=True)
    assert result.failed == 0, f"{module_name} 有 {result.failed} 个 doctest 失败"
