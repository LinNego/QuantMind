"""严格统计工具：重叠样本修正、块自助、多重检验与过拟合检验。

这些函数只做统计，不读行情、不生成报告。所有随机过程固定种子，保证可复现。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd


def _clean(values: Iterable[float]) -> np.ndarray:
    array = np.asarray(list(values), dtype=np.float64)
    return array[np.isfinite(array)]


def newey_west_mean(values: Iterable[float], lag: int) -> dict[str, float]:
    """重叠样本下的均值检验：Newey-West 标准误与 t 值。"""
    array = _clean(values)
    n = len(array)
    if n == 0:
        return {"mean": float("nan"), "se": float("nan"), "t": float("nan"), "n": 0.0}
    mean = float(array.mean())
    errors = array - mean
    gamma0 = float(errors @ errors) / n
    long_run = gamma0
    for offset in range(1, min(lag, n - 1) + 1):
        weight = 1.0 - offset / (lag + 1.0)
        gamma = float(errors[offset:] @ errors[:-offset]) / n
        long_run += 2.0 * weight * gamma
    variance = max(long_run, 0.0) / n
    se = float(np.sqrt(variance))
    return {
        "mean": mean,
        "se": se,
        "t": mean / se if se > 0 else float("nan"),
        "n": float(n),
    }


def _circular_block_indices(
    n: int, block: int, n_boot: int, rng: np.random.Generator
) -> np.ndarray:
    """循环块自助的索引矩阵，形状 (n_boot, n)。"""
    block = max(1, min(block, n))
    n_blocks = int(np.ceil(n / block))
    starts = rng.integers(0, n, size=(n_boot, n_blocks))
    offsets = np.arange(block, dtype=np.int64)
    indices = (starts[:, :, None] + offsets[None, None, :]) % n
    return indices.reshape(n_boot, -1)[:, :n]


def block_bootstrap_mean(
    values: Iterable[float],
    *,
    block: int = 20,
    n_boot: int = 5000,
    seed: int = 20261019,
) -> dict[str, float]:
    """均值、95% 百分位区间与单侧 p 值（循环块自助）。"""
    array = _clean(values)
    n = len(array)
    if n < 3:
        return {
            "mean": float("nan"),
            "ci_low": float("nan"),
            "ci_high": float("nan"),
            "p_positive": float("nan"),
            "p_negative": float("nan"),
            "n": float(n),
            "n_boot": 0.0,
        }
    rng = np.random.default_rng(seed)
    indices = _circular_block_indices(n, block, n_boot, rng)
    means = array[indices].mean(axis=1)
    return {
        "mean": float(array.mean()),
        "ci_low": float(np.quantile(means, 0.025)),
        "ci_high": float(np.quantile(means, 0.975)),
        "p_positive": float((1 + np.sum(means <= 0)) / (n_boot + 1)),
        "p_negative": float((1 + np.sum(means >= 0)) / (n_boot + 1)),
        "n": float(n),
        "n_boot": float(n_boot),
    }


def bh_qvalues(pvalues: Sequence[float]) -> np.ndarray:
    """Benjamini-Hochberg 校正后的 q 值；NaN 保持 NaN。"""
    values = np.asarray(list(pvalues), dtype=np.float64)
    result = np.full(values.shape, np.nan, dtype=np.float64)
    valid = np.isfinite(values)
    if not valid.any():
        return result
    p = values[valid]
    order = np.argsort(p)
    ranked = p[order]
    m = len(ranked)
    q = ranked * m / np.arange(1, m + 1)
    q = np.minimum.accumulate(q[::-1])[::-1]
    q = np.clip(q, 0.0, 1.0)
    restored = np.empty_like(q)
    restored[order] = q
    result[valid] = restored
    return result


def friedman_kendall(matrix: np.ndarray) -> dict[str, float]:
    """Friedman 检验 + Kendall's W：检验多个模型在同一批日期上是否有差异。"""
    from scipy.stats import friedmanchisquare

    values = np.asarray(matrix, dtype=np.float64)
    if values.ndim != 2 or values.shape[1] < 3:
        return {
            "chi2": float("nan"),
            "p": float("nan"),
            "kendall_w": float("nan"),
            "n_days": float(values.shape[0]) if values.ndim == 2 else 0.0,
            "n_models": float(values.shape[1]) if values.ndim == 2 else 0.0,
        }
    values = values[np.isfinite(values).all(axis=1)]
    n, k = values.shape
    if n < 3:
        return {
            "chi2": float("nan"),
            "p": float("nan"),
            "kendall_w": float("nan"),
            "n_days": float(n),
            "n_models": float(k),
        }
    if np.allclose(values, values[:, [0]]):
        # 同一交易日内各模型数值完全相同：秩和相等，检验退化为 p=1。
        return {
            "chi2": 0.0,
            "p": 1.0,
            "kendall_w": 0.0,
            "n_days": float(n),
            "n_models": float(k),
        }
    statistic, pvalue = friedmanchisquare(*[values[:, j] for j in range(k)])
    return {
        "chi2": float(statistic),
        "p": float(pvalue),
        "kendall_w": float(statistic / (n * (k - 1))),
        "n_days": float(n),
        "n_models": float(k),
    }


