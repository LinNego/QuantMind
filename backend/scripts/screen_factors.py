#!/usr/bin/env python3
"""因子筛选：质量门槛 + 同源去重（多库联合：13 库 ∪ factor_research）

输入（均已存在，无需重新计算因子值）：
  <dataset>/report/factor_report.json   —— 因子报告快照（IC/ICIR/换手 + 库内相关矩阵）
      数据集（顺序 = 私人库扫描 discovery.AUTO_PRIORITY + 未列目录字母序，
      跨库重名**先到先得**，两处顺序必须逐字一致）：
      l1_factors(110) / l2_factors(211，2022 起——缺分区的库在早期日期按「无数据」参与，
      其列逐期成对排除，不再跳过整天) / alpha_library(429) / alpha360(360) /
      jq110(109) / tdxgs(88) / features_daily(41，前视收益标签已排除) /
      cand_factors(238) / factor1300(1125) / factor_defs(1336，让位 cand 同名 238 后 1098) /
      gap_mined(69) / l1_batch1_factors(8) / rd_mined(122)；
      l1_l2_factors 为 L1∪L2 冗余并集，不入筛选避免重复
  factor_research/{metrics.json,corr.parquet,monthly_scores.parquet}  —— 82 因子研究库
  <dataset>/dt=*/data.parquet            —— 月末采样做「跨库」相关（单遍联合，只读候选列，缓存 npz）

筛选逻辑：
  1. 质量门槛：|IC 均值| ≥ min_ic 且 |ICIR| ≥ min_icir（各库同一把尺）；
  2. 联合相关（只算候选列）：月末截面 Spearman 逐期均值。pandas 逐对 Pearson 与
     矩阵里还有哪些列无关 → 候选子矩阵与全宽矩阵**逐位相等**（不是近似）——先过
     门槛再算相关，把 corr 的 O(k²) 从全量 4078² 压到候选²，结果不变；
  3. 去重（贪心直接去重）：候选按强度（|ICIR|，同分比 |IC|）降序，逐个与**已保留**因子比
     直接相关 |ρ|（联合矩阵）：≥ 阈值（默认 0.9）者剔除并标注 duplicate_of（取其最相
     关的已保留因子）与 |ρ|；否则保留。
     不用并查集传递闭包——那会把「只通过链式中等相关相连」的因子误并成巨簇（曾出现
     318 成员巨型簇，CLOSE24 与代表 MOM60 直接 ρ 仅 0.54）。
  4. 已知同构对（a101≈gtja、TDXGS_MA≈a158_MA、JQ110_ROC≈a158_ROC 等）由直接相关自然捕获。

输出（<quantdb>/factor_research/screening/）：
  factor_selection.json       机器可读：kept（按库分组）/ dropped（原因/重复对象）/ 门槛与统计
  筛选报告_YYYYMMDD.md         人读报告（各库清单 + 去重明细）
  kept_features.txt           训练可直接消费的特征名清单（每行一个）
  cross_corr.npz              候选列联合相关缓存（--refresh-cross 重算；候选集变化时
                              完全一致→直用 / 子集→切片复用 / 其余→重算）

用法：
  python3 backend/scripts/screen_factors.py                       # 默认门槛（全库联合）
  python3 backend/scripts/screen_factors.py --min-ic 0.03 --min-icir 0.3 --corr 0.85
  python3 backend/scripts/screen_factors.py --skip-cross          # 只用库内矩阵（快，无跨库去重）
  python3 backend/scripts/screen_factors.py --libraries alpha_library,tdxgs   # 只筛部分库
  python3 backend/scripts/screen_factors.py --cross-workers 16    # 调联合相关并行度（默认 min(8, CPU)）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.services.engine.factor_research.catalog import BY_CODE  # noqa: E402
from backend.shared.quantdb_paths import resolve_quantdb_dir  # noqa: E402

# factor_report 包 __init__ 会拉 FastAPI（宿主裸跑没有），按文件直载 clusters 模块（纯 numpy）
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "fr_clusters",
    Path(__file__).resolve().parents[1]
    / "services"
    / "engine"
    / "factor_report"
    / "clusters.py",
)
_fr_clusters = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fr_clusters)
cluster_by_correlation = (
    _fr_clusters.cluster_by_correlation
)  # 供 --method cluster 备用（默认不用）

# 顺序 = discovery.AUTO_PRIORITY + 未列目录按字母序 —— 与私人库扫描的先到先得去重
# **必须逐字一致**：cand_factors 与 factor_defs 有 238 列全同名，顺序一分叉，
# 「参与面与私人库对齐」的承诺就静默失效（2026-10-15 全量扩展时钉死）。
REPORT_DATASETS = [
    "l1_factors",
    "l2_factors",
    "alpha_library",
    "alpha360",
    "jq110",
    "tdxgs",
    "features_daily",  # DAILY_FEATURES_MARK
    "cand_factors",  # CAND_FACTORS_MARK
    "factor1300",
    "factor_defs",
    "gap_mined",  # GAP_MINED_MARK
    "l1_batch1_factors",  # L1_BATCH1_MARK
    "rd_mined",  # RD_MINED_MARK
]
OURS = "factor_research"
DATASET_LABEL = {
    "l1_factors": "L1 因子（量价/换手/波动等基础因子）",
    "l2_factors": "L2 因子（逐笔微观结构，2022 起）",
    "alpha_library": "Alpha 库（Alpha101 / GTJA191 / Alpha158）",
    "alpha360": "Alpha360 原始量价回溯",
    "jq110": "JQ110 聚宽因子",
    "tdxgs": "TDXGS 通达信技术指标",
    "features_daily": "每日特征（技术+估值）",
    "cand_factors": "候选因子池",
    "factor1300": "因子清单1300（收藏公式物化库）",
    "factor_defs": "因子清单库（日频可复现部分）",
    "gap_mined": "空档挖掘因子（股东机构/财务PIT/板块概念/两融/L2二阶/新闻情绪）",
    "l1_batch1_factors": "L1 批量因子（batch1）",
    "rd_mined": "RD-Agent 挖掘因子",
    "factor_research": "因子研究（行情+财务+行为）",
}


def _dataset_dir(dataset: str) -> Path:
    return resolve_quantdb_dir() / "6_ml_datasets" / dataset


def _report_path(dataset: str, reports_dir: str | Path | None = None) -> Path:
    """报告快照路径。``reports_dir`` 用于跑「截断窗口」等替代报告，不覆盖生产快照。"""
    if reports_dir:
        return Path(reports_dir) / dataset / "factor_report.json"
    return _dataset_dir(dataset) / "report" / "factor_report.json"


def _load_report(
    dataset: str, reports_dir: str | Path | None = None
) -> tuple[list[str], np.ndarray, dict[str, dict]]:
    rep = json.loads(_report_path(dataset, reports_dir).read_text(encoding="utf-8"))
    names = list(rep["correlation"]["factors"])
    matrix = np.asarray(rep["correlation"]["matrix"], dtype=np.float64)
    by_name = {f["name"]: f for f in rep["factors"]}
    metrics = {}
    for n in names:
        f = by_name.get(n) or {}
        sub = f.get("library") or dataset  # 报告里的子库（alpha101/gtja191/alpha158）
        metrics[n] = {
            "ic_mean": f.get("ic_mean"),
            "icir": f.get("icir"),
            "turnover": f.get("turnover"),
            "display_name": f.get("display_name") or n,
            "library": dataset,
            "sublibrary": sub,
        }
    return names, matrix, metrics


def _screen_dir(out_dir: str | Path | None = None) -> Path:
    """筛选产物目录（factor_selection.json / kept_features.txt / cross_corr.npz）。

    ``out_dir`` 用于截断窗口等实验：联合相关缓存与产物一起搬走，否则既覆盖
    生产快照，又会让后续生产跑复用错窗口的相关矩阵（缓存静默中毒）。
    """
    if out_dir:
        return Path(out_dir)
    return resolve_quantdb_dir() / OURS / "screening"


def _load_ours(
    ours_dir: str | Path | None = None,
) -> tuple[list[str], np.ndarray, dict[str, dict]]:
    """自有因子库（``factor_research``）的指标与相关阵。

    ``ours_dir`` 指向替代快照（如 ``FACTOR_RESEARCH_OUT`` 产出的截断窗口），
    不传则读生产目录。
    """
    root = Path(ours_dir) if ours_dir else resolve_quantdb_dir() / OURS
    m = json.loads((root / "metrics.json").read_text(encoding="utf-8"))
    metrics_json = m.get("metrics", {})
    names = sorted(metrics_json)
    corr_df = pd.read_parquet(root / "corr.parquet")
    idx = {n: i for i, n in enumerate(names)}
    M = np.eye(len(names))
    for r in corr_df.to_dict("records"):
        i, j = idx.get(r["factor_a"]), idx.get(r["factor_b"])
        if i is None or j is None or r["corr"] is None:
            continue
        M[i, j] = M[j, i] = float(r["corr"])
    metrics = {}
    for n in names:
        k = metrics_json[n]
        blob = BY_CODE.get(n) or {}
        metrics[n] = {
            "ic_mean": k.get("ic_mean"),
            "icir": k.get("ic_ir"),
            "turnover": None,
            "display_name": blob.get("name_cn") or n,
            "library": OURS,
            "sublibrary": f"{blob.get('l1', '')}/{blob.get('l2', '')}".strip("/"),
        }
    return names, M, metrics


def _dedup_first_wins(
    blocks: list[tuple[str, list[str], np.ndarray]],
) -> tuple[list[tuple[str, list[str], np.ndarray]], dict[str, int]]:
    """跨库重名按 blocks 顺序先到先得；返回 (去重后 blocks, {库: 让位列数})。

    顺序语义与私人库扫描（discovery.AUTO_PRIORITY + 未列目录按字母序）逐字一致——
    cand_factors 与 factor_defs 全部 238 列同名，cand 在前全取、factor_defs 让位
    （1336→1098）；两处顺序一旦分叉，「参与面 = 私人库 4011」就静默失效。
    让位库的库内矩阵同步用 np.ix_ 裁到保留列——只裁名字不裁矩阵会让列序错位，
    后续相关全部算错且无一层会报错。
    """
    seen: set[str] = set()
    out: list[tuple[str, list[str], np.ndarray]] = []
    dropped: dict[str, int] = {}
    for ds, nms, mat in blocks:
        keep = [i for i, n in enumerate(nms) if n not in seen]
        if len(keep) != len(nms):
            dropped[ds] = len(nms) - len(keep)
        keep_names = [nms[i] for i in keep]
        seen.update(keep_names)
        out.append((ds, keep_names, mat[np.ix_(keep, keep)]))
    return out, dropped


def _gate_candidates(
    names_all: list[str],
    metrics: dict[str, dict],
    min_ic: float,
    min_icir: float,
) -> tuple[list[str], dict[str, str]]:
    """质量门槛：|IC 均值| ≥ min_ic 且 |ICIR| ≥ min_icir（各库同一把尺）。

    顺序沿 names_all —— 候选列表序即最终联合矩阵列序（缓存/切片都依赖这个序）。
    None 指标 = 未计算或退化，按剔除处理并记录原因。
    """
    gated_out: dict[str, str] = {}
    candidates: list[str] = []
    for n in names_all:
        m = metrics[n]
        ic, icir = m.get("ic_mean"), m.get("icir")
        if ic is None or icir is None:
            gated_out[n] = "无 IC/ICIR 指标（未计算或退化）"
            continue
        if abs(ic) < min_ic:
            gated_out[n] = f"|IC|={abs(ic):.4f} < {min_ic}"
            continue
        if abs(icir) < min_icir:
            gated_out[n] = f"|ICIR|={abs(icir):.3f} < {min_icir}"
            continue
        candidates.append(n)
    return candidates, gated_out


def _matrix_from_cache(
    cached_names: list[str], cached_matrix: np.ndarray, wanted: list[str]
) -> np.ndarray | None:
    """联合相关缓存复用：完全一致 → 直用；候选 ⊂ 缓存 → 按位切片。

    切片合法的前提 = 逐对 Pearson 与其他列无关（子矩阵逐位相等，非近似）。
    缓存未覆盖全部候选列（新增库/新因子）或形状不符 → None（调用方重算）。
    """
    if cached_matrix.shape != (len(cached_names), len(cached_names)):
        return None
    if cached_names == wanted:
        return cached_matrix
    if set(wanted) <= set(cached_names):
        sel = pd.Index(cached_names).get_indexer(wanted)
        return cached_matrix[np.ix_(sel, sel)]
    return None


# ---- 候选列联合相关：worker 只读候选列，父进程按日期池化（imap 保序）----
_CROSS_WORKER_STATE: dict = {}


def _init_cross_worker(state: dict) -> None:
    _CROSS_WORKER_STATE.clear()
    _CROSS_WORKER_STATE.update(state)


def _cross_corr_one_date(task: tuple[str, pd.DataFrame]) -> np.ndarray | None:
    """单期候选列相关矩阵（Pool worker 与串行共用）。

    列序 = 状态里的候选列序（blocks 依次 + our_use）；行数 < 60 的期返回 None
    （沿用「有效样本不足不参与」口径）。逐对 Pearson 与其他列在不在矩阵里无关，
    所以这里的候选子矩阵 == 全宽矩阵的同位子矩阵（逐位相等）。
    """
    dt, our_d = task
    root = Path(_CROSS_WORKER_STATE["root"])
    ds_names: list[str] = _CROSS_WORKER_STATE["ds_names"]
    our_use: list[str] = _CROSS_WORKER_STATE["our_use"]
    comb = None
    for ds, use in _CROSS_WORKER_STATE["blocks"]:
        f = root / "6_ml_datasets" / ds / f"dt={dt}" / "data.parquet"
        if not f.exists():
            continue  # 该库当日期无分区（如 L2 早于 2022）→ 其列本期为 NaN，成对排除
        try:
            part = pd.read_parquet(f, columns=["symbol", *use]).set_index("symbol")
        except Exception:  # noqa: BLE001
            # 列代际断层降级：旧分区缺新列名（实测 features_daily ≤20260630 分区
            # 无 is_st/free_float_shares、标签列旧名）——整块静默 continue 会让该库
            # 所有因子在 82 期里只剩几期可比（min_periods=50 全军 NaN，一个都不
            # 去重、直进 kept）。退化为「可用列交集 + 缺列补 NaN」。
            try:
                import pyarrow.parquet as pq

                have = set(pq.ParquetFile(f).schema_arrow.names)
                cols = [c for c in use if c in have]
                part = pd.read_parquet(f, columns=["symbol", *cols]).set_index("symbol")
                part = part.reindex(columns=use)
            except Exception:  # noqa: BLE001
                continue
        comb = part if comb is None else comb.join(part, how="inner")
    if comb is None:
        return None
    comb = comb.reindex(columns=ds_names)  # 缺失库的候选列补 NaN
    comb = comb.join(our_d, how="inner").reindex(columns=[*ds_names, *our_use])
    if len(comb) < 60:
        return None
    return comb.rank().corr(min_periods=50).to_numpy(dtype=np.float64)


def _union_cross_corr(
    blocks: list[tuple[str, list[str]]],
    our_names: list[str],
    candidates: list[str],
    max_dates: int = 0,
    workers: int = 1,
    ours_dir: str | Path | None = None,
) -> tuple[np.ndarray, int]:
    """单遍联合相关：各库分区 × factor_research 打分，月末截面 Spearman 逐期均值。

    blocks: [(dataset, 因子名列表)]；candidates: 参与列子集（**过门槛候选**，
    names_all 序）；返回 (候选列联合相关均值矩阵, 使用的期数)。
    workers>1 时按日期池化，父进程按日期序累加（imap 保序）——与串行逐位一致。
    ``ours_dir`` 指向替代打分快照（截断窗口实验），不传读生产。
    """
    root = resolve_quantdb_dir()
    cand_set = set(candidates)
    cross = [(ds, [n for n in names if n in cand_set]) for ds, names in blocks]
    ds_names = [n for _, use in cross for n in use]
    our_use = [n for n in our_names if n in cand_set]
    k = len(ds_names) + len(our_use)
    if k == 0:
        return np.empty((0, 0)), 0

    ours_root = Path(ours_dir) if ours_dir else root / OURS
    scores = pd.read_parquet(
        ours_root / "monthly_scores.parquet",
        columns=["trade_date", "symbol", "factor_code", "score"],
    )
    scores["symbol"] = scores["symbol"].astype("category")
    scores["factor_code"] = scores["factor_code"].astype("category")
    groups = {pd.Timestamp(d): g for d, g in scores.groupby("trade_date", sort=True)}
    dates = sorted(groups)
    if max_dates:
        dates = dates[-max_dates:]
    del scores

    # 期任务：our_d 在父进程先 pivot（省 N 份重复计算）；worker 端只取候选列。
    tasks: list[tuple[str, pd.DataFrame]] = []
    for d in dates:
        dt = pd.Timestamp(d).strftime("%Y%m%d")
        our_d = (
            groups[d]
            .pivot_table(
                index="symbol", columns="factor_code", values="score", aggfunc="last"
            )
            .reindex(columns=our_names)
        )
        tasks.append((dt, our_d))

    state = {
        "root": str(root),
        "blocks": cross,
        "ds_names": ds_names,
        "our_use": our_use,
    }
    acc = np.zeros((k, k))
    cnt = np.zeros((k, k))
    used = 0
    total = len(tasks)
    t_batch = time.time()

    def _accumulate(cm: np.ndarray | None) -> None:
        nonlocal used
        if cm is None:
            return
        good = np.isfinite(cm)
        acc[good] += cm[good]
        cnt[good] += 1
        used += 1

    def _tick(i: int) -> None:
        if i % 10 == 0 or i == total:
            print(
                f"      {i}/{total} 期（{time.time() - t_batch:.0f}s，有效 {used}）",
                flush=True,
            )

    if workers > 1 and total > 1:
        import multiprocessing as mp

        ctx = mp.get_context("fork")  # fork：worker 继承父进程已导入的 pandas/numpy
        with ctx.Pool(
            processes=min(workers, total),
            initializer=_init_cross_worker,
            initargs=(state,),
        ) as pool:
            for i, cm in enumerate(
                pool.imap(_cross_corr_one_date, tasks, chunksize=1), 1
            ):
                _accumulate(cm)
                _tick(i)
    else:
        _init_cross_worker(state)
        for i, task in enumerate(tasks, 1):
            _accumulate(_cross_corr_one_date(task))
            _tick(i)

    with np.errstate(invalid="ignore"):
        mean = np.where(cnt > 0, acc / np.maximum(cnt, 1), np.nan)
    np.fill_diagonal(mean, 1.0)
    return mean, used


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--min-ic", type=float, default=0.02, help="|IC 均值| 门槛（默认 0.02）"
    )
    ap.add_argument(
        "--min-icir", type=float, default=0.2, help="|ICIR| 门槛（默认 0.2）"
    )
    ap.add_argument(
        "--corr", type=float, default=0.9, help="去重相关阈值 |ρ|（默认 0.9）"
    )
    ap.add_argument(
        "--skip-cross", action="store_true", help="跳过跨库相关（仅库内矩阵，快）"
    )
    ap.add_argument(
        "--cross-workers",
        type=int,
        default=min(8, os.cpu_count() or 4),
        help="联合相关的按日期并行进程数（1=串行；默认 min(8, CPU)）",
    )
    ap.add_argument(
        "--refresh-cross", action="store_true", help="忽略联合相关缓存，重算"
    )
    ap.add_argument(
        "--libraries",
        default=",".join([*REPORT_DATASETS, OURS]),
        help="参与筛选的库（逗号分隔；默认全部）",
    )
    ap.add_argument(
        "--reports-dir",
        default=None,
        help="替代报告根目录（<dir>/<dataset>/factor_report.json）；用于截断窗口等实验，不覆盖生产快照",
    )
    ap.add_argument(
        "--ours-dir",
        default=None,
        help="自有因子库替代快照目录（metrics.json + corr.parquet）；"
        "配 FACTOR_RESEARCH_OUT 用同一截断窗口，不覆盖生产快照",
    )
    ap.add_argument(
        "--out-dir",
        default=None,
        help="替代输出目录（factor_selection.json / kept_features.txt / cross_corr.npz）；"
        "截断窗口实验必须给，否则会覆盖生产筛选快照与相关缓存",
    )
    args = ap.parse_args()
    screen_dir = _screen_dir(args.out_dir)
    wanted = [x.strip() for x in args.libraries.split(",") if x.strip()]

    t0 = time.time()
    names_all: list[str] = []
    metrics: dict[str, dict] = {}
    blocks: list[tuple[str, list[str], np.ndarray]] = []  # (dataset, names, 库内矩阵)
    use_ours = OURS in wanted
    for ds in REPORT_DATASETS:
        if ds not in wanted:
            continue
        rep_file = _report_path(ds, args.reports_dir)
        if not rep_file.exists():
            print(
                f"      ⚠ {ds} 无因子报告快照（{rep_file}），跳过"
                f"（先跑 build_factor_report.py --dataset {ds}）"
            )
            continue
        nms, mat, mets = _load_report(ds, args.reports_dir)
        print(f"[1/5] {ds}: {len(nms)} 因子（报告矩阵 {mat.shape}）")
        blocks.append((ds, nms, mat))
        names_all.extend(nms)
        for n, m in mets.items():
            metrics.setdefault(n, m)  # 首见为准——与跨库去重的先到先得同序
    if use_ours:
        nms, mat, mets = _load_ours(args.ours_dir)
        print(f"[2/5] {OURS}: {len(nms)} 因子")
        blocks.append((OURS, nms, mat))
        names_all.extend(nms)
        for n, m in mets.items():
            metrics.setdefault(n, m)
    if not names_all:
        raise SystemExit("没有可筛选的库（--libraries 为空或缺报告快照）")

    # ---- 跨库重名：按库序先到先得（口径与私人库扫描 discovery.AUTO_PRIORITY 同序）----
    # 曾直接 SystemExit 拒绝重名——现在 cand_factors 与 factor_defs 有 238 列合法同名，
    # 让位是设计行为而非冲突。metrics 已用 setdefault 保持首见为准，与这里的保留列一致。
    blocks, dedup_dropped = _dedup_first_wins(blocks)
    for ds, n_drop in dedup_dropped.items():
        print(f"      跨库重名：{ds} 让位 {n_drop} 列（先到先得，同名由高优先库提供）")
    names_all = [n for _, nms, _ in blocks for n in nms]

    # ---- 质量门槛（先于联合相关）----
    # 逐对 Pearson 与矩阵里还有哪些列无关 → 先过门槛、只对候选列算相关：候选子矩阵
    # 与全宽矩阵逐位相等（不是近似），corr 的 O(k²) 从全量 4078² 压到候选规模。
    print("[3/5] 质量门槛 ...")
    candidates, gated_out = _gate_candidates(
        names_all, metrics, args.min_ic, args.min_icir
    )
    print(f"      候选 {len(candidates)}（门槛剔除 {len(gated_out)}）")
    idx_all = {n: i for i, n in enumerate(names_all)}
    pos_cand = {n: i for i, n in enumerate(candidates)}

    # ---- 联合相关矩阵（候选列子集）----
    off = {}
    pos = 0
    for ds, nms, _ in blocks:
        off[ds] = (pos, pos + len(nms))
        pos += len(nms)
    if args.skip_cross or not use_ours:
        note = (
            "--skip-cross"
            if args.skip_cross
            else "未选 factor_research（跨库相关以其打分为基准）"
        )
        print(f"[4/5] 跳过跨库相关（{note}）：库内矩阵拼接")
        M_full = np.full((len(names_all), len(names_all)), np.nan)
        for ds, _nms, mat in blocks:
            a, b = off[ds]
            M_full[a:b, a:b] = mat
        sel = [idx_all[n] for n in candidates]
        M = M_full[np.ix_(sel, sel)]
        cross_note = f"未计算跨库（{note}；仅库内去重）"
    else:
        cache_file = screen_dir / "cross_corr.npz"
        M = None
        if cache_file.exists() and not args.refresh_cross:
            try:
                z = np.load(cache_file, allow_pickle=False)
                valid = "names" in z and "matrix" in z and "used" in z
            except Exception:  # noqa: BLE001
                valid = False
            if valid:
                M = _matrix_from_cache(
                    [str(x) for x in z["names"]], z["matrix"], candidates
                )
            if M is not None:
                cross_note = (
                    f"联合月末截面 Spearman 均值（{int(z['used'])} 期，缓存复用）"
                )
                print(f"[4/5] 载入联合相关缓存（{int(z['used'])} 期，{M.shape}）")
            else:
                print("[4/5] 缓存缺失/不兼容/未含全部候选列，重算联合相关")
        if M is None:
            cross_blocks = [(ds, nms) for ds, nms, _ in blocks if ds != OURS]
            our_names = [n for ds, nms, _ in blocks if ds == OURS for n in nms]
            if cross_blocks:
                print(
                    f"[4/5] 计算联合跨库相关（候选 {len(candidates)} 列，月末截面，"
                    f"{args.cross_workers} 路并行）..."
                )
                M, used = _union_cross_corr(
                    cross_blocks,
                    our_names,
                    candidates,
                    workers=args.cross_workers,
                    ours_dir=args.ours_dir,
                )
            else:
                M, used = _union_cross_corr(
                    [(OURS, our_names)],
                    [],
                    candidates,
                    workers=args.cross_workers,
                    ours_dir=args.ours_dir,
                )
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            np.savez_compressed(
                cache_file, matrix=M, used=used, names=np.array(candidates, dtype=str)
            )
            cross_note = f"联合月末截面 Spearman 均值（{used} 期）"
            print(
                f"      完成：{used} 期 × {M.shape}（{time.time() - t0:.0f}s，已缓存）"
            )

    # ---- 联合去重（候选 × 已保留，贪心直接去重）----
    print("[5/5] 联合去重 ...")

    def _strength(n: str) -> float:
        m = metrics[n]
        return abs(float(m.get("icir") or 0)) * 1000 + abs(float(m.get("ic_mean") or 0))

    order = sorted(candidates, key=_strength, reverse=True)
    kept: list[str] = []
    dup_of: dict[str, tuple[str, float]] = {}
    for n in order:
        i = pos_cand[n]
        hits: list[tuple[str, float]] = []
        for k in kept:
            rho = M[i, pos_cand[k]]
            if np.isfinite(rho) and abs(rho) >= args.corr:
                hits.append((k, abs(float(rho))))
        if hits:
            rep, cc = max(
                hits, key=lambda x: x[1]
            )  # 与本次最相关的已保留因子作为「重复于」
            dup_of[n] = (rep, cc)
        else:
            kept.append(n)

    # 报告视图：按代表归组的直接去重明细（无链式传递）
    groups: dict[str, list[tuple[str, float]]] = {}
    for n, (rep, cc) in dup_of.items():
        groups.setdefault(rep, []).append((n, cc))
    cluster_rows = [
        {
            "representative": rep,
            "size": len(members) + 1,
            "members": [
                {"name": rep, "is_rep": True, "corr_to_rep": 1.0},
                *[
                    {"name": n, "is_rep": False, "corr_to_rep": round(cc, 3)}
                    for n, cc in sorted(members, key=lambda x: -x[1])
                ],
            ],
        }
        for rep, members in sorted(groups.items(), key=lambda kv: -len(kv[1]))
    ]
    n_clusters = len(cluster_rows)
    print(
        f"      候选 {len(candidates)} → 保留 {len(kept)}"
        f"（门槛剔除 {len(gated_out)}，去重剔除 {len(dup_of)}，{n_clusters} 组同源）"
    )

    # ---- 落盘 ----
    out_dir = screen_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    def _row(n: str) -> dict:
        m = metrics[n]
        return {
            "name": n,
            "display_name": m.get("display_name"),
            "library": m.get("library"),
            "sublibrary": m.get("sublibrary"),
            "ic_mean": m.get("ic_mean"),
            "icir": m.get("icir"),
            "turnover": m.get("turnover"),
        }

    selected = {
        "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
        "dedup_method": "greedy_direct_corr",
        "gates": {
            "min_abs_ic": args.min_ic,
            "min_abs_icir": args.min_icir,
            "corr_threshold": args.corr,
        },
        "libraries": [ds for ds, _, _ in blocks],
        "cross_corr": cross_note,
        "counts": {
            "candidates": len(candidates),
            "kept": len(kept),
            "gated_out": len(gated_out),
            "deduped": len(dup_of),
            "total_considered": len(names_all),
        },
        "kept": [_row(n) for n in kept],
        "dropped_gated": [
            {"name": n, "library": metrics[n]["library"], "reason": r}
            for n, r in sorted(gated_out.items())
        ],
        "dropped_duplicate": [
            {
                "name": n,
                "library": metrics[n]["library"],
                "duplicate_of": rep,
                "abs_corr": round(cc, 3),
            }
            for n, (rep, cc) in sorted(dup_of.items())
        ],
        "clusters": cluster_rows,
        "cluster_summary": {
            "n_groups": n_clusters,
            "n_duplicates": len(dup_of),
            "largest_group": cluster_rows[0]["representative"]
            if cluster_rows
            else None,
            "largest_group_size": cluster_rows[0]["size"] if cluster_rows else 0,
        },
    }
    (out_dir / "factor_selection.json").write_text(
        json.dumps(selected, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (out_dir / "kept_features.txt").write_text("\n".join(kept) + "\n", encoding="utf-8")

    # ---- 报告 md ----
    lines = []
    lines.append(f"# 因子筛选报告（{pd.Timestamp.now().strftime('%Y-%m-%d')}）")
    lines.append("")
    libs_label = " ∪ ".join(
        DATASET_LABEL.get(ds, ds) + f"（{len(nms)}）" for ds, nms, _ in blocks
    )
    lines.append(f"> 来源：{libs_label}")
    lines.append(
        f"> 门槛：|IC 均值| ≥ {args.min_ic}，|ICIR| ≥ {args.min_icir}；去重：联合相关 |ρ| ≥ {args.corr} 每簇留最优（|ICIR|）"
    )
    lines.append(f"> 相关：{cross_note}")
    lines.append("")
    lines.append("## 统计")
    lines.append("")
    lines.append("| 项 | 数量 |")
    lines.append("|---|---|")
    lines.append(f"| 参与筛选 | {len(names_all)} |")
    lines.append(f"| 过门槛候选 | {len(candidates)} |")
    lines.append(f"| **最终保留** | **{len(kept)}** |")
    lines.append(f"| 门槛剔除 | {len(gated_out)} |")
    lines.append(f"| 同源去重剔除 | {len(dup_of)}（{n_clusters} 组） |")
    lines.append("")
    for ds, nms, _ in blocks:
        rows = [n for n in kept if metrics[n]["library"] == ds]
        kept_ratio = len(rows) / max(len(nms), 1)
        lines.append(
            f"## 保留清单 · {DATASET_LABEL.get(ds, ds)}（{len(rows)}/{len(nms)} = {kept_ratio:.0%}）"
        )
        lines.append("")
        if not rows:
            lines.append("（无）")
            lines.append("")
            continue
        sub_col = ds == "alpha_library"
        head = (
            "| # | 因子 |"
            + (" 子库 |" if sub_col else "")
            + " 说明 | IC 均值 | ICIR | 换手 |"
        )
        sep = "|---|---|" + ("---|" if sub_col else "") + "---|---|---|---|"
        lines.append(head)
        lines.append(sep)
        for i, n in enumerate(rows, 1):
            m = metrics[n]
            tv = f"{m['turnover']:.2f}" if m.get("turnover") is not None else "—"
            mid = f" {m.get('sublibrary')} |" if sub_col else ""
            lines.append(
                f"| {i} | `{n}` |{mid} {m.get('display_name')} | {m.get('ic_mean')} | {m.get('icir')} | {tv} |"
            )
        lines.append("")
    lines.append(
        f"## 去重剔除（{len(dup_of)}）—— 与保留因子直接相关 |ρ| ≥ {args.corr}，**不要重复进训练**"
    )
    lines.append("")
    lines.append(
        "> 贪心直接去重：每个剔除项与「重复于」的保留因子直接相关 ≥ 阈值（非链式传递）。"
    )
    lines.append("")
    lines.append("| 因子 | 库 | 重复于 | \\|ρ\\| | 说明 |")
    lines.append("|---|---|---|---|---|")
    for n, (rep, cc) in sorted(dup_of.items(), key=lambda x: -x[1][1]):
        lines.append(
            f"| `{n}` | {metrics[n]['library']} | `{rep}` | {cc:.3f} | {metrics[n].get('display_name')} |"
        )
    lines.append("")
    lines.append(f"### 同源组一览（{n_clusters} 组，按组大小降序，前 20）")
    lines.append("")
    for c in cluster_rows[:20]:
        mems = "、".join(f"`{m['name']}`" for m in c["members"] if not m["is_rep"])
        rep = c["representative"]
        lines.append(f"- **{rep}**（保留）← 直接重复 {c['size'] - 1} 个：{mems}")
    lines.append("")
    lines.append("## 训练接入")
    lines.append("")
    lines.append(
        "- 特征清单：`kept_features.txt`（每行一个因子名；跨库使用时按所属数据集分别取列）"
    )
    lines.append(
        "- 机器可读结果：`factor_selection.json`（kept / dropped 原因 / 簇结构）"
    )
    lines.append(
        "- 提醒：>0.9 相关的一对因子只保留一个；若嫌去重过狠，可用 `--corr 0.95` 收紧阈值。"
    )
    lines.append("")
    md_name = f"筛选报告_{pd.Timestamp.now().strftime('%Y%m%d')}.md"
    (out_dir / md_name).write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"完成 → {out_dir}（{time.time() - t0:.0f}s）")
    print(f"  保留 {len(kept)} 个；报告 {md_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
