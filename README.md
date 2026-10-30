# qbank · 题库管理系统

跨平台（macOS / Linux / Windows）本地题库客户端：按**课程 → 章节 → 标签**管理含
图片、公式、表格的多类型题目，支持按模板批量导入与手工录入，支持手动与自动组卷，
并导出为 Markdown / HTML / Word / PDF。

## 它解决什么问题

题目散落在各处 Word 和 Markdown 文件里，无法统一检索、无法复用、每次出卷都在复制粘贴。
qbank 把题目收进一个结构化的本地库，让「找题 → 组卷 → 出卷」变成几分钟的事。

## 特性

- **多课程 / 多级章节 / 标签**，中文全文检索（jieba 分词 + SQLite FTS5）
- **题型**：单选、多选、判断、填空、综合应用（含子题）、编程
- **内容格式统一为 Markdown + LaTeX + GFM 表格**，图片按内容哈希寻址，迁移不断链
- **批量导入** md / docx / html，三种格式共用一套解析器；Word 公式自动转 LaTeX
- **导入前预览**：逐条校验、查重（精确 + 近似）、冲突策略可选，不做静默导入
- **组卷**：手动选题篮 + 按蓝图自动组卷（难度分布、章节覆盖、避免近期重复、A/B 卷）
- **导出** md / html / docx / pdf，试卷与答案卷同源，支持自定义模板
- 单机即用；架构分层，后续可直接部署为 Web 端

## 安装

需要 Python >= 3.10。

```bash
git clone https://github.com/zhwen/question_bank.git
cd question_bank
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[web,desktop,pdf]"
```

PDF 导出需要一次性下载 Chromium 内核（约 130 MB）：

```bash
playwright install chromium
```

不装也能用——PDF 会降级为「导出 HTML，由浏览器打印」。pandoc 由 `pypandoc_binary`
自带，三平台均无需单独安装。

## 快速开始

先确认环境：

```bash
qbank doctor
```

建课、录题、检索：

```bash
qbank course add "数据结构" --code CS201
qbank question add "数据结构" docs/examples/题目草稿示例.yaml
qbank question list --course 数据结构 -s 最短路径
qbank question show <题目id前8位>
```

常用命令：

| 命令 | 作用 |
|---|---|
| `qbank info` | 题库位置与规模 |
| `qbank chapter list <课程>` | 章节树与每节题量 |
| `qbank question list --type/--difficulty/--tag/-s` | 筛选与全文检索 |
| `qbank question duplicates <课程> <文件>` | 导入前查重（只报告，不改动） |
| `qbank question trash [--purge --yes]` | 回收站 |
| `qbank question history <id> [--rollback-to N]` | 修改历史与回滚 |
| `qbank tag merge <源> <目标>` | 合并同义标签 |
| `qbank reindex` | 重建全文索引 |

`qbank question add` 吃的是最贴近内部结构的 YAML/JSON，面向脚本化。
批量导入 md / docx / html 请用 `qbank import`（见[导入模板规范](docs/导入模板规范.md)）。

## 文档

- [设计说明](docs/设计说明.md)
- [导入模板规范](docs/导入模板规范.md) —— 批量导入前先读这篇
- [导出模板制作指南](docs/导出模板制作指南.md)

## 开发

```bash
pip install -e ".[web,dev,pdf]"
pre-commit install
pytest
```

分层约定（`tests/unit/test_layering.py` 会强制检查）：

| 包 | 职责 | 允许依赖 |
|---|---|---|
| `qbank.core` | 领域模型、存储、内容渲染、查重、组卷算法 | 不得依赖 `web` / `desktop` |
| `qbank.exchange` | 导入导出、pandoc 封装、`.qbz` 交换包 | 可依赖 `core` |
| `qbank.web` | FastAPI + Jinja2 + HTMX | 可依赖 `core` / `exchange` |
| `qbank.desktop` | pywebview 外壳 | 可依赖 `web` |
| `qbank.cli` | Typer 命令行 | 可依赖全部 |

**题库数据不进 Git**（见 `.gitignore`）。共享与备份走 `.qbz` 交换包。

## License

MIT
