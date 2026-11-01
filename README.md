# qbank · 题库管理系统

跨平台（macOS / Linux / Windows）本地题库客户端：按**课程 → 章节 → 标签**管理含
图片、公式、表格的多类型题目，支持按模板从 Word / Markdown / HTML 批量导入，
按蓝图自动组卷，并导出为 Markdown / HTML / Word / PDF。可打包成双击即用的桌面应用。

## 它解决什么问题

题目散落在各处 Word 和 Markdown 文件里，无法统一检索、无法复用、每次出卷都在复制粘贴。
qbank 把题目收进一个结构化的本地库，让「找题」变成几秒钟的事，让「出卷」变成
填一份蓝图，让「印卷子」变成点一下。

## 当前状态

开发中。**已经可以日常使用**的部分：

- **多课程 / 多级章节 / 标签**，中文全文检索（字符 n-gram + SQLite FTS5 / Postgres tsvector）
- **七种题型**：单选、多选、判断、填空、简答、综合应用（含子题）、编程
- **内容统一为 Markdown + LaTeX + GFM 表格**，图片按内容哈希寻址，迁移不断链
- **批量导入** md / docx / html，三种格式共用一套解析器；Word 公式自动转 LaTeX
- **导入前预览**：逐条校验、查重、冲突策略可选，相似题逐字高亮差异，不做静默导入
- **自动组卷**：按蓝图抽题（题型、题数、难度配比、章节、标签），先预览后确认；
  组不齐时说清缺哪一类、放宽哪个条件能补上几道，**不默默少出几道**
- **手工组卷**：一边翻题库一边勾选加入，分节、配分、排序、跨节移动
- **A/B 卷**：同一份蓝图换个种子再出一套，自动避开原卷用过的题
- **导出** md / html / docx / pdf，三种版面（学生卷 / 带答案 / 答案卷）、
  三套预设模板，Word 里的公式是原生可编辑的
- **图形界面**：课程章节树、题目列表与筛选、七种题型的结构化编辑器、导入向导、
  组卷、试卷编辑与导出
- **批量操作**：勾选若干道题，一次改标签、章节、难度、状态，或整批移入回收站
- **`.qbz` 交换包**：整库（或单门课）备份、共享、恢复——含题目、章节、标签、
  图片与试卷
- **桌面应用**：`qbank desktop` 开原生窗口；可用 PyInstaller 打成双击即用的包
- **修改历史与回滚**、回收站、标签合并、孤儿图片清理、环境自检
- **部署成 Web**：同一套代码换两个环境变量就能跑在 Postgres 上并带鉴权，
  见 **[Web 部署指南](docs/Web部署指南.md)**

**还没做**：

- 图片的对象存储（现在只落文件系统，多机部署要共享目录）
- 按人分权（只有「可编辑 / 只读」两档，没有用户表）
- 共享口令没有尝试次数限制，公网部署要在代理层加限流
- macOS / Windows 的代码签名与公证

设计上为它们预留的位置见[设计说明](docs/设计说明.md)最后一节。

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

出一份卷，再印出来：

```bash
qbank paper assemble "数据结构" docs/examples/组卷蓝图示例.yaml --dry-run   # 先看抽题结果
qbank paper assemble "数据结构" docs/examples/组卷蓝图示例.yaml
qbank paper export <试卷id前8位> -f pdf -f docx        # 学生卷
qbank paper export <试卷id前8位> -f pdf -l answers     # 答案与评分标准
```

或者直接用图形界面：

```bash
qbank serve      # 浏览器里打开
qbank desktop    # 原生窗口（需要 pip install "qbank[desktop]"）
```

**定期备份**——删课程是硬删除，没有回收站：

