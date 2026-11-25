# 共享情报库（多智能体共用）

所有跑在这个工作区的智能体共用一个 SQLite 库：`shared/intel/intel.db`。
写入用 WAL + `busy_timeout=15s` + `BEGIN IMMEDIATE` 重试，**多个智能体同时写不会互相锁死**（已实测 3 并发无报错）。

## 一条命令接入

```bash
shared/intel/bin/ossint stats                  # 库概览
shared/intel/bin/ossint find "crispr"          # 查已有数据，不联网
shared/intel/bin/ossint search "基因编辑递送" --sources openalex,pubmed --since 2024
shared/intel/bin/ossint export --format jsonl  # 拿去喂给别的模型/管道
```

包装脚本自动设置：

- `OSSINT_DB` → 本工作区共享库
- `OSSINT_AGENT` → `$OPENCLAW_AGENT_ID`（记录谁写的，`stats` 里能看到来源分布）

## 写数据时记得报身份

```bash
OSSINT_AGENT="researcher-1" shared/intel/bin/ossint search "hypersonic" --limit 20
```

`works.added_by` 会记下写入方，`queries.created_by` 记检索请求，方便追责与去重。

## 表结构

| 表 | 用途 |
|---|---|
| `works` | 主表。`uid` 哈希主键，同一条成果重复采集只更新不重复插入 |
| `works_fts` | FTS5 全文索引（title + abstract），`find` 走它 |
| `queries` | 检索历史（查询词、来源、条数、时间、发起方） |
| `work_queries` | 成果 ↔ 查询 多对多 |
| `tags` | 人工/脚本打的标签 |
| `meta` | schema 版本与创建时间 |

主键去重按 `(source, 原始 id)` 哈希，**同一篇论文从 OpenAlex 和 PubMed 各来一次会存两条**（DOI 相同）——这是有意的：两边的引用数和摘要质量不同，保留全貌；`find` 里看 DOI 就能认出重复。

## 并发与备份

- WAL 模式：读者永不阻塞写者，写者之间靠 `busy_timeout` 排队
- 体检：`ossint doctor`（完整性 + 日志模式 + FTS + 写锁测试）
- 备份（在线安全，不影响写入）：

```bash
sqlite3 shared/intel/intel.db ".backup 'shared/intel/backup-$(date +%F).db'"
```

## 给智能体的约定

1. **先 `find` 再 `search`**：库里有的别再联网抓，省时间也省对方服务器
2. **一次一主题**：`search` 的 query 就是这条记录的归属，别用 "test"/"tmp" 当查询词
3. **导出用 jsonl**：`export --format jsonl` 是跨模型传递的标准格式，一行一条
4. **不写私有数据**：这个库是工作区共享的，别把个人隐私、凭据、内部数据 `import` 进来

## 当前规模

```
条目 91 · 344 KB · 2014–2026 · 引用合计 10,045
openalex 66 | pubmed 18 | arxiv 7
```

会随采集增长。要扩容就 `search` 更多主题，别手动插库。
