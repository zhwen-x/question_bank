# Web 部署指南

把 qbank 从「自己电脑上的一个窗口」变成「同事们都能打开的一个网址」。

跑的是**同一套代码**：没有「Web 版」这个分支，也没有需要重写的部分。区别全在三个
环境变量和一层反向代理上。

---

## 一、先想清楚要哪一种

| 形态 | 数据库 | 鉴权 | 适合 |
|---|---|---|---|
| 本机单人 | SQLite（默认） | 不要 | 自己用。`qbank desktop` 或 `qbank serve` |
| 一台服务器、几位老师 | SQLite 或 Postgres | 共享口令 | 一个教研室 |
| 学校内网统一入口 | Postgres | 反向代理身份头 | 接已有的统一身份认证 |

**不用急着上 Postgres。** SQLite 在 WAL 模式下支撑得住几个人同时用：读互不阻塞，
写是串行的，而录题这种操作一次只写几 KB。真正该换的信号是「写入经常撞上」——
批量导入的同时别人在编辑，或者打算跑多个 worker 进程。

---

## 二、环境变量速查

| 变量 | 默认 | 说明 |
|---|---|---|
| `QBANK_DATA_DIR` | 按平台惯例 | 数据目录：图片、导出、暂存、日志。**不只是数据库** |
| `QBANK_DATABASE_URL` | 数据目录下的 SQLite | 设了就用它，见下节 |
| `QBANK_AUTH` | `open` | `open` / `password` / `header` |
| `QBANK_AUTH_PASSWORD` | — | `password` 模式必填，**不填就拒绝启动** |
| `QBANK_AUTH_READONLY_PASSWORD` | — | 另给一个只读口令，可不设 |
| `QBANK_AUTH_HEADER` | — | `header` 模式必填，如 `X-Forwarded-User` |
| `QBANK_AUTH_EDITORS` | 空 | `header` 模式下谁能改；留空表示进得来的都能改 |
| `QBANK_SECRET_KEY` | 从口令派生 | 会话签名密钥。一般不用设 |
| `QBANK_HTTPS_ONLY` | `0` | **上了 HTTPS 就设成 1**，见第七节 |

配错不会退回宽松默认，而是**拒绝启动**：一个以为自己有鉴权、实际敞开的部署，比一个
起不来的部署危险得多。

---

## 三、Postgres

### 建库

```bash
createuser --pwprompt qbank
createdb --owner=qbank qbank
```

连接串写法：

```bash
export QBANK_DATABASE_URL="postgresql://qbank:口令@127.0.0.1:5432/qbank"
```

`postgres://`（Heroku / Railway 这类平台给的写法）和裸 `postgresql://` 都会被自动改成
`postgresql+psycopg://`，不必自己改。驱动要装：

```bash
pip install "qbank[web,postgres]"
```

### 建表

```bash
alembic upgrade head
```

应用第一次打开一个空库时也会自动建表，但部署时**显式跑一次**更好：迁移失败应该在
部署脚本里炸出来，而不是在第一个访客的页面上。

### 把现有题库搬过去

用 `.qbz` 交换包，不要去导 SQL：

```bash
# 在原来那台机器上
qbank bundle export --output 题库.qbz

# 在新环境里
QBANK_DATABASE_URL=... qbank bundle import 题库.qbz
```

题目的 UUID 在包里原样保留，搬过去之后还是同一道题——以后两边再互相发包也认得出。
图片随包走。检索索引在导入时重建，不需要额外动作。

### 中文检索在 Postgres 上是怎么做的

> 一句话：**不用 Postgres 自己的分词器**。

分词在 Python 里完成（字符一元 + 二元 n-gram），切好的 token 拼成 `tsvector` 字面量
直接写进去，绕过 `to_tsvector` 和它背后的解析器。不这么做不行——实测 PostgreSQL 16
的 `default` 解析器把中日韩字符判成空白：

```sql
SELECT to_tsvector('simple', '栈 图 最短 cpp');
 -> 'cpp':4
```

中文 token 一个都没进去。症状会是「搜英文能搜到、搜中文永远空」，而索引表看起来是满的。

绕开之后，两种后端索引的是同一批 token，**命中集合完全一致**（有测试守着：
`tests/integration/test_postgres.py::test_both_backends_return_the_same_hits`）。
排序分数不同——bm25 与 `ts_rank` 是两套公式——但加权意图一样：题干 > 标签 > 答案与解析。

---

## 四、跑起来

```bash
qbank serve --host 0.0.0.0 --port 8765 --no-open
```

