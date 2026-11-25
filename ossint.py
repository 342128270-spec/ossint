#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ossint —— 公开科研/技术情报采集器 + 共享本地情报库

采集：OpenAlex / arXiv / PubMed（全公开、免费、无需 key）
存储：**共享 SQLite 库**，多智能体并发安全（WAL + busy_timeout），支持全文检索
输出：report.html / results.csv / results.json / jsonl 导出

零第三方依赖，Python 3.9+ 标准库。

库位置解析顺序：
  1. --db 参数
  2. $OSSINT_DB
  3. <工作区>/shared/intel/intel.db（工作区 = $OPENCLAW_WORKSPACE 或含 AGENTS.md 的目录）
  4. ~/.ossint/index.db

用法:
  python3 ossint.py search "protein language model" --since 2023
  python3 ossint.py find "crispr" --limit 10          # 查本地库（不联网）
  python3 ossint.py export --format jsonl > dump.jsonl
  python3 ossint.py import dump.jsonl
  python3 ossint.py stats | doctor | schema
"""
import argparse, csv, hashlib, html, json, os, sqlite3, sys, time
import urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

VERSION = "0.2.0"
SCHEMA_VERSION = 2
UA = f"ossint/{VERSION} (open-source research aggregator; contact: local-user)"
LOCK_RETRIES = 6


# ---------------------------------------------------------------- 库路径

def find_workspace():
    env = os.environ.get("OPENCLAW_WORKSPACE")
    if env and os.path.isdir(env):
        return env
    cur = os.path.abspath(os.getcwd())
    for _ in range(6):
        if os.path.exists(os.path.join(cur, "AGENTS.md")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def resolve_db(explicit=None):
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    env = os.environ.get("OSSINT_DB")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    ws = find_workspace()
    if ws:
        return os.path.join(ws, "shared", "intel", "intel.db")
    return os.path.expanduser("~/.ossint/index.db")


DB = resolve_db()


# ---------------------------------------------------------------- 网络

def fetch(url, tries=3, timeout=25):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
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
    if not inv_idx:
        return ""
    slots = []
    for word, positions in inv_idx.items():
        for p in positions:
            slots.append((p, word))
    slots.sort()
    return " ".join(w for _, w in slots)


def make_uid(source, raw):
    return f"{source}:" + hashlib.sha1((raw or "").encode("utf-8", "replace")).hexdigest()[:20]


# ---------------------------------------------------------------- 源: OpenAlex

def src_openalex(q, since, until, limit):
    out, cursor, per = [], "*", 200
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
            doi = (w.get("doi") or "").replace("https://doi.org/", "")
            out.append(rec(
                uid=make_uid("openalex", (w.get("doi") or w.get("id") or "")),
                source="openalex",
                title=w.get("title") or "(无标题)",
                year=w.get("publication_year"),
                doi=doi,
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
    params = {"search_query": f"all:{q}", "start": 0,
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
            uid=make_uid("arxiv", aid), source="arxiv", title=title or "(无标题)", year=year,
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
                   + urllib.parse.urlencode({"db": "pubmed", "term": term, "retmode": "json",
                                             "retmax": str(min(limit, 200)),
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
        pd = d.get("pubdate") or ""
        year = int(pd[:4]) if pd[:4].isdigit() else None
        out.append(rec(
            uid=make_uid("pubmed", pid), source="pubmed",
            title=(d.get("title") or "").rstrip("."), year=year,
            doi=next((x["value"] for x in (d.get("articleids") or [])
                      if x.get("idtype") == "doi"), ""),
            url=f"https://pubmed.ncbi.nlm.nih.gov/{pid}/",
            venue=d.get("fulljournalname") or d.get("source") or "",
            authors=[a.get("name", "") for a in (d.get("authors") or [])][:12],
            citations=0, abstract="", oa_url="",
            concepts=[x for x in (d.get("pubtype") or [])][:6],
        ))
    return out


SOURCES = {"openalex": src_openalex, "arxiv": src_arxiv, "pubmed": src_pubmed}


# ---------------------------------------------------------------- 共享库

DDL = [
    """CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)""",
    """CREATE TABLE IF NOT EXISTS works(
        uid TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT NOT NULL, year INT,
        doi TEXT, url TEXT, venue TEXT, authors TEXT, citations INT DEFAULT 0,
        abstract TEXT, oa_url TEXT, concepts TEXT,
        added_at TEXT, updated_at TEXT, added_by TEXT)""",
    """CREATE INDEX IF NOT EXISTS idx_works_year ON works(year)""",
    """CREATE INDEX IF NOT EXISTS idx_works_source ON works(source)""",
    """CREATE INDEX IF NOT EXISTS idx_works_cites ON works(citations DESC)""",
    """CREATE TABLE IF NOT EXISTS queries(
        qid INTEGER PRIMARY KEY AUTOINCREMENT, query TEXT NOT NULL, source TEXT,
        n_results INT, created_at TEXT, created_by TEXT)""",
    """CREATE INDEX IF NOT EXISTS idx_queries_q ON queries(query)""",
    """CREATE TABLE IF NOT EXISTS work_queries(
        uid TEXT NOT NULL, qid INTEGER NOT NULL, PRIMARY KEY(uid, qid))""",
    """CREATE TABLE IF NOT EXISTS tags(
        uid TEXT NOT NULL, tag TEXT NOT NULL, PRIMARY KEY(uid, tag))""",
    """CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag)""",
]

FTS_DDL = [
    """CREATE VIRTUAL TABLE IF NOT EXISTS works_fts USING fts5(
        title, abstract, content='works', content_rowid='rowid',
        tokenize='unicode61 remove_diacritics 2')""",
    """CREATE TRIGGER IF NOT EXISTS works_ai AFTER INSERT ON works BEGIN
        INSERT INTO works_fts(rowid, title, abstract)
        VALUES (new.rowid, new.title, new.abstract);
    END""",
    """CREATE TRIGGER IF NOT EXISTS works_ad AFTER DELETE ON works BEGIN
        INSERT INTO works_fts(works_fts, rowid, title, abstract)
        VALUES ('delete', old.rowid, old.title, old.abstract);
    END""",
    """CREATE TRIGGER IF NOT EXISTS works_au AFTER UPDATE ON works BEGIN
        INSERT INTO works_fts(works_fts, rowid, title, abstract)
        VALUES ('delete', old.rowid, old.title, old.abstract);
        INSERT INTO works_fts(rowid, title, abstract)
        VALUES (new.rowid, new.title, new.abstract);
    END""",
]


def connect(path=None, readonly=False):
    path = path or DB
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    c = sqlite3.connect(path, timeout=15, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=15000")
    if not readonly:
        try:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            pass
    return c


# v0.1 → v0.2 需要补的列（旧库没有 added_at/updated_at/added_by）
MIGRATE_COLS = [
    ("added_at", "TEXT"),
    ("updated_at", "TEXT"),
    ("added_by", "TEXT"),
    ("citations", "INT DEFAULT 0"),
]


def migrate(c):
    """把任意旧版本的库就地升级到当前 schema；重复执行安全。"""
    cols = {r[1] for r in c.execute("PRAGMA table_info(works)")} if \
        c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='works'").fetchone() \
        else set()
    if not cols:
        return False  # 空库，交给 DDL 建
    legacy_cols = {r[1] for r in c.execute("PRAGMA table_info(works)")}
    changed = False
    for name, ddl in MIGRATE_COLS:
        if name not in legacy_cols:
            c.execute(f"ALTER TABLE works ADD COLUMN {name} {ddl}")
            changed = True
    if not changed:
        return False
    # 旧库的 FTS 影子表常与主表对不齐（UPDATE 会报 malformed），
    # 先拆掉 FTS 与触发器，交给 init_db 用当前 DDL 重建后 rebuild。
    for trg in ("works_ai", "works_ad", "works_au"):
        try:
            c.execute(f"DROP TRIGGER IF EXISTS {trg}")
        except sqlite3.Error:
            pass
    try:
        c.execute("DROP TABLE IF EXISTS works_fts")
    except sqlite3.Error as e:
        print(f"[warn] 旧 FTS 表拆除失败（将重建）: {e}", file=sys.stderr)
    # 旧库的 fetched_at / query 语义迁移到新列
    if "fetched_at" in legacy_cols:
        c.execute("UPDATE works SET added_at=fetched_at, updated_at=fetched_at "
                  "WHERE added_at IS NULL OR updated_at IS NULL")
    c.execute("UPDATE works SET added_by='migrated-v0.1' WHERE added_by IS NULL")
    c.execute("UPDATE works SET citations=0 WHERE citations IS NULL")
    print("[*] 库已从旧版升级到 schema v%d" % SCHEMA_VERSION, file=sys.stderr)
    return True


def init_db(c):
    for stmt in DDL:
        c.execute(stmt)
    migrated = migrate(c)
    try:
        for stmt in FTS_DDL:
            c.execute(stmt)
    except sqlite3.Error as e:
        print(f"[warn] FTS5 不可用，全文检索将退化为 LIKE: {e}", file=sys.stderr)
    row = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    old_ver = int(row[0]) if row and str(row[0]).isdigit() else 0
    if migrated or old_ver < SCHEMA_VERSION:
        try:  # 索引与主表对不齐时重建
            c.execute("INSERT INTO works_fts(works_fts) VALUES('rebuild')")
        except sqlite3.Error:
            pass
    c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version',?)",
              (str(SCHEMA_VERSION),))
    c.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('created_at',?)",
              (datetime.now(timezone.utc).isoformat(timespec="seconds"),))


def retry_lock(fn, *a, **kw):
    """并发写入时遇到 database is locked 自动重试"""
    last = None
    for i in range(LOCK_RETRIES):
        try:
            return fn(*a, **kw)
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                raise
            last = e
            time.sleep(0.4 * (i + 1))
    raise last


def upsert(rows, query=None, source=None, actor=None):
    """写入共享库；已存在则更新引用数（取较大值）与最近时间戳"""
    c = connect()
    try:
        init_db(c)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        qid = None
        if query:
            cur = c.execute("INSERT INTO queries(query,source,n_results,created_at,created_by)"
                            " VALUES(?,?,?,?,?)", (query, source, len(rows), now, actor))
            qid = cur.lastrowid
        added = updated = 0
        retry_lock(lambda: c.execute("BEGIN IMMEDIATE"))
        try:
            for r in rows:
                exist = c.execute("SELECT citations FROM works WHERE uid=?",
                                  (r["uid"],)).fetchone()
                if exist:
                    c.execute("""UPDATE works SET title=?, year=?, doi=?, url=?, venue=?,
                        authors=?, citations=MAX(COALESCE(citations,0),?), abstract=?,
                        oa_url=?, concepts=?, updated_at=? WHERE uid=?""",
                              (r["title"], r["year"], r["doi"], r["url"], r["venue"],
                               json.dumps(r["authors"], ensure_ascii=False),
                               r["citations"], r["abstract"], r["oa_url"],
                               json.dumps(r["concepts"], ensure_ascii=False), now, r["uid"]))
                    updated += 1
                else:
                    c.execute("""INSERT INTO works(uid,source,title,year,doi,url,venue,
                        authors,citations,abstract,oa_url,concepts,added_at,updated_at,added_by)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                              (r["uid"], r["source"], r["title"], r["year"], r["doi"],
                               r["url"], r["venue"],
                               json.dumps(r["authors"], ensure_ascii=False),
                               r["citations"], r["abstract"], r["oa_url"],
                               json.dumps(r["concepts"], ensure_ascii=False),
                               now, now, actor))
                    added += 1
                if qid:
                    c.execute("INSERT OR IGNORE INTO work_queries(uid,qid) VALUES(?,?)",
                              (r["uid"], qid))
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
        return added, updated, qid
    finally:
        c.close()