```bash
qbank bundle export -o ~/备份/题库.qbz
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
- **自动组卷**：填一份蓝图（各大题的题型、题数、难度配比、章节、标签）→ 预览抽题
  结果 → 确认生成。组不齐时不给「生成」按钮，而是列出缺哪一类、放宽哪个条件能
  补上几道
- **试卷编辑**：分节、逐题或整节配分、上下移、跨节移动、移出；可切「带答案 /
  学生视角」逐题核对；一键再出一套不重题的 B 卷
- **导出**：选格式、版面与模板，点一下就下载；Markdown 的旁挂图片会自动打成 zip
- **批量修改**：勾选若干道题，一次改标签、章节、难度、状态，或整批移入回收站；
  逐题留修改历史，改错了能一道道回滚
- **修改历史**：每次保存前自动存一份快照，可回滚；回滚本身也会留痕
- **标签管理**：合并同义标签（「循环」「循环结构」「while循环」）
- **状态与自检**：能力检查、题库规模、索引是否过期、孤儿图片清理
- **登录与只读身份**（设了 `QBANK_AUTH` 才出现）：共享口令进入，另给一个只读口令
  的话那一档能看能搜能导出，但所有修改入口都不显示、服务端也一律拒绝

默认只监听 `127.0.0.1`，此时不鉴权——防护来自「只有本机能连」。要让同事访问需显式
`--host 0.0.0.0`，**并且先设好 `QBANK_AUTH`**，否则局域网内任何人都能改这个题库
（`qbank serve` 会就此警告一句）。见 **[Web 部署指南](docs/Web部署指南.md)**。

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
| `qbank doctor` | 环境自检：数据库、中文检索、鉴权、pandoc、PDF、桌面与 Web |
| `qbank formats` | 支持的导入格式及依赖状态 |
| `qbank serve` | 启动图形界面 |
| `qbank info` | 题库位置与规模 |
| `qbank course add / list / rm` | 课程 |
| `qbank chapter add / list` | 章节树与每节题量 |
| `qbank import <课程> <文件>` | 批量导入（见上） |
| `qbank batch list / rollback` | 导入历史与整批撤销 |
| `qbank paper assemble <课程> <蓝图>` | 按蓝图自动组卷（`--dry-run` 先看） |
| `qbank paper list / show` | 试卷列表与卷面（`--answers` 带答案） |
| `qbank paper new / add` | 手工建卷与加题 |
| `qbank paper variant <id>` | 照同一蓝图再出一套，避开原卷用过的题 |
| `qbank paper rm <id>` | 删除试卷（默认进回收站） |
| `qbank paper export <id> -f pdf` | 导出 md/html/docx/pdf，`-l` 选版面 |
| `qbank paper templates` | 列出导出模板，`--copy` 拷一套出来改 |
| `qbank bundle export / import / info` | `.qbz` 交换包：备份、共享、恢复 |
| `qbank desktop` | 在原生窗口里打开 |
| `qbank question list --type/--difficulty/--tag/-s` | 筛选与全文检索 |
| `qbank question show <id>` | 查看一道题（`--yaml` 可导出后再导入） |
| `qbank question add <课程> <文件>` | 从 YAML/JSON 录入，面向脚本化 |
| `qbank question duplicates <课程> <文件>` | 查重（只报告，不改动） |
| `qbank question history <id> [--rollback-to N]` | 修改历史与回滚 |
| `qbank question rm / restore / trash` | 回收站 |
| `qbank tag list / merge / clean` | 标签 |
| `qbank asset stats / clean` | 图片占用与孤儿清理 |
| `qbank reindex` | 重建全文索引 |

## 组卷

蓝图规定「各大题要什么题型、几道、难度怎么配」，是一份可以存下来、提交进 git、
明年再用一次的文件：

```yaml
title: 2026 春季期末
seed: 20261030                   # 同种子可复现同一份卷
avoid_used_within_days: 180      # 避开近半年考过的题
sections:
  - title: 一、单项选择题（每题 2 分，共 20 分）
    types: [single_choice]
    count: 10
    score_each: 2
    difficulty_mix: {2: 0.3, 3: 0.5, 4: 0.2}   # 易 3 中 5 难 2
    shuffle_options: true
  - title: 二、综合应用题（每题 15 分，共 30 分）
    types: [comprehensive]
    count: 2
    score_each: 15
    tags_any: [图, 最短路径]
