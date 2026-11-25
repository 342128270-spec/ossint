# ossint — 公开科研情报采集器

一条命令，把散落在 OpenAlex / arXiv / PubMed 的公开研究成果聚成一份带引用排序的可读报告。**零 API key、零第三方依赖、零越权**。

```bash
python3 ossint.py search "protein language model" --since 2023
python3 ossint.py search "hypersonic vehicle" --sources openalex,arxiv --limit 60
python3 ossint.py search "crispr delivery" --sources pubmed --since 2024 --min-cites 5
python3 ossint.py stats                 # 本地库概览
python3 ossint.py report --out ./intel  # 从缓存重新出报告（不再联网）
```

产物：`report.html`（可读，含引用/年份/作者/摘要）、`results.csv`（导入表格）、`results.json`（编程用）。

## 为什么做这个

想要"顶尖技术情报"的人通常第一反应是去偷。其实 90% 的东西本来就公开：论文、预印本、专利、招标书。**难点不是获取，是筛选与串联**——两万条结果里真正有用的那 20 条。

ossint 就干这件事：多渠道并发采集 → 标题去重 → 引用与时效加权排序 → 出可读报告。

## 三个数据源

| 源 | 覆盖 | 特点 |
|---|---|---|
| **OpenAlex** | 2.5 亿+ 学术作品 | 引用数、机构、开放获取链接，摘要倒排索引需还原 |
| **arXiv** | 物理/CS/数学/生物预印本 | 最新前沿，比期刊早 6-18 个月，无引用数据 |
| **PubMed** | 生物医学 3,700 万+ | 临床与生命科学权威索引 |

全部免费公开、无需注册。请求带 `User-Agent` 与 `mailto`，符合各家的礼貌爬取约定。

## 排序算法

```
score = √引用数 × 时间衰减(1/(1+0.28×年差)) + 标题关键词命中×2 + 开放获取加成1.5
```

为什么不是纯引用数：2023 年那篇 746 引的经典综述会永远压住 2026 年的新突破，而做情报的人要的往往是后者。衰减让新东西有机会冒头，关键词命中则保证主题相关性。

## 实测（2026-11-26，本机）

```
$ python3 ossint.py search "protein language model" --since 2023 --limit 24
    openalex: 24 条 (6.6s)          → 24 条入库
  1. ProGen2: Exploring the boundaries of protein language models   2023 · 746 引
  2. Efficient evolution of human antibodies from general PLMs      2023 · 557 引
  3. Genome-wide prediction of disease variant effects with deep PLM 2023 · 540 引

$ python3 ossint.py search "mRNA lipid nanoparticle delivery" \
    --sources openalex,arxiv,pubmed --since 2023 --limit 30
    openalex: 10 条 (33.5s) | arxiv: 7 条 (1.1s) | pubmed: 10 条 (1.6s)
    → 25 条入库（跨源去重 2 条）
```

`stats` 输出：49 条，年份 2023–2026，引用合计 5,338。

## 设计取舍

- **SQLite 本地库**：同一主题反复查不重复请求；`report` 可离线重出报告。
- **倒排索引还原**：OpenAlex 的 `abstract_inverted_index` 是 `{词: [位置]}`，按位置排序拼回原文。
- **按源均分配额**：`--limit 50 --sources a,b` → 每源 25，避免单一源垄断结果。
- **失败不中断**：某源超时只打印警告，其余照跑。

## 边界（重要）

本工具**只访问公开学术元数据接口**，不抓取付费全文，不绕过任何访问控制，不接触任何非公开系统。想要封闭数据，请走机构订阅、数据共享协议或正式采购——那才是可持续的路。

## Roadmap

- [ ] 专利源：USPTO PatentsView / EPO OPS（需注册 key，可选）
- [ ] 全文 PDF 抓取（仅开放获取，遵守 robots.txt）
- [ ] 跨查询主题聚类与时间线可视化
- [ ] `watch` 子命令：跟踪某主题的新增成果，接 automations 定期推送

## 许可

MIT
