#!/usr/bin/env python3
"""抓取 A 股「监管风险事件」公告，落成可回测的事件表。

为什么要单独建这张表
--------------------
平台库里只有 `news_article_enrichment`（Huntly RSS → 情绪/事件标签），
它**只覆盖平台运行期间抓到的新闻**，做不了 10 年回溯；而且新闻不是公告，
「收到立案告知书」这类硬事件在公告里才是最全的。

本脚本走**公告**，按标题关键词归类，产出带**披露日期**的事件表——披露日就是
信息可见日，回测里只能在 T 日之后使用，不构成未来函数。

事件分类（对应 `backend/services/api/news/report_match.py` 的 EVENT_TAG_RULES）
------------------------------------------------------------------------------
    investigation  立案调查   立案 / 立案调查 / 立案告知
    penalty        监管处罚   处罚 / 警示函 / 监管措施 / 通报批评 / 公开谴责 / 违规
    inquiry        问询函     问询函（剔除「回复」类）
    delist_risk    退市风险   退市风险 / 终止上市 / 面值退市

数据源
------
`--source cninfo`（默认）：巨潮资讯**全文检索**，按关键词翻页（每页 100 条，
时间倒序），再在本地按披露日过滤。接口在带 `searchkey` 时会忽略日期参数，
所以不能指望服务端按 `sdate/edate` 切片；关键词 13 个、扫完 10 年约 **10 分钟**。
`--source em`：东方财富公告库逐日全市场（`akshare.stock_notice_report`），
约 5 秒/交易日，10 年 ≈ 2.5 小时；口径更全（含当日全部公告），作为兜底保留。
注意巨潮接口深翻页上限为 200 页（约 2 万条），超出的关键词会记入 `failed`，
默认以退出码 1 结束，需显式 `--allow-partial` 才放行。

产出
----
    data/regulatory/events.parquet
        symbol / announce_date / category / title

用法
----
    python scripts/fetch_regulatory_events.py --start 2016-01-01 --end 2026-11-02
    python scripts/fetch_regulatory_events.py --keywords 立案
    python scripts/fetch_regulatory_events.py --source em --start 2023-01-01
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

OUT = Path("data/regulatory")

RULES = [
    ("investigation", ("立案调查", "立案告知", "立案")),
    (
        "penalty",
        ("行政处罚", "警示函", "监管措施", "通报批评", "公开谴责", "纪律处分", "违规"),
    ),
    ("inquiry", ("问询函",)),
    ("delist_risk", ("退市风险", "终止上市", "面值退市")),
]
# 标题里出现这些词的多为「回复/IPO 审核」，不是新增风险事件
NOISE = ("回复", "审核问询", "审查问询", "保荐", "问核", "核查意见", "法律意见书")

# ---- 巨潮全文检索 ----------------------------------------------------------
# 检索是全文级的，命中不代表标题命中（比如「年报」里提到立案），
# 因此仍要过 classify()；反过来标题里写「调查通知书」不含「立案」的也要捞，
# 所以关键词用一组而不是单个。
#
# 2026-11 实测：searchkey 与 sdate/edate 同时传会返回 0 条（接口已不认旧参数），
# 只带 searchkey 时按时间倒序返回全部命中。因此这里翻页后在本地按时间过滤。
CNINFO_URL = "http://www.cninfo.com.cn/new/fulltextSearch/full"
CNINFO_KEYWORDS = (
    "立案",
    "调查通知书",
    "行政处罚",
    "处罚决定",
    "警示函",
    "监管措施",
    "通报批评",
    "公开谴责",
    "纪律处分",
    "违规",
    "退市风险警示",
    "终止上市",
    "问询函",
)
CNINFO_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": "http://www.cninfo.com.cn/new/fulltextSearch",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}
CNINFO_PAGE_SIZE = 100  # 接口上限，传更大也只回 100 条
CNINFO_MAX_PAGES = 200  # 服务端深翻页上限：第 201 页直接 HTTP 500
CNINFO_SLEEP = 0.1  # 翻页间隔，别把对方打挂


def classify(title: str) -> str | None:
    if not title or any(n in title for n in NOISE):
        return None
    for cat, kws in RULES:
        if any(k in title for k in kws):
            return cat
    return None


def to_symbol(code: str) -> str | None:
    c = str(code).strip().zfill(6)
    if not c.isdigit() or len(c) != 6:
        return None
    if c[0] == "6":
        return f"{c}.SH"
    if c[0] in "03":
        return f"{c}.SZ"
    if c[:2] in ("43", "83", "87", "88", "92"):
        return f"{c}.BJ"
    return None


def trading_days(con, start: str, end: str) -> list[str]:
    rows = con.execute(
        "SELECT DISTINCT dt FROM kb WHERE dt >= ? AND dt <= ? ORDER BY dt",
        [int(start.replace("-", "")), int(end.replace("-", ""))],
    ).fetchall()
    return [str(r[0]) for r in rows]


def _clean_title(title: str) -> str:
    return (
        str(title)
        .replace("<em>", "")
        .replace("</em>", "")
        .replace("\u3000", " ")
        .strip()
    )


def _get_json(params: dict, retries: int = 4) -> dict:
    url = f"{CNINFO_URL}?{urllib.parse.urlencode(params)}"
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=CNINFO_HEADERS)
            with urllib.request.urlopen(req, timeout=25) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - 网络抖动重试
            last = exc
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"巨潮检索失败：{type(last).__name__}") from last


def fetch_cninfo(
    keywords: tuple[str, ...], start: str, end: str
) -> tuple[pd.DataFrame, list[str]]:
    """按关键词翻页（时间倒序），再在本地按披露日过滤。

    巨潮接口在带 ``searchkey`` 时会忽略 ``sdate`` / ``edate``（实测返回 0 条），
    因此不能靠服务端筛日期：这里一直翻到本页最老的公告早于 ``start`` 为止。
    """
    lo_day = int(start.replace("-", ""))
    hi_day = int(end.replace("-", ""))
    rows: list[dict] = []
    seen: set[str] = set()
    failed: list[str] = []
    for kw in keywords:
        page, pages, got = 1, None, 0
        while page <= CNINFO_MAX_PAGES:
            try:
                js = _get_json(
                    {"searchkey": kw, "pageNum": page, "pageSize": CNINFO_PAGE_SIZE}
                )
            except RuntimeError as exc:
                failed.append(f"{kw}/p{page}: {exc}")
                break
            anns = js.get("announcements") or []
            if pages is None:
                pages = int(js.get("totalpages") or 0)
            if not anns:
                # 有 totalpages 却回空页 = 接口异常，不能当成「翻完了」
                if pages:
                    failed.append(f"{kw}/p{page}: 返回空页（totalpages={pages}）")
                break
            oldest = 99_999_999
            for a in anns:
                ms = a.get("announcementTime")
                if not ms:
                    continue
                day = int(
                    pd.Timestamp(int(ms), unit="ms", tz="UTC")
                    .tz_convert("Asia/Shanghai")
                    .strftime("%Y%m%d")
                )
                oldest = min(oldest, day)
                if day < lo_day or day > hi_day:
                    continue
                aid = str(a.get("announcementId") or "")
                title = _clean_title(
                    a.get("announcementTitle") or a.get("shortTitle") or ""
                )
                cat = classify(title)
                sym = to_symbol(a.get("secCode") or "")
                if not aid or aid in seen or not cat or not sym:
                    continue
                seen.add(aid)
                rows.append(
                    {
                        "symbol": sym,
                        "announce_date": day,
                        "category": cat,
                        "title": title,
                    }
                )
            got += len(anns)
            if page == 1 or page % 10 == 0 or oldest < lo_day:
                print(
                    f"  {kw:<6} p{page}/{pages or '?'} got={got} "
                    f"kept={len(rows)} oldest={oldest}",
                    flush=True,
                )
            if oldest < lo_day:
                break
            if pages and page >= pages:
                break
            page += 1
            time.sleep(CNINFO_SLEEP)
        if pages and pages > CNINFO_MAX_PAGES:
            failed.append(
                f"{kw}: totalpages={pages}，服务端只放前 {CNINFO_MAX_PAGES} 页，"
                f"最多 {CNINFO_MAX_PAGES * CNINFO_PAGE_SIZE} 条"
            )
    print()
    df = pd.DataFrame(rows, columns=["symbol", "announce_date", "category", "title"])
    if df.empty:
        failed.append("所有关键词均未取到事件（接口可能变更，或日期区间为空）")
        return df, failed
    df = df.drop_duplicates(["symbol", "announce_date", "category", "title"])
    return df.sort_values(["announce_date", "symbol"]).reset_index(drop=True), failed


def fetch_eastmoney(days: list[str], frames: list[pd.DataFrame]) -> list[pd.DataFrame]:
    """东财口径：逐交易日全市场公告（需要 akshare，慢但最全）。"""
    import akshare as ak

    for i, d in enumerate(days, 1):
        try:
            df = ak.stock_notice_report(symbol="全部", date=d)
        except Exception as exc:  # noqa: BLE001 - 单日失败不应中断整轮
            print(f"  {d} 拉取失败：{type(exc).__name__}，跳过")
            time.sleep(1.0)
            continue
        if df is None or df.empty:
            continue
        df["category"] = df["公告标题"].map(classify)
        hit = df[df.category.notna()].copy()
        if hit.empty:
            continue
        hit["symbol"] = hit["代码"].map(to_symbol)
        hit = hit.dropna(subset=["symbol"])
        hit["announce_date"] = d
        frames.append(
            hit[["symbol", "announce_date", "category", "公告标题"]].rename(
                columns={"公告标题": "title"}
            )
        )
        if i % 25 == 0:
            print(f"  {i}/{len(days)} 天，累计 {len(frames)} 段", flush=True)
    return frames


def _merge_old(path: Path, new: pd.DataFrame) -> pd.DataFrame:
    if not path.exists():
        return new
    old = pd.read_parquet(path)
    out = pd.concat([old, new], ignore_index=True)
    out["announce_date"] = pd.to_numeric(out.announce_date, errors="coerce")
    out = out.dropna(subset=["symbol", "announce_date"])
    out["announce_date"] = out.announce_date.astype(int)
    return out.drop_duplicates(["symbol", "announce_date", "category", "title"])


def main() -> int:
    ap = argparse.ArgumentParser(description="抓取监管风险事件公告")
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--end", default="2026-10-23")
    ap.add_argument("--data-dir", default="/opt/quantmind/data/quantdb")
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--source", default="cninfo", choices=["cninfo", "em"])
    ap.add_argument(
        "--keywords",
        default=",".join(CNINFO_KEYWORDS),
        help="巨潮全文检索关键词（逗号分隔）",
    )
    ap.add_argument(
        "--allow-partial",
        action="store_true",
        help="抓取切片失败/被截断时仍按成功退出（默认 fail-closed）",
    )
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / "events.parquet"

    if args.source == "cninfo":
        kws = tuple(k.strip() for k in args.keywords.split(",") if k.strip())
        print(f"巨潮全文检索：{args.start} ~ {args.end}，关键词 {kws}")
        new, failed = fetch_cninfo(kws, args.start, args.end)
        if new.empty:
            # 空表不能覆盖旧数据：宁可失败退出，也不要写出一个「看起来正常」的空文件
            print("❌ 一条事件都没取到，本次不写入；先检查接口或关键词。")
            for f in failed[:10]:
                print("   ", f)
            return 1
        allx = _merge_old(path, new)
        allx.to_parquet(path, index=False)
        print(
            f"事件表已写入 {path}：{len(allx)} 条 / {allx.symbol.nunique()} 只 / "
            f"{allx.announce_date.nunique()} 个披露日"
        )
        print(allx.category.value_counts().to_string())
        if failed:
            print(f"⚠️ {len(failed)} 个切片失败或不完整，事件表可能有缺漏：")
            for f in failed[:10]:
                print("   ", f)
            if not args.allow_partial:
                print("   已写入当前结果，但退出码为 1；确认可用请加 --allow-partial")
                return 1
        return 0

    import duckdb

    con = duckdb.connect()
    con.execute(
        "CREATE VIEW kb AS SELECT dt FROM read_parquet("
        f"'{args.data_dir}/1_kline_data/daily_backward/**/*.parquet')"
    )
    days = trading_days(con, args.start, args.end)
    print(f"待扫描交易日 {len(days)} 个：{days[0]} ~ {days[-1]}")
    frames: list[pd.DataFrame] = []
    if path.exists():  # 断点续跑
        old = pd.read_parquet(path)
        frames.append(old)
        done = set(old.announce_date.astype(str).unique())
        days = [d for d in days if d not in done]
        print(f"已有 {len(old)} 条事件，跳过 {len(done)} 个交易日")
    try:
        fetch_eastmoney(days, frames)
    finally:
        if frames:
            allx = _merge_old(path, pd.concat(frames, ignore_index=True))
            allx.to_parquet(path, index=False)
            print(
                f"事件表已写入 {path}：{len(allx)} 条 / "
                f"{allx.symbol.nunique()} 只 / {allx.announce_date.nunique()} 个交易日"
            )
            print(allx.category.value_counts().to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
