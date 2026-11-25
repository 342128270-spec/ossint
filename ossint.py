#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ossint —— 公开科研/技术情报采集器（Open Source Scientific & Technical INTelligence）

只采集**公开、免费、无需授权**的学术与技术元数据源：
  OpenAlex  —— 2.5 亿+ 学术作品，含引用数、机构、开放获取链接
  arXiv     —— 预印本全文检索（物理/CS/数学/生物）
  PubMed    —— 生物医学文献（NCBI E-utilities）

三个都无需 API key。零第三方依赖，Python 3.9+ 标准库。

用法:
  python3 ossint.py search "protein language model" --since 2023
  python3 ossint.py search "hypersonic vehicle" --sources openalex,arxiv --limit 60
  python3 ossint.py search "crispr delivery" --sources pubmed --since 2024 --min-cites 5
  python3 ossint.py report --out ./intel        # 从本地库重新生成报告
  python3 ossint.py stats                       # 本地库概览

产物: report.html（可读）/ results.csv（可导入）/ results.json（可编程）
"""
import argparse, csv, html, json, os, sqlite3, sys, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

VERSION = "0.1.0"
UA = f"ossint/{VERSION} (open-source research aggregator; contact: local-user)"
DB = os.path.expanduser("~/.ossint/index.db")

# ---------------------------------------------------------------- 网络

def fetch(url, tries=3, timeout=25):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            return raw
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.2 * (i + 1))
    raise RuntimeError(f"抓取失败 {url}: {last}")


def get_json(url, tries=3):
    return json.loads(fetch(url, tries=tries).decode("utf-8", "replace"))


def get_text(url, tries=3):
    return fetch(url, tries=tries).decode("utf-8", "replace")


# ---------------------------------------------------------------- 数据模型

REC_FIELDS = ["uid", "source", "title", "year", "doi", "url", "venue",
              "authors", "citations", "abstract", "oa_url", "concepts"]


def rec(**kw):
    d = {k: kw.get(k) for k in REC_FIELDS}
    d["authors"] = d["authors"] or []
    d["concepts"] = d["concepts"] or []
    d["citations"] = d["citations"] or 0
    return d


def clean_abstract(inv_idx):
    """OpenAlex 的 abstract 是倒排索引，还原成文本"""
    if not inv_idx:
        return ""
    slots = []
    for word, positions in inv_idx.items():
        for p in positions:
            slots.append((p, word))
    slots.sort()
    return " ".join(w for _, w in slots)


# ---------------------------------------------------------------- 源: OpenAlex

def src_openalex(q, since, until, limit):
    out, cursor = [], "*"
    per = 200
    while len(out) < limit:
        params = {
            "search": q, "per-page": str(min(per, limit - len(out))),
            "cursor": cursor, "mailto": "ossint@localhost",
            "select": "id,doi,title,publication_year,cited_by_count,authorships,"
                      "primary_location,open_access,abstract_inverted_index,concepts",
        }
        f = []
        if since:
            f.append(f"from_publication_date:{since}-01-01")
        if until:
            f.append(f"to_publication_date:{until}-12-31")
        if f:
            params["filter"] = ",".join(f)
        data = get_json("https://api.openalex.org/works?" + urllib.parse.urlencode(params))
        results = data.get("results") or []
        if not results:
            break
        for w in results:
            loc = w.get("primary_location") or {}
            srcname = ((loc.get("source") or {}) or {}).get("display_name") or ""
            oa = (w.get("open_access") or {}).get("oa_url") or ""
            out.append(rec(
                uid=(w.get("doi") or w.get("id") or "").lower(),
                source="openalex",
                title=w.get("title") or "(无标题)",
                year=w.get("publication_year"),
                doi=(w.get("doi") or "").replace("https://doi.org/", ""),
                url=w.get("doi") or w.get("id") or "",
                venue=srcname,
                authors=[(a.get("author") or {}).get("display_name", "")
                         for a in (w.get("authorships") or [])][:12],
                citations=w.get("cited_by_count") or 0,
                abstract=clean_abstract(w.get("abstract_inverted_index")),
                oa_url=oa,
                concepts=[c.get("display_name") for c in (w.get("concepts") or [])][:6],
            ))
        cursor = data.get("meta", {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(0.15)
    return out[:limit]


# ---------------------------------------------------------------- 源: arXiv

ARXIV_NS = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


def src_arxiv(q, since, until, limit):
    query = f"all:{q}" if " " not in q else "all:" + q
    params = {"search_query": query, "start": 0,
              "max_results": str(min(limit, 200)), "sortBy": "relevance"}
    xml = get_text("https://export.arxiv.org/api/query?" + urllib.parse.urlencode(params))
    root = ET.fromstring(xml)
    out = []
    for e in root.findall("a:entry", ARXIV_NS):
        title = " ".join((e.findtext("a:title", "", ARXIV_NS) or "").split())
        pub = e.findtext("a:published", "", ARXIV_NS) or ""
        year = int(pub[:4]) if pub[:4].isdigit() else None
        if since and year and year < int(since):
            continue
        if until and year and year > int(until):
            continue
        aid = e.findtext("a:id", "", ARXIV_NS)
        out.append(rec(
            uid=aid.lower(), source="arxiv", title=title or "(无标题)", year=year,
            doi=e.findtext("arxiv:doi", "", ARXIV_NS) or "",
            url=aid, venue="arXiv",
            authors=[a.findtext("a:name", "", ARXIV_NS)
                     for a in e.findall("a:author", ARXIV_NS)][:12],
            citations=0,
            abstract=" ".join((e.findtext("a:summary", "", ARXIV_NS) or "").split()),
            oa_url=aid,
            concepts=[c.get("term") for c in e.findall("a:category", ARXIV_NS)][:6],
        ))
    return out[:limit]


# ---------------------------------------------------------------- 源: PubMed

def src_pubmed(q, since, until, limit):
    term = q
    if since:
        term += f' AND ("{since}"[Date - Publication] : "3000"[Date - Publication])'
    ids = get_json("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?"
                   + urllib.parse.urlencode({"db": "pubmed", "term": term,
                                             "retmode": "json", "retmax": str(min(limit, 200)),
                                             "sort": "relevance"}))
    idlist = ids.get("esearchresult", {}).get("idlist") or []
    if not idlist:
        return []
    time.sleep(0.4)
    sm = get_json("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?"
                  + urllib.parse.urlencode({"db": "pubmed", "id": ",".join(idlist),
                                            "retmode": "json"}))
    out = []
    for pid in idlist:
        d = (sm.get("result") or {}).get(pid) or {}
        if not isinstance(d, dict):
            continue
        year = None
        pd = d.get("pubdate") or ""
        if pd[:4].isdigit():
            year = int(pd[:4])
        out.append(rec(
            uid=f"pubmed:{pid}", source="pubmed", title=(d.get("title") or "").rstrip("."),
            year=year, doi=next((x["value"] for x in (d.get("articleids") or [])
                                 if x.get("idtype") == "doi"), ""),
            url=f"https://pubmed.ncbi.nlm.nih.gov/{pid}/",
            venue=d.get("fulljournalname") or d.get("source") or "",
            authors=[a.get("name", "") for a in (d.get("authors") or [])][:12],
            citations=0, abstract="", oa_url="",
            concepts=[x for x in (d.get("pubtype") or [])][:6],
        ))
    return out


SOURCES = {"openalex": src_openalex, "arxiv": src_arxiv, "pubmed": src_pubmed}


# ---------------------------------------------------------------- 存储

def db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS works(
        uid TEXT PRIMARY KEY, source TEXT, title TEXT, year INT, doi TEXT, url TEXT,
        venue TEXT, authors TEXT, citations INT, abstract TEXT, oa_url TEXT,
        concepts TEXT, query TEXT, fetched_at TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_q ON works(query)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_year ON works(year)")
    return c


def save(rows, query):
    c = db()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    n = 0
    for r in rows:
        try:
            c.execute("INSERT OR REPLACE INTO works VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (r["uid"], r["source"], r["title"], r["year"], r["doi"], r["url"],
                       r["venue"], json.dumps(r["authors"], ensure_ascii=False),
                       r["citations"], r["abstract"], r["oa_url"],
                       json.dumps(r["concepts"], ensure_ascii=False), query, now))
            n += 1
        except sqlite3.Error as e:
            print(f"[warn] 跳过 {r['uid'][:40]}: {e}", file=sys.stderr)
    c.commit()
    c.close()
    return n


def load(query=None):
    c = db()
    q = "SELECT * FROM works"
    args = ()
    if query:
        q += " WHERE query=?"
        args = (query,)
    rows = []
    for row in c.execute(q, args):
        d = dict(zip([x[0] for x in c.execute("SELECT * FROM works LIMIT 0").description], row))
        d["authors"] = json.loads(d["authors"] or "[]")
        d["concepts"] = json.loads(d["concepts"] or "[]")
        rows.append(d)
    c.close()
    return rows


# ---------------------------------------------------------------- 排序/去重

def norm_title(t):
    return "".join(ch for ch in (t or "").lower() if ch.isalnum())[:90]


def score(r, q, now_year):
    """引用数 × 时间衰减 + 标题命中加权"""
    age = max(0, now_year - (r.get("year") or now_year))
    decay = 1.0 / (1.0 + 0.28 * age)
    cites = (r.get("citations") or 0) ** 0.5
    terms = [t for t in q.lower().split() if len(t) > 2]
    hits = sum(1 for t in terms if t in (r.get("title") or "").lower())
    return cites * decay + hits * 2.0 + (1.5 if r.get("oa_url") else 0)


def dedupe(rows):
    seen, out = set(), []
    for r in sorted(rows, key=lambda x: -(x.get("citations") or 0)):
        k = norm_title(r.get("title"))
        if k and k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


# ---------------------------------------------------------------- 报告

CSS = """body{font:15px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;
margin:0;background:#0f1115;color:#dfe3ea}
.wrap{max-width:1080px;margin:0 auto;padding:32px 22px 80px}
h1{font-size:26px;margin:0 0 6px;color:#fff}
.sub{color:#8b95a5;font-size:13px;margin-bottom:24px}
.stats{display:flex;gap:12px;flex-wrap:wrap;margin:18px 0 28px}
.card{background:#171b22;border:1px solid #232936;border-radius:10px;padding:12px 16px;min-width:110px}
.card b{display:block;font-size:22px;color:#4da3ff}
.card span{font-size:12px;color:#8b95a5}
.item{background:#151920;border:1px solid #212736;border-radius:12px;padding:16px 18px;margin-bottom:12px}
.item h3{margin:0 0 8px;font-size:16px;color:#eaf0ff;font-weight:600;line-height:1.4}
.meta{font-size:12px;color:#8b95a5;margin-bottom:8px}
.tag{display:inline-block;background:#1e2a3d;color:#7fb6ff;border-radius:5px;
padding:1px 7px;font-size:11px;margin-right:6px}
.abs{font-size:13.5px;color:#aab4c4;margin:8px 0 0}
a{color:#4da3ff;text-decoration:none}a:hover{text-decoration:underline}
.hl{color:#ffd479}"""


def write_report(rows, query, outdir):
    os.makedirs(outdir, exist_ok=True)
    rows = dedupe(rows)
    now_year = datetime.now().year
    rows.sort(key=lambda r: -score(r, query, now_year))

    with open(os.path.join(outdir, "results.json"), "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)

    with open(os.path.join(outdir, "results.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["标题", "年份", "引用", "来源", "期刊/会议", "DOI", "链接", "作者", "开放获取"])
        for r in rows:
            w.writerow([r["title"], r["year"], r["citations"], r["source"], r["venue"],
                        r["doi"], r["url"], "; ".join(r["authors"]),
                        "是" if r["oa_url"] else "否"])

    years = [r["year"] for r in rows if r.get("year")]
    srcs = {}
    for r in rows:
        srcs[r["source"]] = srcs.get(r["source"], 0) + 1
    oa = sum(1 for r in rows if r.get("oa_url"))
    cites = sum(r.get("citations") or 0 for r in rows)

    parts = [f'<!doctype html><html lang="zh"><head><meta charset="utf-8">'
             f'<title>{html.escape(query)} — 情报汇总</title><style>{CSS}</style></head><body>'
             f'<div class="wrap"><h1>{html.escape(query)}</h1>'
             f'<div class="sub">ossint v{VERSION} · 公开数据源聚合 · '
             f'生成于 {datetime.now().strftime("%Y-%m-%d %H:%M")} · '
             f'来源分布 {", ".join(f"{k} {v}" for k, v in sorted(srcs.items()))}</div>'
             f'<div class="stats">'
             f'<div class="card"><b>{len(rows)}</b><span>成果</span></div>'
             f'<div class="card"><b>{cites:,}</b><span>总引用</span></div>'
             f'<div class="card"><b>{oa}</b><span>可免费获取</span></div>'
             f'<div class="card"><b>{min(years) if years else "-"}–{max(years) if years else "-"}</b>'
             f'<span>年份跨度</span></div></div>']

    for i, r in enumerate(rows, 1):
        title = html.escape(r["title"])
        link = r.get("oa_url") or r.get("url") or ""
        meta = [f'<span class="tag">{html.escape(r["source"])}</span>']
        if r.get("year"):
            meta.append(str(r["year"]))
        if r.get("citations"):
            meta.append(f'引用 {r["citations"]:,}')
        if r.get("venue"):
            meta.append(html.escape(r["venue"][:60]))
        abs_text = (r.get("abstract") or "")[:520]
        if abs_text:
            abs_text = html.escape(abs_text) + ("…" if len(r.get("abstract") or "") > 520 else "")
        parts.append(
            f'<div class="item"><h3>{i}. '
            + (f'<a href="{html.escape(link)}" target="_blank" rel="noopener">{title}</a>'
               if link else title)
            + f'</h3><div class="meta">{" · ".join(meta)}</div>'
            + (f'<div class="meta">👤 {html.escape(", ".join(r["authors"][:5]))}'
               f'{" 等" if len(r["authors"]) > 5 else ""}</div>' if r["authors"] else "")
            + (f'<div class="abs">{abs_text}</div>' if abs_text else "")
            + '</div>')

    parts.append('</div></body></html>')
    path = os.path.join(outdir, "report.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(parts))
    return path, rows


# ---------------------------------------------------------------- CLI

def cmd_search(a):
    srcs = [s.strip() for s in a.sources.split(",") if s.strip()]
    for s in srcs:
        if s not in SOURCES:
            sys.exit(f"未知数据源: {s}（可选: {', '.join(SOURCES)}）")
    per = max(1, a.limit // len(srcs))
    print(f"[*] 查询「{a.query}」 数据源={','.join(srcs)} 每源上限={per} 起始年={a.since or '不限'}",
          file=sys.stderr)
    all_rows = []
    for s in srcs:
        try:
            t0 = time.time()
            rows = SOURCES[s](a.query, a.since, a.until, per)
            print(f"    {s}: {len(rows)} 条 ({time.time()-t0:.1f}s)", file=sys.stderr)
            all_rows += rows
        except Exception as e:  # noqa: BLE001
            print(f"    {s}: 失败 {e}", file=sys.stderr)
    rows = dedupe(all_rows)
    if a.min_cites:
        rows = [r for r in rows if (r.get("citations") or 0) >= a.min_cites]
    n = save(rows, a.query)
    print(f"[*] 入库 {n} 条 → {DB}", file=sys.stderr)
    path, final = write_report(rows, a.query, a.out)
    print(f"[+] 报告 {path}  ({len(final)} 条)")
    if final[:5]:
        print("\nTop 5:")
        for i, r in enumerate(final[:5], 1):
            print(f"  {i}. {r['title'][:78]}")
            print(f"     {r.get('year')} · 引用 {r.get('citations', 0)} · {r.get('source')}")


def cmd_report(a):
    rows = load(a.query)
    if not rows:
        sys.exit("本地库没有数据，先跑 search")
    path, final = write_report(rows, a.query or "(全部)", a.out)
    print(f"[+] 报告 {path} ({len(final)} 条)")


def cmd_stats(a):
    c = db()
    total = c.execute("SELECT COUNT(*) FROM works").fetchone()[0]
    if not total:
        print("本地库为空")
        return
    print(f"本地库 {DB}")
    print(f"  总条目 {total}")
    print("  按查询:")
    for q, n in c.execute("SELECT query, COUNT(*) FROM works GROUP BY query ORDER BY 2 DESC LIMIT 10"):
        print(f"    {n:5}  {q}")
    print("  按来源:")
    for s, n in c.execute("SELECT source, COUNT(*) FROM works GROUP BY source ORDER BY 2 DESC"):
        print(f"    {n:5}  {s}")
    r = c.execute("SELECT MIN(year), MAX(year), SUM(citations) FROM works").fetchone()
    print(f"  年份 {r[0]}–{r[1]} · 引用合计 {r[2] or 0:,}")
    c.close()


def main():
    ap = argparse.ArgumentParser(description="ossint —— 公开科研情报采集器")
    ap.add_argument("-v", "--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="检索并生成报告")
    s.add_argument("query")
    s.add_argument("--sources", default="openalex", help="openalex,arxiv,pubmed（逗号分隔）")
    s.add_argument("--since", help="起始年，如 2023")
    s.add_argument("--until", help="截止年")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--min-cites", type=int, default=0)
    s.add_argument("--out", default="./intel")
    s.set_defaults(func=cmd_search)

    r = sub.add_parser("report", help="从本地库重新生成报告")
    r.add_argument("--query", help="只导出该查询的结果")
    r.add_argument("--out", default="./intel")
    r.set_defaults(func=cmd_report)

    st = sub.add_parser("stats", help="本地库概览")
    st.set_defaults(func=cmd_stats)

    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