def row_to_rec(row):
    d = dict(row)
    d["authors"] = json.loads(d.get("authors") or "[]")
    d["concepts"] = json.loads(d.get("concepts") or "[]")
    return d


CJK = lambda s: any("\u4e00" <= ch <= "\u9fff" for ch in s)  # noqa: E731


def local_find(q, limit=30, since=None, source=None, min_cites=0):
    c = connect(readonly=True)
    try:
        init_db(c) if os.path.exists(DB) else None
        params, where = [], []
        if q and not CJK(q):
            try:
                sql = ("SELECT w.* FROM works_fts f JOIN works w ON w.rowid=f.rowid "
                       "WHERE works_fts MATCH ?")
                params.append(" ".join(t + "*" for t in q.split() if len(t) > 1))
                if since:
                    sql += " AND w.year>=?"; params.append(int(since))
                if source:
                    sql += " AND w.source=?"; params.append(source)
                if min_cites:
                    sql += " AND w.citations>=?"; params.append(min_cites)
                sql += " ORDER BY bm25(works_fts) LIMIT ?"
                params.append(limit)
                return [row_to_rec(r) for r in c.execute(sql, params)]
            except sqlite3.Error:
                pass  # FTS 缺失或语法问题，退回 LIKE
        sql = "SELECT * FROM works WHERE 1=1"
        if q:
            sql += " AND (title LIKE ? OR abstract LIKE ?)"
            params += [f"%{q}%", f"%{q}%"]
        if since:
            sql += " AND year>=?"; params.append(int(since))
        if source:
            sql += " AND source=?"; params.append(source)
        if min_cites:
            sql += " AND citations>=?"; params.append(min_cites)
        sql += " ORDER BY citations DESC, year DESC LIMIT ?"
        params.append(limit)
        return [row_to_rec(r) for r in c.execute(sql, params)]
    finally:
        c.close()


