# Recruitment Mail Agent

一个小型招聘流程 Agent：增量读取 IMAP 邮箱中的招聘邮件，使用 DeepSeek 提取公司、岗位、流程状态和关键时间，再同步到飞书多维表格。

## 功能

- 严格增量：只下载 `UID > last_uid` 的新邮件；没有新邮件时不调用 LLM，也不消耗模型 Token。
- 正文识别：向模型提供主题、发件人、收信时间和正文前 4,000 字符。
- 结构化流程：支持 `待确认`、`投递`、`测评&AI面`、`笔试`、`技术面`、`HR面`、`主管面`、`Offer`、`已挂`。
- 时间提取：保存投递时间、更新时间，以及测评/笔试截止时间或已约面试时间。
- 人工协作：允许直接修改表格；同步不会覆盖已确认的公司和岗位，也不会回退人工推进的状态。
- 邮件星标：后续流程成功写入飞书后，为原邮件添加 IMAP `\Flagged`；不会将邮件标记为已读。
- 广告过滤：排除职位推荐、招聘简章、校招启动、宣讲会等非个人申请流程邮件。
- 断点续跑：SQLite 保存 IMAP 游标、已处理 Message-ID 和模型结果缓存。
- 项目内常驻：内置后台 watcher，默认每 5 分钟执行一次增量检查。

## 数据流

```text
IMAP（BODY.PEEK）
  → UID / Message-ID 去重
  → DeepSeek JSON 结构化抽取
  → 状态、时间和字段白名单校验
  → 飞书多维表格 upsert
  → SQLite 保存游标与结果缓存
  → 后续流程邮件添加 IMAP 星标
```

飞书表包含六列：`公司名称`、`岗位名称`、`流程状态`、`更新时间`、`投递时间`、`截止时间`。默认表格与看板按更新时间倒序排列，不同状态使用不同标签颜色。

## 环境要求

- Python 3.11+
- Node.js 16+
- 可访问 IMAP、DeepSeek API 和飞书开放平台
- 支持客户端密码或应用专用密码的邮箱账户

## 快速启动

```bash
git clone https://github.com/<YOUR_USERNAME>/recruitment-mail-agent.git
cd recruitment-mail-agent
npm ci
cp .env.example .env
chmod 600 .env
```

编辑 `.env`：

```dotenv
IMAP_EMAIL=you@example.com
IMAP_HOST=imap.example.com
IMAP_PORT=993
IMAP_FOLDER=INBOX
IMAP_PASSWORD=your_imap_app_password

DEEPSEEK_API_KEY=your_deepseek_api_key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_BATCH_SIZE=3

SYNC_SINCE_DATE=2026-08-20
SYNC_INTERVAL_SECONDS=300
SYNC_MAX_MESSAGES=300
SYNC_MIN_CONFIDENCE=0.55
SYNC_TIMEZONE=Asia/Shanghai
```

`SYNC_SINCE_DATE` 是首次建游标时允许读取的最早日期。建好游标后，程序只处理 UID 更大的新邮件。

### 配置飞书

项目使用官方 `@larksuite/cli`：

```bash
npm run lark -- config init --new
npm run lark -- auth login --recommend --domain base
npm run lark -- auth status
./tracker init-base
```

`init-base` 会创建六列表格和进展看板，并把 Base Token 与 Table ID 写入本地 `.env`。

### 检查与首次运行

```bash
./tracker doctor
./tracker sync --dry-run
./tracker sync
```

`--dry-run` 会读取和分类当前增量，但不会写飞书、邮箱星标或本地游标。

## 项目内部署

启动后台 watcher：

```bash
./tracker start
./tracker status
```

默认每 300 秒运行一次。调整为每 10 分钟：

```bash
./tracker stop
./tracker start --interval 600
```

查看日志：

```bash
tail -f logs/watcher.log
```

停止服务：

```bash
./tracker stop
```

watcher 与终端会话分离，关闭终端后仍会继续运行。它不注册系统服务，因此电脑重启后需要重新执行 `./tracker start`。

## 清空并重新识别

以下命令会删除目标飞书表的全部记录，并清空本地游标和分类缓存：

```bash
./tracker stop
./tracker rebuild --yes
./tracker start
```

字段和视图不会被删除。重建仍受 `SYNC_SINCE_DATE` 和 `SYNC_MAX_MESSAGES` 限制。

## 安全与隐私

- `.env`、SQLite、PID、日志、临时文件和依赖目录都在 `.gitignore` 中。
- IMAP 拉取使用 `BODY.PEEK[]`，不会因为读取正文而将邮件设为已读。
- 邮件主题、发件人、时间和正文前 4,000 字符会发送到配置的 DeepSeek API。
- 飞书只保存六个业务字段；Message-ID、LLM 缓存和 IMAP 游标保存在本地。
- 邮件正文按不可信输入处理；提示词禁止执行邮件中的指令、链接、代码或工具请求。
- 不要提交 `.env`，也不要在 Issue、日志或截图中公开邮箱密码、API Key、Base Token 和 Table ID。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

测试覆盖结构化输出校验、广告过滤、投递失败处理、截止时间约束、流程推进策略、岗位归一化和 IMAP 星标。

## License

[MIT](LICENSE)
