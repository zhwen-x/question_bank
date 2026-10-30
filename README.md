# qbank · 题库管理系统

跨平台（macOS / Linux / Windows）本地题库客户端：按**课程 → 章节 → 标签**管理含
图片、公式、表格的多类型题目，支持按模板从 Word / Markdown / HTML 批量导入。

## 它解决什么问题

题目散落在各处 Word 和 Markdown 文件里，无法统一检索、无法复用、每次出卷都在复制粘贴。
qbank 把题目收进一个结构化的本地库，先让「找题」变成几秒钟的事。

## 当前状态

开发中。**已经可以日常使用**的部分：

- **多课程 / 多级章节 / 标签**，中文全文检索（字符 n-gram + SQLite FTS5）
- **七种题型**：单选、多选、判断、填空、简答、综合应用（含子题）、编程
- **内容统一为 Markdown + LaTeX + GFM 表格**，图片按内容哈希寻址，迁移不断链
- **批量导入** md / docx / html，三种格式共用一套解析器；Word 公式自动转 LaTeX
- **导入前预览**：逐条校验、查重、冲突策略可选，相似题逐字高亮差异，不做静默导入
- **图形界面**：课程章节树、题目列表与筛选、七种题型的结构化编辑器、导入向导
- **修改历史与回滚**、回收站、标签合并、孤儿图片清理、环境自检

**还没做**：

- 组卷（手动选题篮、按蓝图自动组卷）
- 导出 md / html / docx / pdf
- `.qbz` 整库交换包
- 桌面外壳（pywebview）与打包分发

设计上为它们预留的位置见[设计说明](docs/设计说明.md#八还没做的)。

## 安装

需要 Python >= 3.10。

```bash
git clone https://github.com/zhwen/question_bank.git
cd question_bank
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[web]"
```

pandoc 由 `pypandoc_binary` 自带，三平台均无需单独安装——Word / HTML 的导入开箱即用。

## 快速开始

先确认环境：

```bash
qbank doctor
```

建课、导入、检索：

```bash
qbank course add "数据结构" --code CS201
qbank import "数据结构" docs/examples/导入模板示例.md --dry-run -v   # 先看预览
qbank import "数据结构" docs/examples/导入模板示例.md
qbank question list --course 数据结构 -s 最短路径
```

或者直接用图形界面：

```bash
qbank serve
```

## 图形界面

浏览器会自动打开 <http://localhost:8765/>。界面包含：

- **课程 / 章节树**：侧栏按章节浏览，带每节题量；点一章会连带它的所有子章节
- **题目列表**：按题型、难度、状态、标签筛选 + 全文检索，公式直接渲染。
  筛选状态全在 URL 里，可以把「数据结构第 2 章的难题」收藏成书签
- **编辑器**：左边写 Markdown，右边实时预览；图片可以直接 Ctrl+V 粘贴或拖进来；
  各题型有对应的结构化字段（选项打勾、填空逐空、综合题逐小问）
- **导入向导**：拖文件进来 → 逐条预览（将要新建 / 已存在将跳过 / 第几行解析失败）
  → 勾选确认。相似题会**逐字高亮差异**，并对「只差几个字」额外提醒
- **修改历史**：每次保存前自动存一份快照，可回滚；回滚本身也会留痕
- **标签管理**：合并同义标签（「循环」「循环结构」「while循环」）
- **状态与自检**：能力检查、题库规模、索引是否过期、孤儿图片清理

默认只监听 `127.0.0.1`。要让同事访问需显式 `--host 0.0.0.0`，但请注意**目前还没有鉴权**。

界面零前端依赖：公式由服务端转成 MathML 交给浏览器原生渲染，不需要 KaTeX 的
JS 与字体文件，断网也能正常用。

## 批量导入

从 Word / Markdown / HTML 按模板导入。**先预览、后确认**：

```bash
qbank import "数据结构" 历年真题.docx --dry-run -v
```

它会逐条告诉你「将要新建 / 已存在将跳过 / 第几行解析失败」，**不写入任何内容**。
确认无误后去掉 `--dry-run`：

```bash
qbank import "数据结构" 历年真题.docx
qbank import "数据结构" 历年真题.docx --only 1,3-8      # 只导入某几道
qbank import "数据结构" 历年真题.docx --on-conflict overwrite
```

导错了可以整批撤销：

```bash
qbank batch list
qbank batch rollback <批次id前8位>
```

模板格式见 **[导入模板规范](docs/导入模板规范.md)**，示例文件在
[`docs/examples/`](docs/examples/)（md / docx / html 三份，内容等价，可直接改）。

要点：

- Word 里的**公式直接用公式编辑器**，导入时自动转成 LaTeX；图片直接贴，自动收进题库
- 合并单元格的表格会完整保留
- 一道题格式不对不影响同一份文件里的其他题目，错误带源文件行号
- 相似题只提示、不自动处理——一字之差就可能把题意反过来（正确↔错误）。
  界面会把差异逐字高亮，并对「只差几个字」的情况额外提醒

## 命令一览

| 命令 | 作用 |
|---|---|
| `qbank doctor` | 环境自检：存储、中文检索、pandoc、PDF、Web |
| `qbank serve` | 启动图形界面 |
| `qbank info` | 题库位置与规模 |
| `qbank course add / list / rm` | 课程 |
| `qbank chapter add / list` | 章节树与每节题量 |
| `qbank import <课程> <文件>` | 批量导入（见上） |
| `qbank formats` | 支持的导入格式及依赖状态 |
| `qbank batch list / rollback` | 导入历史与整批撤销 |
| `qbank question list --type/--difficulty/--tag/-s` | 筛选与全文检索 |
| `qbank question show <id>` | 查看一道题（`--yaml` 可导出后再导入） |
| `qbank question add <课程> <文件>` | 从 YAML/JSON 录入，面向脚本化 |
| `qbank question duplicates <课程> <文件>` | 查重（只报告，不改动） |
| `qbank question history <id> [--rollback-to N]` | 修改历史与回滚 |
| `qbank question rm / restore / trash` | 回收站 |
| `qbank tag list / merge / clean` | 标签 |
| `qbank asset stats / clean` | 图片占用与孤儿清理 |
| `qbank reindex` | 重建全文索引 |

## 文档

- [设计说明](docs/设计说明.md) —— 为什么这样做，以及被实测推翻过的设计
- [导入模板规范](docs/导入模板规范.md) —— 批量导入前先读这篇

## 开发

```bash
pip install -e ".[web,dev]"
pre-commit install
pytest
```

分层约定（`tests/unit/test_layering.py` 用 AST 强制检查）：

| 包 | 职责 | 允许依赖 |
|---|---|---|
| `qbank.core` | 领域模型、存储、内容渲染、查重 | 不得依赖 `web` |
| `qbank.exchange` | 导入导出、pandoc 封装 | 可依赖 `core` |
| `qbank.web` | FastAPI + Jinja2（零前端依赖） | 可依赖 `core` / `exchange` |
| `qbank.cli` | Typer 命令行 | 可依赖全部 |

这条约束是「后续可以部署成 Web 端」唯一的技术保障——只要界面层没渗进内核，
换前端或换部署形态就不需要动业务代码。

**题库数据不进 Git**（见 `.gitignore`）：SQLite 二进制文件和图片会让仓库迅速
膨胀且无法 diff。

## License

MIT