```

再写上 `total_score: 50`，各节配分之和对不上时会当场报错——笔误不必等到卷子
印出来才发现。

要点：

- **抽不齐不会默默少出几道**。会告诉你缺哪一类，以及放宽哪个条件能补上几道；
  几节在抢同一批题时也会点名，而不是让你以为题库不够
- **同一题库状态 + 同一份蓝图 + 同一个 seed = 同一份卷**，连选项乱序都一样
- **卷面在成卷那一刻冻结**。题库里改了题、删了题，去年的卷子不受影响
- 选项乱序后会**重新编号**并换算答案；答案里写了别的内容时保持原样，留给人核对

完整字段见 **[组卷蓝图规范](docs/组卷蓝图规范.md)**，示例在
[`docs/examples/组卷蓝图示例.yaml`](docs/examples/组卷蓝图示例.yaml)。

## 导出

四种格式，三种版面，三套预设模板：

```bash
qbank paper export <id> -f pdf                    # 学生卷
qbank paper export <id> -f pdf -l full            # 题目 + 答案（教师用）
qbank paper export <id> -f pdf -l answers         # 只有答案与评分标准
qbank paper export <id> -f md -f docx -o ~/卷子/  # 一次多种格式
qbank paper export <id> -f pdf -t exam            # 换一套模板
```

| 格式 | 特点 |
|---|---|
| **md** | 图片旁挂成一个目录，正文是相对路径。可提交进 git、可再加工 |
| **html** | **单文件**：图片内联、公式用 MathML，断网也能打开，直接发给学生 |
| **docx** | 公式是 Word **原生可编辑**的，合并单元格的表格完整保留 |
| **pdf** | 无头浏览器打印，所见即所得，自动带页码 |

| 模板 | 用途 |
|---|---|
| `standard` | A4 单栏，卷头含考试信息与班级/姓名/学号栏 |
| `exam` | 正式考试卷：密封线、得分栏、考场座位号 |
| `compact` | 练习卷：选项分栏，同样题量少用一半纸 |

要点：

- **导出的是成卷时冻结的题面**。题库里后来改了题、删了题，去年的卷子不受影响
- **学生卷里不会出现答案**——这条有专门的测试守着
- **选项乱序后答案自动跟着换**；答案里写了别的内容时保持原样，留给人核对
- 同一份卷导出两次**字节一致**（docx 除外，pandoc 会写时间戳），方便 diff 对比
- PDF 需要一个浏览器内核：`playwright install chromium`，**或者系统里已经装了
  Google Chrome / Edge** 即可（会自动找）。都没有时仍可导出 HTML 自行打印

改模板：`qbank paper templates --copy standard --as 我的`，改完用
`-t 我的` 导出。详见 **[导出模板制作指南](docs/导出模板制作指南.md)**。

## 备份与共享

**`.qbz` 交换包是本系统唯一的备份手段。** 删课程是硬删除，连题目与试卷一起没，
没有回收站也没有撤销——所以定期导一份放别处：

```bash
qbank bundle export -o ~/备份/题库.qbz          # 全部课程
qbank bundle export -c 数据结构 -o 数据结构.qbz   # 只导一门课
qbank bundle info 题库.qbz                      # 看看里面有什么
qbank bundle import 题库.qbz --dry-run          # 先看会发生什么
qbank bundle import 题库.qbz
```

包里有：课程与章节、题目（含图片与子题）、标签及其分类、试卷（含冻结的题面
快照）。**不含**修改历史与导入批次记录——它们是本地的操作痕迹，对搬库没有意义。

要点：

- **课程按名字合并**，题目与试卷**按 id 认重**。同一个包导两次不会把东西翻倍
- **题目的 UUID 跨库保持不变**。所以「发给同事 → 他改几道 → 再发回来」能认出
  那是同一道题，而不是当成一批新题再建一遍
- 题目已存在时按 `--on-conflict` 处理（`skip` / `overwrite` / `new_revision` /
  `create_copy`），和文件导入是同一套策略
- **试卷即使一道原题都没导进来也完整**——题面冻结在卷子里，只是点不回原题

## 桌面应用

```bash
pip install -e ".[web,desktop]"
qbank desktop
```

界面和 `qbank serve` 完全一样，只是套了个原生窗口、用随机端口（不会和已经开着
的 `qbank serve` 抢 8765）。

打成双击即用的包：

```bash
pip install -e ".[web,desktop,package]"
pyinstaller packaging/qbank.spec --noconfirm
```

产出在 `dist/`：macOS 是 `qbank.app`，Windows / Linux 是 `dist/qbank/`。
包里捆了 pandoc（Word 导入导出），**默认不捆 PDF 支持**（省约 140 MB；PDF 可以
用「导出 HTML 再浏览器打印」代替）。细节与取舍见
**[打包分发指南](docs/打包分发指南.md)**。

打包版同时也是命令行：

```bash
dist/qbank.app/Contents/MacOS/qbank doctor
dist/qbank.app/Contents/MacOS/qbank bundle export -o 备份.qbz
```

## 部署到 Web

同一套代码，换几个环境变量就从「自己电脑上的窗口」变成「同事们都能打开的网址」：

```bash
pip install -e ".[web,postgres]"

