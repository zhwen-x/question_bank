#!/usr/bin/env python3
"""从 Markdown 示例生成 Word 与 HTML 示例。

三份示例必须表达同一批题目，否则用户照着 Word 示例写出来的文件可能导不进去。
由一份源头生成，顺带就是一次往返自测：如果 docx 示例导入后题数不对，
说明解析器或 pandoc 参数出了问题。

用法::

    python scripts/生成模板示例.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SOURCE = ROOT / "docs" / "examples" / "导入模板示例.md"
DOCX = SOURCE.with_suffix(".docx")
HTML = SOURCE.with_suffix(".html")


def main() -> int:
    import pypandoc

    from qbank.exchange.importers.template import parse_document
    from qbank.exchange.pandoc import MARKDOWN_SOURCE

    markdown = SOURCE.read_text(encoding="utf-8")
    expected = len(parse_document(markdown).drafts)
    print(f"源文件 {SOURCE.name}：{expected} 道题")

    targets = (
        ("docx", DOCX, ["--wrap=none"]),
        # --mathml 让公式以 MathML 形式写入 HTML。默认的 span 形式在读回来时
        # 只能当作原始 HTML 看待，公式就还原不成 LaTeX 了。
        ("html", HTML, ["--wrap=none", "--standalone", "--mathml"]),
    )
    for target, output, extra_args in targets:
        pypandoc.convert_text(
            markdown,
            target,
            format=MARKDOWN_SOURCE,
            outputfile=str(output),
            extra_args=extra_args,
        )
        print(f"已生成 {output.name}（{output.stat().st_size} 字节）")

    # 往返自测：从生成的文件读回来，题数必须一致
    from qbank.exchange.importers.loader import read_source

    failures = 0
    for output in (DOCX, HTML):
        with read_source(output) as document:
            report = parse_document(document.markdown)
        got = len(report.drafts)
        status = "一致" if got == expected else f"不一致（期望 {expected}）"
        print(f"  往返检查 {output.name}：解析出 {got} 道，{status}")
        for error in report.errors:
            print(f"      {error}")
        if got != expected or report.errors:
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
