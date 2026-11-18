#!/usr/bin/env python3
"""quantdb_fetch.py —— 从魔搭（ModelScope）按数据集下载 QuantDB parquet，支持断点续传。

与仓库内 backend/services/engine/data_platform/modelscope_dataset_sync.py 同一套
远端协议（tree 清单 + FilePath 走 302 拿 CDN 签名地址），但独立成单文件脚本，
只依赖 httpx，可直接在任意 Python 环境运行，便于换机后补数据。

断点续传规则：
  * 本地已有文件且大小 == 远端大小，且本地状态库记录的 sha256 == 远端 sha256 → 跳过；
  * 状态库缺失但文件大小一致 → 本地算一次 sha256 确认，一致则登记后跳过；
  * 其余情况（缺失 / 大小不符 / 校验不符）→ 重新下载到 .part，校验通过后原子替换。

示例：
  python tools/quantdb_fetch.py --group base --root data/quantdb --dry-run
  python tools/quantdb_fetch.py --group base,l1 --root data/quantdb
  python tools/quantdb_fetch.py --group all --root data/quantdb --workers 8
  python tools/quantdb_fetch.py --verify --group base,l1 --root data/quantdb
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote

import httpx

DEFAULT_ENDPOINT = "https://www.modelscope.cn"
DEFAULT_REPO = "qusong0627/LightGBM_Alpha300"
DEFAULT_REVISION = "master"
PAGE_SIZE = 1000
TREE_RETRIES = 4
DOWNLOAD_RETRIES = 4
STATE_FILE = ".fetch_state.json"

# rel_dir -> dataset 名（与 backend/shared/quantdb_datasets.py 保持一致）
DATASETS: dict[str, str] = {
    "1_kline_data/daily_forward": "daily_forward",
    "1_kline_data/daily_backward": "daily_backward",
    "1_kline_data/daily_unadjusted": "daily_unadjusted",
    "1_kline_data/index_daily": "index_daily",
    "2_base_sector/instrument_detail": "instrument_detail",
    "2_base_sector/sector_concept": "sector_concept",
    "2_base_sector/index_weights": "index_weights",
    "2_base_sector/trading_calendar": "trading_calendar",
    "2_base_sector/margin_trading": "margin_trading",
    "3_financial_data/balance": "balance",
    "3_financial_data/income": "income",
    "3_financial_data/cashflow": "cashflow",
    "3_financial_data/capital": "capital",
    "3_financial_data/pershare_index": "pershare_index",
    "3_financial_data/per_share": "per_share",
    "3_financial_data/dividend_factors": "dividend_factors",
    "3_financial_data/holder_num": "holder_num",
    "4_bond_etf/etf_pcf": "etf_pcf",
    "4_bond_etf/convertible_bond": "convertible_bond",
    "5_technical_derived/valuation": "valuation",
    "5_technical_derived/technical_indicators": "technical_indicators",
    "5_technical_derived/market_sentiment": "market_sentiment",
    "6_ml_datasets/features_daily": "features_daily",
    "6_ml_datasets/l1_factors": "l1_factors",
    "6_ml_datasets/l1_l2_factors": "l1_l2_factors",
    "6_ml_datasets/factor_dictionary": "factor_dictionary",
}

# 预设分组：基础数据 = 日线行情 + 板块基础 + 财务；L1 = 预计算 L1 因子
GROUPS: dict[str, list[str]] = {
    "base": [
        "daily_forward",
        "daily_backward",
        "daily_unadjusted",
        "index_daily",
        "instrument_detail",
        "sector_concept",
        "index_weights",
        "trading_calendar",
        "margin_trading",
        "balance",
        "income",
        "cashflow",
        "capital",
        "pershare_index",
        "per_share",
        "dividend_factors",
        "holder_num",
    ],
    "l1": ["l1_factors"],
    "l2": ["l1_l2_factors"],
    "tech": ["valuation", "technical_indicators", "market_sentiment"],
    "features": ["features_daily"],
}

STATE_LOCK = threading.Lock()


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def dataset_of(path: str) -> str | None:
    for rel_dir, name in DATASETS.items():
        if path.startswith(rel_dir + "/"):
            return name
    return None


def fetch_json(client: httpx.Client, url: str, params: dict, retries: int = TREE_RETRIES):
    last: Exception | None = None
    for attempt in range(retries):
        try:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            body = resp.json()
            if body.get("Code") != 200:
                raise RuntimeError(f"tree API 失败: {body.get('Message') or body.get('Code')}")
            return body.get("Data") or {}
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise last  # type: ignore[misc]


def list_remote(client: httpx.Client, endpoint: str, repo: str, revision: str) -> list[dict]:
    url = f"{endpoint}/api/v1/datasets/{repo}/repo/tree"
    base = {"Revision": revision, "Recursive": "true", "PageSize": PAGE_SIZE}

    first = fetch_json(client, url, {**base, "PageNumber": 1})
    files = list(first.get("Files") or [])
    total = first.get("TotalCount")
    pages = max(1, (int(total) + PAGE_SIZE - 1) // PAGE_SIZE) if total else 1
    log(f"远端清单：{total if total is not None else '?'} 个条目，{pages} 页")

    def _page(n: int) -> list[dict]:
        data = fetch_json(client, url, {**base, "PageNumber": n})
        return list(data.get("Files") or [])

    if pages > 1:
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(_page, n): n for n in range(2, pages + 1)}
            for fut in as_completed(futures):
                files.extend(fut.result())
    return files


def sha256_file(path: Path, chunk: int = 8 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def load_state(root: Path) -> dict[str, str]:
    p = root / STATE_FILE
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:  # noqa: BLE001
            return {}
    return {}


def save_state(root: Path, state: dict[str, str]) -> None:
    p = root / STATE_FILE
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, p)


def download_one(
    client: httpx.Client,
    endpoint: str,
    repo: str,
    revision: str,
    rel_path: str,
    target: Path,
    expected_size: int,
    expected_sha: str,
) -> str:
    """下载单文件，返回 'downloaded' | 'rehashed'。失败抛异常。"""
    if target.exists() and target.stat().st_size == expected_size:
        if expected_sha and sha256_file(target) == expected_sha:
            return "rehashed"

    url = (
        f"{endpoint}/api/v1/datasets/{repo}/repo"
        f"?Revision={quote(revision, safe='')}&FilePath={quote(rel_path, safe='')}"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")

    last: Exception | None = None
    for attempt in range(DOWNLOAD_RETRIES):
        try:
            h = hashlib.sha256()
            got = 0
            with client.stream("GET", url, headers={"User-Agent": "QuantDB-Fetch/1.0"}) as resp:
                resp.raise_for_status()
                with part.open("wb") as fh:
                    for chunk in resp.iter_bytes(1 << 20):
                        fh.write(chunk)
                        h.update(chunk)
                        got += len(chunk)
            if expected_size and got != expected_size:
                raise IOError(f"大小不符：{got} != {expected_size}")
            if expected_sha and h.hexdigest() != expected_sha:
                raise IOError(f"sha256 不符：{h.hexdigest()[:12]} != {expected_sha[:12]}")
            os.replace(part, target)
            return "downloaded"
        except Exception as exc:  # noqa: BLE001
            last = exc
            part.unlink(missing_ok=True)
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404:
                break
            time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"{rel_path}: {last}")  # noqa: BLE001


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="从魔搭下载 QuantDB 数据（可断点续传）")
    ap.add_argument("--root", default=os.getenv("QM_QUANTDB_DATA_DIR") or "data/quantdb",
                    help="本地 QuantDB 数据目录（默认环境变量 QM_QUANTDB_DATA_DIR / data/quantdb）")
    ap.add_argument("--group", default="base,l1",
                    help="预设分组，逗号分隔：base,l1,l2,tech,features,all（默认 base,l1）")
    ap.add_argument("--datasets", default="",
                    help="直接指定数据集名，逗号分隔（设置后忽略 --group）")
    ap.add_argument("--workers", type=int, default=6, help="并发下载数（默认 6）")
    ap.add_argument("--limit", type=int, default=0, help="仅下载前 N 个文件（调试用）")
    ap.add_argument("--dry-run", action="store_true", help="只枚举并打印规模，不下载")
    ap.add_argument("--verify", action="store_true", help="只校验已有文件的 sha256（不下载）")
    ap.add_argument("--endpoint", default=os.getenv("MODELSCOPE_ENDPOINT") or DEFAULT_ENDPOINT)
    ap.add_argument("--repo", default=os.getenv("MODELSCOPE_DATASET_REPO") or DEFAULT_REPO)
    ap.add_argument("--revision", default=os.getenv("MODELSCOPE_DATASET_REVISION") or DEFAULT_REVISION)
    args = ap.parse_args(argv)

    if args.datasets:
        wanted = [d.strip() for d in args.datasets.split(",") if d.strip()]
    else:
        wanted = []
        for g in (x.strip() for x in args.group.split(",")):
            if g == "all":
                wanted.extend(["daily_forward", "daily_backward", "daily_unadjusted",
                               "index_daily", "min5_kline", "min1_kline", "tick_data",
                               "instrument_detail", "sector_concept", "index_weights",
                               "trading_calendar", "margin_trading", "balance", "income",
                               "cashflow", "capital", "pershare_index", "per_share",
                               "dividend_factors", "holder_num", "etf_pcf", "convertible_bond",
                               "valuation", "technical_indicators", "market_sentiment",
                               "features_daily", "l1_factors", "l1_l2_factors",
                               "factor_dictionary"])
                break
            for d in GROUPS.get(g, []):
                wanted.append(d)

    known = set(DATASETS.values())
    unknown = [d for d in wanted if d not in known]
    if unknown:
        log(f"未知数据集: {unknown}")
        return 2

    rel_dirs = {rel: name for rel, name in DATASETS.items() if name in wanted}
    root = Path(args.root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)

    timeout = httpx.Timeout(30.0, read=300.0, write=120.0, pool=60.0)
    with httpx.Client(timeout=timeout, follow_redirects=True, trust_env=True,
                      headers={"User-Agent": "QuantDB-Fetch/1.0"}) as client:
        remote = list_remote(client, args.endpoint.rstrip("/"), args.repo, args.revision)
        targets: list[dict] = []
        for f in remote:
            if f.get("Type") != "blob" or not str(f.get("Path", "")).endswith(".parquet"):
                continue
            ds = dataset_of(f["Path"])
            if ds in wanted:
                targets.append(f)

        per_ds: dict[str, dict[str, int]] = {}
        for f in targets:
            ds = dataset_of(f["Path"]) or "?"
            stat = per_ds.setdefault(ds, {"files": 0, "bytes": 0})
            stat["files"] += 1
            stat["bytes"] += int(f.get("Size") or 0)

        log(f"目标目录: {root}")
        log(f"数据集 {len(per_ds)} 个，文件 {len(targets)} 个，共 {human(sum(x['bytes'] for x in per_ds.values()))}")
        for ds in sorted(per_ds):
            s = per_ds[ds]
            log(f"  - {ds:<20} {s['files']:>6} 文件  {human(s['bytes'])}")

        if args.dry_run or args.verify:
            state = load_state(root)
            ok = bad = 0
            bad_bytes = 0
            for f in targets[: args.limit or None]:
                p = root / f["Path"]
                if p.exists() and p.stat().st_size == int(f["Size"] or 0) and sha256_file(p) == f.get("Sha256"):
                    ok += 1
                else:
                    bad += 1
                    bad_bytes += int(f.get("Size") or 0)
            if args.verify:
                log(f"校验：完整 {ok} 个，缺失/损坏 {bad} 个（需补 {human(bad_bytes)}）")
            else:
                log("dry-run 结束，未下载")
            return 0 if bad == 0 or args.dry_run else 1

        if args.limit:
            targets = targets[: args.limit]

        state = load_state(root)
        lock = threading.Lock()
        done_bytes = 0
        counters = {"downloaded": 0, "rehashed": 0, "skipped": 0, "failed": 0}
        failed: list[str] = []
        total_bytes = sum(int(f.get("Size") or 0) for f in targets)
        t0 = time.time()

        def job(f: dict) -> str:
            nonlocal done_bytes
            rel = f["Path"]
            target = root / rel
            size = int(f.get("Size") or 0)
            sha = f.get("Sha256") or ""
            if (target.exists() and target.stat().st_size == size
                    and state.get(rel) == sha):
                return "skipped"
            kind = download_one(client, args.endpoint.rstrip("/"), args.repo,
                                args.revision, rel, target, size, sha)
            with lock:
                state[rel] = sha
                done_bytes += size
            return kind

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(job, f): f["Path"] for f in targets}
            for i, fut in enumerate(as_completed(futures), 1):
                path = futures[fut]
                try:
                    kind = fut.result()
                    counters[kind] = counters.get(kind, 0) + 1
                except Exception as exc:  # noqa: BLE001
                    counters["failed"] += 1
                    failed.append(path)
                    log(f"失败：{exc}")
                if i % 200 == 0 or i == len(futures):
                    save_state(root, state)
                    elapsed = max(time.time() - t0, 1e-6)
                    speed = done_bytes / elapsed
                    eta = (total_bytes - done_bytes) / speed if speed else 0
                    pct = 100.0 * done_bytes / total_bytes if total_bytes else 100.0
                    log(f"进度 {i}/{len(futures)} 文件（{pct:5.1f}%）"
                        f" 已下 {human(done_bytes)}/{human(total_bytes)}"
                        f" {human(speed)}/s ETA {eta/60:.0f}min"
                        f" [新 {counters['downloaded']} 复用 {counters['rehashed']} "
                        f"跳过 {counters['skipped']} 失败 {counters['failed']}]")

        save_state(root, state)
        log(f"完成：{counters}，用时 {(time.time()-t0)/60:.1f} 分钟")
        if failed:
            log(f"{len(failed)} 个文件失败，重跑本命令即可续传。示例：{failed[:5]}")
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())