或者直接用 uvicorn（部署时更常见，便于配 worker 数与代理头）：

```bash
uvicorn qbank.web.app:app --factory \
    --host 127.0.0.1 --port 8765 \
    --workers 4 \
    --proxy-headers --forwarded-allow-ips='127.0.0.1'
```

几个要点：

- **`--factory` 不能少**，`qbank.web.app:app` 是个工厂函数。
- **多 worker 请配 Postgres。** SQLite 多进程写会互相等锁；真撞上时报
  `database is locked`（已经设了 5 秒的 `busy_timeout`，但那只是推迟了问题）。
- `--proxy-headers` 让应用认 `X-Forwarded-Proto`，否则生成的重定向地址会是 `http://`。
  `--forwarded-allow-ips` 要写**代理的地址**，不能写 `*`：那等于让任何直连应用端口的人
  自称来自 HTTPS。
- 监听 `127.0.0.1` 让代理转发，不要让应用端口直接暴露——绕过代理就绕过了代理上的
  所有防护（包括第六节 `header` 模式的全部安全性）。

### systemd

```ini
[Unit]
Description=qbank
After=network.target postgresql.service

[Service]
User=qbank
Environment=QBANK_DATA_DIR=/var/lib/qbank
Environment=QBANK_DATABASE_URL=postgresql://qbank:口令@127.0.0.1/qbank
Environment=QBANK_AUTH=password
Environment=QBANK_AUTH_PASSWORD=换成真的口令
Environment=QBANK_HTTPS_ONLY=1
ExecStart=/opt/qbank/.venv/bin/uvicorn qbank.web.app:app --factory \
          --host 127.0.0.1 --port 8765 --workers 4 \
          --proxy-headers --forwarded-allow-ips=127.0.0.1
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

口令写在 unit 文件里会进 `systemctl show` 和日志。正式环境用
`EnvironmentFile=/etc/qbank.env` 并把那个文件设成 `0600`。

---

## 五、反向代理（nginx）

```nginx
server {
    listen 443 ssl http2;
    server_name qbank.example.cn;

    # 导入 Word 时单个文件可能几十 MB。应用自己限到 80 MB，
    # 这里设得比它稍大，让超限的提示由应用给出（文案比 nginx 的 413 友好）
    client_max_body_size 100m;

    location / {
        proxy_pass http://127.0.0.1:8765;

        # Host 必须透传：同源检查比的就是它和 Origin
        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;

        # 导出 PDF、批量导入都可能跑十几秒
        proxy_read_timeout 120s;
    }
}
```

**`proxy_set_header Host $host` 是必须的。** 写成 `$proxy_host`（或者干脆不写）的话，
应用收到的 `Host` 是 `127.0.0.1:8765`，而浏览器发来的 `Origin` 是真实域名，同源检查
一律不通过——表现是**所有写操作都 403，读却完全正常**。

静态资源不必单独配 `location`：整个界面只有一个 CSS 和一个 JS，交给应用就行。

---

## 六、鉴权

### `open`——不鉴权

本地客户端与桌面版的正确设置。`qbank serve` 默认绑 `127.0.0.1`，防护来自「只有本机
能连」。**一旦 `--host 0.0.0.0`，这层防护就没了**，局域网里任何人都能改题库。

### `password`——共享口令

```bash
export QBANK_AUTH=password
export QBANK_AUTH_PASSWORD='教研室内部口令'
export QBANK_AUTH_READONLY_PASSWORD='给学生看的口令'   # 可选
```

适合一个教研室几个人共用。口令比账号体系粗，但和真实的协作方式是对齐的：
「这学期的题库，组里这几个人都能改」——谁改的不重要，每道题的修改历史本来就逐条
留在库里。

只读口令那一档能看能搜能导出，**不能改任何东西**。界面上的修改入口会整体隐藏，
服务端对所有写请求返回 403（有测试逐条枚举全部写路由来断言这一点）。

会话 cookie 的签名密钥默认从口令派生，所以多 worker 天然一致、重启也一致，
而**改口令会立刻作废所有现有会话**——这正是改口令时想要的效果。

### `header`——接已有的统一认证

```bash
export QBANK_AUTH=header
export QBANK_AUTH_HEADER=X-Forwarded-User
export QBANK_AUTH_EDITORS='zhang,li,wang'    # 留空表示进得来的都能改
```

由 nginx / Apache / oauth2-proxy / 各家网关先完成认证，再把用户名写进请求头，应用只读它。
接 SSO 不需要在题库里实现任何协议。

> **这个模式的前提必须说清楚**：HTTP 头是客户端可以随便写的。只有当代理**覆盖**
> （而不是追加）该头时它才成立。nginx 里要写成
> `proxy_set_header X-Forwarded-User $认证得到的用户名;`——这条会覆盖客户端发来的同名头。
> 而且应用端口绝不能直接暴露：谁能直连，谁就能自称任何身份。

---

## 七、HTTPS 与 cookie

`QBANK_HTTPS_ONLY` **默认是 0**，不是「安全优先」的 1。这是故意的：在明文 HTTP 的
内网部署里设成 1，浏览器根本不会保存会话 cookie，表现是**登录成功后又被弹回登录页，
而且没有任何报错**——这种故障极难自查。

所以规矩是：**上了 TLS 就显式设 `QBANK_HTTPS_ONLY=1`**，这是部署清单上的必选项。

会话 cookie 本身是 `HttpOnly` + `SameSite=Lax`，有效期 12 小时（一个工作日；再短会在
批量录题中途把人踢下线）。

### 跨站写请求（CSRF）

所有写请求都要通过同源检查：带了 `Origin` 却和 `Host` 对不上、或者
`Sec-Fetch-Site` 说是跨站，一律 403。

这层防护**在不鉴权模式下也开着**：`qbank serve` 绑在 `127.0.0.1` 上，任何网页都能
向它发跨站表单提交，而那里没有鉴权——「只有本机能连」挡不住这个。

---

## 八、数据目录必须是共享的

数据库换成 Postgres 之后，**仍然有东西落在本地磁盘上**：

- `assets/`——题目里的图片，按内容哈希存
- `cache/imports/`——导入向导「先预览、后确认」之间的暂存文件
- `exports/`、`backups/`、`logs/`

所以：

- **一台服务器多个 worker**：没问题，它们共用同一个 `QBANK_DATA_DIR`。
- **多台服务器**：`QBANK_DATA_DIR` 必须指向共享存储（NFS 之类）。否则图片只在上传的
  那台机器上，别的机器渲染出来是破图；导入预览也会随机找不到暂存文件。

对象存储目前**不支持**——所有落盘都走 `qbank.config.Paths`，换成 S3 需要在那一层加一个
存储抽象，还没做。

---

## 九、备份

数据库和数据目录要一起备，缺一不可：

```bash
pg_dump --format=custom qbank > qbank-$(date +%F).dump
tar czf qbank-data-$(date +%F).tar.gz -C /var/lib/qbank assets
```

或者用交换包，一个文件装下全部（题目、图片、试卷）：

```bash
qbank bundle export --output /backup/题库-$(date +%F).qbz
```

两者的差别：`pg_dump` 是精确快照，连导入批次和修改历史都在；`.qbz` 只带业务数据，
但**跨版本、跨后端都能装回去**，而且装回去的是同一批 UUID。日常备份用 `.qbz`
就够了，它也是唯一经过「删掉整门课再恢复」测试的那条路。

---

## 十、出问题时先跑自检

```bash
qbank doctor
```

它会报出当前用的是哪个库、连不连得上、中文检索在**当前后端**上是否真的工作、
以及鉴权是哪种模式。

| 症状 | 多半是 |
|---|---|
| 所有写操作 403，读正常 | nginx 没有 `proxy_set_header Host $host` |
| 登录成功又被弹回登录页 | 明文 HTTP 却设了 `QBANK_HTTPS_ONLY=1` |
| 会话随机失效 | 多 worker 且各自设了不同的 `QBANK_SECRET_KEY` |
| 搜中文永远空、搜英文正常 | 索引是用别的途径写进去的，没走本项目的分词。跑 `qbank reindex` |
| 早上第一次打开必定 500，刷新就好 | 连接池拿到了被掐断的空闲连接。确认用的是本项目的 `make_engine`（它设了 `pool_pre_ping`） |
| 图片有时显示有时破图 | 多台服务器而 `QBANK_DATA_DIR` 没共享，见第八节 |
| `ModuleNotFoundError: psycopg2` | 装一下 `pip install "qbank[postgres]"` |

---

## 十一、这份方案还没做的

- **对象存储**：图片只能落在文件系统上，多机部署要靠共享存储。
- **按人分权**：只有「可编辑 / 只读」两档，没有用户表，也记不住「这道题是谁改的」
  （修改历史记的是改了什么，不是谁改的）。题库里的数据不属于某个人，再细的权限模型
  暂时没有对应的真实需求。
- **限流**：共享口令没有尝试次数限制。放在公网上时请在代理层加
  （nginx 的 `limit_req`），或者只在内网开放。