# ---------------------------------------------------------------- 排序/去重

def norm_title(t):
    return "".join(ch for ch in (t or "").lower() if ch.isalnum())[:90]


def score(r, q, now_year):
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
a{color:#4da3ff;text-decoration:none}a:hover{text-decoration:underline}"""


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
             f'<div class="sub">ossint v{VERSION} · 共享库 {html.escape(DB)} · '
             f'{datetime.now().strftime("%Y-%m-%d %H:%M")} · '
             f'{", ".join(f"{k} {v}" for k, v in sorted(srcs.items()))}</div>'
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

def actor_name(a):
    return a.agent or os.environ.get("OSSINT_AGENT") or os.environ.get("OPENCLAW_AGENT_ID") or "unknown"


def cmd_search(a):
    srcs = [s.strip() for s in a.sources.split(",") if s.strip()]
    for s in srcs:
        if s not in SOURCES:
            sys.exit(f"未知数据源: {s}（可选: {', '.join(SOURCES)}）")
    per = max(1, a.limit // len(srcs))
    print(f"[*] 查询「{a.query}」 sources={','.join(srcs)} 每源={per} since={a.since or '不限'}",
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
    added, updated, qid = upsert(rows, a.query, ",".join(srcs), actor_name(a))
    print(f"[*] 共享库 {DB}: 新增 {added} / 更新 {updated} / 查询#{qid}", file=sys.stderr)
    path, final = write_report(rows, a.query, a.out)
    print(f"[+] 报告 {path}  ({len(final)} 条)")
    for i, r in enumerate(final[:5], 1):
        print(f"  {i}. {r['title'][:78]}")
        print(f"     {r.get('year')} · 引用 {r.get('citations', 0)} · {r.get('source')}")


def cmd_find(a):
    rows = local_find(a.query, a.limit, a.since, a.source, a.min_cites)
    if not rows:
        print("本地库无匹配")
        return
    for i, r in enumerate(rows, 1):
        print(f"{i:3}. {r['title'][:76]}")
        print(f"     {r.get('year')} · 引用 {r.get('citations', 0)} · {r['source']}"
              + (f" · {r['doi']}" if r.get("doi") else ""))
    print(f"\n共 {len(rows)} 条（库 {DB}）")


def cmd_export(a):
    rows = local_find(a.query or "", 10_000_000, a.since, a.source, a.min_cites)
    out = sys.stdout
    fh = None
    if a.output:
        fh = open(a.output, "w", encoding="utf-8")
        out = fh
    try:
        if a.format == "jsonl":
            for r in rows:
                out.write(json.dumps({k: r.get(k) for k in REC_FIELDS + ["added_at", "updated_at"]},
                                     ensure_ascii=False) + "\n")
        elif a.format == "json":
            json.dump(rows, out, ensure_ascii=False, indent=2)
        else:
            w = csv.writer(out)
            w.writerow(["uid", "source", "title", "year", "citations", "doi", "url", "authors"])
            for r in rows:
                w.writerow([r["uid"], r["source"], r["title"], r["year"], r["citations"],
                            r["doi"], r["url"], "; ".join(r["authors"])])
    finally:
        if fh:
            fh.close()
    print(f"[+] 导出 {len(rows)} 条 → {a.output or 'stdout'}", file=sys.stderr)


def cmd_import(a):
    rows = []
    with open(a.file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "title" not in d or "uid" not in d:
                continue
            rows.append(rec(**{k: d.get(k) for k in REC_FIELDS}))
    added, updated, _ = upsert(rows, a.query, "import", actor_name(a))
    print(f"[+] 导入 {len(rows)} 条：新增 {added} / 更新 {updated} → {DB}")


def cmd_stats(a):
    c = connect(readonly=True)
    try:
        total = c.execute("SELECT COUNT(*) FROM works").fetchone()[0]
    except sqlite3.Error:
        sys.exit("库还没初始化，先跑一次 search")
    print(f"共享库 {DB}")
    print(f"  条目 {total} · 大小 {os.path.getsize(DB)/1024:.0f} KB")
    print("  按来源:")
    for s, n in c.execute("SELECT source,COUNT(*) FROM works GROUP BY source ORDER BY 2 DESC"):
        print(f"    {n:6}  {s}")
    print("  最近查询:")
    for q, n, t in c.execute("SELECT query,n_results,created_at FROM queries "
                             "ORDER BY qid DESC LIMIT 8"):
        print(f"    {t}  {n:5}  {q}")
    r = c.execute("SELECT MIN(year),MAX(year),SUM(citations) FROM works").fetchone()
    print(f"  年份 {r[0]}–{r[1]} · 引用合计 {r[2] or 0:,}")
    print("  写入方:")
    for ag, n in c.execute("SELECT COALESCE(added_by,'?'),COUNT(*) FROM works "
                           "GROUP BY 1 ORDER BY 2 DESC"):
        print(f"    {n:6}  {ag}")
    c.close()


def cmd_doctor(a):
    print(f"库路径 {DB}")
    print(f"存在  {os.path.exists(DB)}")
    if not os.path.exists(DB):
        sys.exit("库不存在，先跑 search")
    c = connect()
    init_db(c)
    print("  完整性", c.execute("PRAGMA integrity_check").fetchone()[0])
    print("  日志模式", c.execute("PRAGMA journal_mode").fetchone()[0])
    try:
        print("  FTS5  可用")
        print("  FTS 行数", c.execute("SELECT COUNT(*) FROM works_fts").fetchone()[0])
    except sqlite3.Error as e:
        print("  FTS5  不可用", e)
    print("  写锁测试", end=" ")
    try:
        retry_lock(lambda: c.execute("BEGIN IMMEDIATE"))
        c.execute("COMMIT")
        print("通过（并发写入安全）")
    except sqlite3.Error as e:
        print("失败", e)
    c.close()


def cmd_schema(a):
    c = connect(readonly=True)
    print(f"-- ossint schema v{c.execute('SELECT value FROM meta WHERE key=?', ('schema_version',)).fetchone()[0]} @ {DB}")
    for (sql,) in c.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL "
                            "AND name NOT LIKE 'sqlite_%'"):
        print(sql, ";\n")
    c.close()


def cmd_tag(a):
    c = connect()
    init_db(c)
    if a.add:
        for t in a.add.split(","):
            c.execute("INSERT OR IGNORE INTO tags(uid,tag) VALUES(?,?)", (a.uid, t.strip()))
        print(f"[+] 已给 {a.uid} 打标签 {a.add}")
    if a.list:
        for (t,) in c.execute("SELECT tag FROM tags WHERE uid=?", (a.uid,)):
            print(t)
    c.close()


def main():
    ap = argparse.ArgumentParser(description="ossint —— 公开科研情报采集器 / 共享情报库")
    ap.add_argument("-v", "--version", action="version", version=VERSION)
    ap.add_argument("--db", help="指定数据库路径（默认共享库）")
    ap.add_argument("--agent", help="写入者标识（默认取 $OSSINT_AGENT / $OPENCLAW_AGENT_ID）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="联网检索并写入共享库")
    s.add_argument("query")
    s.add_argument("--sources", default="openalex")
    s.add_argument("--since")
    s.add_argument("--until")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--min-cites", type=int, default=0)
    s.add_argument("--out", default="./intel")
    s.set_defaults(func=cmd_search)

    f = sub.add_parser("find", help="查本地共享库（不联网）")
    f.add_argument("query")
    f.add_argument("--limit", type=int, default=20)
    f.add_argument("--since")
    f.add_argument("--source")
    f.add_argument("--min-cites", type=int, default=0)
    s2 = f
    s2.set_defaults(func=cmd_find)

    e = sub.add_parser("export", help="导出共享库")
    e.add_argument("--format", choices=["jsonl", "json", "csv"], default="jsonl")
    e.add_argument("--output")
    e.add_argument("--query")
    e.add_argument("--since")
    e.add_argument("--source")
    e.add_argument("--min-cites", type=int, default=0)
    e.set_defaults(func=cmd_export)

    i = sub.add_parser("import", help="从 jsonl 导入共享库")
    i.add_argument("file")
    i.add_argument("--query")
    i.set_defaults(func=cmd_import)

    st = sub.add_parser("stats", help="库概览")
    st.set_defaults(func=cmd_stats)

    d = sub.add_parser("doctor", help="库体检（完整性/锁/FTS）")
    d.set_defaults(func=cmd_doctor)

    sc = sub.add_parser("schema", help="打印表结构")
    sc.set_defaults(func=cmd_schema)

    tg = sub.add_parser("tag", help="给条目打标签")
    tg.add_argument("uid")
    tg.add_argument("--add")
    tg.add_argument("--list", action="store_true")
    tg.set_defaults(func=cmd_tag)

    a = ap.parse_args()
    global DB
    DB = resolve_db(a.db)
    a.func(a)


if __name__ == "__main__":
    main()