def sharpe_ratio(returns: Iterable[float], periods: int = 252) -> float:
    array = _clean(returns)
    if len(array) < 2:
        return float("nan")
    std = float(array.std(ddof=1))
    if std <= 0:
        return float("nan")
    return float(array.mean() / std * np.sqrt(periods))


def deflated_sharpe_ratio(
    returns: Iterable[float],
    *,
    n_trials: int,
    trial_sr_std: float,
    periods: int = 252,
) -> dict[str, float]:
    """Bailey-López de Prado 的 Deflated Sharpe Ratio。

    ``trial_sr_std`` 是同一族试验里非年化 Sharpe 的截面标准差；用它估计
    多重试验下最大 Sharpe 的期望。返回值是「真实 Sharpe 大于 0」的概率。
    """
    from scipy.stats import kurtosis, norm, skew

    array = _clean(returns)
    n = len(array)
    if n < 20 or n_trials < 1:
        return {
            "sharpe": float("nan"),
            "sr0": float("nan"),
            "dsr": float("nan"),
            "n": float(n),
        }
    std = float(array.std(ddof=1))
    if std <= 0:
        return {
            "sharpe": float("nan"),
            "sr0": float("nan"),
            "dsr": float("nan"),
            "n": float(n),
        }
    sr = float(array.mean() / std)
    gamma = 0.5772156649015329
    if trial_sr_std > 0 and n_trials > 1:
        sr0 = trial_sr_std * (
            (1.0 - gamma) * norm.ppf(1.0 - 1.0 / n_trials)
            + gamma * norm.ppf(1.0 - 1.0 / (n_trials * np.e))
        )
    else:
        sr0 = 0.0
    skewness = float(skew(array, bias=False))
    kurt = float(kurtosis(array, fisher=False, bias=False))
    variance = 1.0 - skewness * sr + ((kurt - 1.0) / 4.0) * sr * sr
    if not np.isfinite(variance) or variance <= 0:
        dsr = float("nan")
    else:
        statistic = (sr - sr0) * np.sqrt(n - 1.0) / np.sqrt(variance)
        dsr = float(norm.cdf(statistic))
    return {
        "sharpe": float(sr * np.sqrt(periods)),
        "sr0": float(sr0 * np.sqrt(periods)),
        "dsr": dsr,
        "n": float(n),
    }


def white_reality_check(
    excess_matrix: np.ndarray,
    *,
    block: int = 20,
    n_boot: int = 5000,
    seed: int = 20261019,
) -> dict[str, float]:
    """White Reality Check：控制数据窥探后，最优模型是否仍有超额。

    ``excess_matrix`` 形状为 (交易日, 模型数)。统计量是各模型平均超额的
    最大值；自助时对每一列去均值，强制零假设成立，并保留模型间相关性。
    """
    values = np.asarray(excess_matrix, dtype=np.float64)
    if values.ndim != 2 or values.shape[0] < 3 or values.shape[1] < 1:
        return {
            "stat": float("nan"),
            "p": float("nan"),
            "best_index": float("nan"),
            "n_boot": 0.0,
        }
    values = values[np.isfinite(values).all(axis=1)]
    n, m = values.shape
    if n < 3:
        return {
            "stat": float("nan"),
            "p": float("nan"),
            "best_index": float("nan"),
            "n_boot": 0.0,
        }
    observed = values.mean(axis=0)
    stat = float(observed.max())
    if stat <= 0:
        return {
            "stat": stat,
            "p": 1.0,
            "best_index": float(int(observed.argmax())),
            "n_boot": float(n_boot),
        }
    rng = np.random.default_rng(seed)
    indices = _circular_block_indices(n, block, n_boot, rng)
    centered = values - observed[None, :]
    boot = centered[indices].mean(axis=1).max(axis=1)
    pvalue = float((1 + np.sum(boot >= stat)) / (n_boot + 1))
    return {
        "stat": stat,
        "p": pvalue,
        "best_index": float(int(observed.argmax())),
        "n_boot": float(n_boot),
    }


def _rank_desc(values: np.ndarray) -> np.ndarray:
    from scipy.stats import rankdata

    return rankdata(-values, method="average")