export QBANK_DATABASE_URL="postgresql://qbank:口令@127.0.0.1/qbank"
export QBANK_AUTH=password
export QBANK_AUTH_PASSWORD='教研室内部口令'
export QBANK_AUTH_READONLY_PASSWORD='给学生看的口令'   # 可选，这一档只能看

alembic upgrade head
uvicorn qbank.web.app:app --factory --host 127.0.0.1 --port 8765 --workers 4
```

鉴权有三种：不鉴权（本地默认）、共享口令、信任反向代理注入的身份头（接已有的
统一认证）。**配错不会退回宽松默认，而是拒绝启动**。所有写请求都要过同源检查，
这一层在不鉴权模式下也开着。

现有题库搬过去用 `.qbz`，题目 UUID 原样保留：

```bash
qbank bundle export --output 题库.qbz                      # 旧环境
QBANK_DATABASE_URL=... qbank bundle import 题库.qbz        # 新环境
```

反向代理配置、HTTPS、备份、以及「所有写操作 403 但读正常」这类故障的排查，
见 **[Web 部署指南](docs/Web部署指南.md)**。

## 文档

- [设计说明](docs/设计说明.md) —— 为什么这样做，以及被实测推翻过的设计
- [导入模板规范](docs/导入模板规范.md) —— 批量导入前先读这篇
- [组卷蓝图规范](docs/组卷蓝图规范.md) —— 蓝图的完整字段
- [导出模板制作指南](docs/导出模板制作指南.md) —— 改版面、做自己的模板
- [打包分发指南](docs/打包分发指南.md) —— 打成桌面应用，以及捆了什么没捆什么
- [Web 部署指南](docs/Web部署指南.md) —— Postgres、鉴权、反向代理、常见故障
- [变更记录](CHANGELOG.md) —— 各里程碑做了什么、推翻过什么

## 开发

```bash
pip install -e ".[web,dev]"
pre-commit install
pytest
```

分层约定（`tests/unit/test_layering.py` 用 AST 强制检查）：

| 包 | 职责 | 允许依赖 |
|---|---|---|
| `qbank.core` | 领域模型、存储、内容渲染、查重、组卷 | 不得依赖 `web` |
| `qbank.exchange` | 导入导出、`.qbz` 交换包、pandoc 封装 | 可依赖 `core` |
| `qbank.web` | FastAPI + Jinja2（零前端依赖）、鉴权与同源检查 | 可依赖 `core` / `exchange` |
| `qbank.desktop` | pywebview 外壳 | 只负责开窗，不含业务 |
| `qbank.cli` | Typer 命令行 | 可依赖全部 |

这条约束是「后续可以部署成 Web 端」唯一的技术保障——只要界面层没渗进内核，
换前端或换部署形态就不需要动业务代码。M7 兑现了这张支票：换到 Postgres、
加上鉴权，`core` 与 `exchange` 两个包**一行没改**。

针对 Postgres 的测试默认跳过，要跑的话给一个可以随便删的空库：

```bash
createdb qbank_test
QBANK_TEST_DATABASE_URL=postgresql://localhost/qbank_test pytest tests/integration/test_postgres.py
```

**题库数据不进 Git**（见 `.gitignore`）：SQLite 二进制文件和图片会让仓库迅速
膨胀且无法 diff。

## License

MIT