def pbo_cscv(
    returns_matrix: np.ndarray,
    *,
    n_blocks: int = 10,
    periods: int = 252,
) -> dict[str, float]:
    """组合对称交叉验证（CSCV）的过拟合概率 PBO。

    把时间轴切成 S 个连续块，枚举一半做样本内、一半做样本外；每次选样本内
    Sharpe 最高的模型，看它在样本外的排名。PBO 是「样本外排名低于中位数」
    的比例。
    """
    from itertools import combinations

    values = np.asarray(returns_matrix, dtype=np.float64)
    if values.ndim != 2 or values.shape[0] < 20 or values.shape[1] < 2:
        return {
            "pbo": float("nan"),
            "n_combos": 0.0,
            "median_logit": float("nan"),
            "median_oos_rank": float("nan"),
        }
    values = values[np.isfinite(values).all(axis=1)]
    n, m = values.shape
    n_blocks = max(2, min(n_blocks, n // 5))
    if n_blocks % 2 == 1:
        n_blocks -= 1
    blocks = np.array_split(np.arange(n), n_blocks)
    logits: list[float] = []
    oos_ranks: list[float] = []
    for chosen in combinations(range(n_blocks), n_blocks // 2):
        is_idx = np.concatenate([blocks[i] for i in chosen])
        oos_idx = np.concatenate(
            [blocks[i] for i in range(n_blocks) if i not in chosen]
        )
        is_sr = np.array([sharpe_ratio(values[is_idx, j], periods) for j in range(m)])
        oos_sr = np.array([sharpe_ratio(values[oos_idx, j], periods) for j in range(m)])
        if not np.isfinite(is_sr).all() or not np.isfinite(oos_sr).all():
            continue
        best = int(np.nanargmax(is_sr))
        ranks = _rank_desc(oos_sr)
        rank = float(ranks[best])
        # rank 1 = 样本外最好；换成「越好 omega 越大」，使 logit>0 表示优于中位数。
        omega = (m + 1.0 - rank) / (m + 1.0)
        omega = min(max(omega, 1e-6), 1.0 - 1e-6)
        logits.append(float(np.log(omega / (1.0 - omega))))
        oos_ranks.append(rank)
    if not logits:
        return {
            "pbo": float("nan"),
            "n_combos": 0.0,
            "median_logit": float("nan"),
            "median_oos_rank": float("nan"),
        }
    logit_array = np.asarray(logits)
    return {
        "pbo": float((logit_array <= 0).mean()),
        "n_combos": float(len(logits)),
        "median_logit": float(np.median(logit_array)),
        "median_oos_rank": float(np.median(oos_ranks)),
    }


def pairwise_wilcoxon_bh(
    best: Iterable[float],
    others: dict[str, Iterable[float]],
) -> list[dict[str, float | str]]:
    """最优模型 vs 其它模型的配对 Wilcoxon 检验，BH 校正。"""
    from scipy.stats import wilcoxon

    base = np.asarray(list(best), dtype=np.float64)
    rows: list[dict[str, float | str]] = []
    for name, values in others.items():
        other = np.asarray(list(values), dtype=np.float64)
        if len(base) != len(other):
            raise ValueError("配对样本长度不一致")
        mask = np.isfinite(base) & np.isfinite(other)
        diff = base[mask] - other[mask]
        if len(diff) < 10 or np.allclose(diff, 0):
            statistic, pvalue = float("nan"), 1.0
        else:
            statistic, pvalue = wilcoxon(diff, alternative="two-sided")
        rows.append(
            {
                "model": name,
                "stat": float(statistic),
                "p": float(pvalue),
                "mean_diff": float(diff.mean()) if len(diff) else float("nan"),
            }
        )
    qvalues = bh_qvalues([float(row["p"]) for row in rows])
    for row, qvalue in zip(rows, qvalues, strict=True):
        row["q"] = float(qvalue)
    return rows


@dataclass(frozen=True)
class StatSummary:
    mean: float
    ci_low: float
    ci_high: float
    p_positive: float
    n: int


def summarize_mean(
    values: Iterable[float],
    *,
    block: int = 20,
    n_boot: int = 5000,
    seed: int = 20261019,
) -> StatSummary:
    result = block_bootstrap_mean(values, block=block, n_boot=n_boot, seed=seed)
    return StatSummary(
        mean=result["mean"],
        ci_low=result["ci_low"],
        ci_high=result["ci_high"],
        p_positive=result["p_positive"],
        n=int(result["n"]),
    )


def spearman(left: Sequence[float], right: Sequence[float]) -> float:
    """秩相关；样本不足或方差为零时返回 NaN。"""
    a = pd.Series(list(left), dtype=float)
    b = pd.Series(list(right), dtype=float)
    joined = pd.concat([a, b], axis=1, keys=["a", "b"]).dropna()
    if len(joined) < 3:
        return float("nan")
    if joined["a"].nunique() < 2 or joined["b"].nunique() < 2:
        return float("nan")
    return float(joined["a"].rank().corr(joined["b"].rank()))
