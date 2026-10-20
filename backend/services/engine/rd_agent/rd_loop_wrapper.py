"""RDLoop 包装器 — 将 RD-Agent 的 FactorRDLoop 适配到 QuantMind

提供统一接口:
- 接收 MarketAdapter 配置市场参数
- 启动/监控/取消 RDLoop
- 从日志中提取因子结果
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from bisect import bisect_right
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

# 运行时导入（不止注解用）：``quantdb_coverage_from_dir`` 要构造 Window。
# mining_config 只依赖 mining_datasets（纯表），不反向依赖本模块，无环。
from backend.services.engine.alpha_agent.mining_config import Window
from backend.shared.safe_pickle import load_pickle

from .market_adapters import get_adapter, list_markets
from .market_adapters.base import MarketAdapter
from .mining_datasets import (
    DATASET_SHORT_NAMES,
    ENRICH_BY_DATASET,
    L2_DATASET,
    SUPPORTED_DATASETS,
    datasets_from_env,
)
from .qlib_conf_template import install_qlib_execute_patch
from .stage_prompt import STAGE_BLOCK_MARKER, render_eval_criteria_block

logger = logging.getLogger(__name__)

# QuantDB 富化列白名单（仅 A 股）：{源列名: 输出列名}，输出列在 h5 中加 "$" 前缀。
# 这些列会随 daily_pv.h5 一起提供给 RD-Agent，LLM 可直接在因子表达式中引用
# （$<输出列名>），使因子能用上 QuantDB 的预计算技术指标/估值/资金流/筹码等富数据。
#
# 白名单本体已抽到 ``mining_datasets``（单源）：数据集选择（移植 P1）
# 要同时驱动**数据面**（本文件 _merge_enrich/_merge_l2_factors）与**提示面**
# （a_share._l2_referenceable_columns 等），留副本必然漂移。下划线名字保留给
# 既有引用方（market_adapters/a_share.py、既有测试）。
_ENRICH_FEATURES_DAILY = ENRICH_BY_DATASET["features_daily"]
_ENRICH_L1 = ENRICH_BY_DATASET["l1_factors"]
# 静态数据集的输出列名（不含 L2——动态列集由 l2_factor_columns 现场读盘）
_ENRICH_OUTPUT_COLUMNS: tuple[str, ...] = tuple(
    col
    for _dataset in ("features_daily", "l1_factors")
    for col in (ENRICH_BY_DATASET[_dataset] or {}).values()
)

# L2 微观结构因子全量接入挖掘数据面（T-MV-07）：l2_factors 的真因子列经
# _merge_l2_factors 并入 daily_pv.h5，LLM 因子表达式可直接引用 $<真列名>。
# 列清单每次现场读盘（l2_factor_columns）：「数据面列清单与目录一致」的验收
# 即此谓词；这里不维护静态名单，避免与上游发布漂移。
_L2_LIBRARY = L2_DATASET
_L2_SKIP_COLUMNS: frozenset[str] = frozenset(
    {
        # 键列
        "symbol",
        "date",
        "datetime",
        "instrument",
        "dt",
        "trade_date",
        # OHLCV 锚列：与 base h5 的 $open/$high/$low/$close/$volume/$amount 同名
        # （且口径为原始未复权价），混入会让 $ 引用静默指向错数据
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
    }
)
# 分块 fetch：200+ 列一次性 DuckDB→pandas 的峰值内存约 8GB（float64），
# 48 列/块把单块峰值压到 ~2GB 以内，逐块 merge 后立即释放
_L2_FETCH_CHUNK = 48

# L2 取数再按 symbol 分片（2026-10-14 二修，与连接释放配套）：整市场单查询
# 48 列 = 结果 ~3GB + DuckDB 在途瞬态 ~6GB；分片后单查询行域 1/4，在途瞬态
# 与连接驻留同比例缩小，键定位（get_indexer）语义与分片无关。
_L2_SYMBOL_SHARD = 1400

# 无自定义窗口（QM_MINING_WINDOW_* 未设）时 h5 数据面的旧默认跨度，保持迁移前
# 行为逐位不变（含共享缓存文件名）。
_H5_DEFAULT_START = date(2020, 1, 1)
_H5_DEFAULT_END = date(2026, 12, 31)


class MinuteDataError(RuntimeError):
    """分钟数据面（provider / h5）不可用——显式失败，绝不静默退回日线。

    分钟任务最坏的失败形态是「悄悄按日线跑完、产出一批自称分钟因子的东西」，
    所以从 provider 构建到 h5 生成、再到 conf 渲染，每一环拿不到分钟数据都
    直接抛错让任务失败。
    """


def _h5_cache_name(debug: bool, start: date, end: date, datasets: Sequence[str]) -> str:
    """共享缓存文件名：默认组合沿用旧名，自定义窗口/数据集按内容分文件。

    「窗口 A 生成的缓存冒充窗口 B 的数据面」是静默错数据（LLM 引用到的列、
    因子计算覆盖的区间都随之错位），所以缓存身份 = 窗口 + 数据集选择。默认
    组合（旧默认窗口 + 全数据集）保持 ``daily_pv_all.h5``/``daily_pv_debug.h5``
    旧名，存量缓存与外部脚本（rd_mined_materialize / 回测同源读取）零变化。
    """
    base = "daily_pv_debug" if debug else "daily_pv_all"
    if (start, end) == (
        _H5_DEFAULT_START,
        _H5_DEFAULT_END,
    ) and datasets == SUPPORTED_DATASETS:
        return f"{base}.h5"
    short = "+".join(DATASET_SHORT_NAMES[d] for d in datasets)
    return f"{base}__{start.isoformat()}_{end.isoformat()}__{short}.h5"


def l2_factor_columns(quantdb_dir: str) -> list[str]:
    """读 l2_factors 最新分区 schema，返回可并入数据面的真因子列（字母序）。

    T-MV-07 单源：挖掘数据面（``_merge_l2_factors``）与 LLM 提示面
    （``a_share._l2_referenceable_columns``）共用此清单。按字母序而非文件列序
    ——parquet 物理列序随日漂移（历史教训），依赖文件序会在某天静默换序。
    目录缺失/分区不可读返回 []（调用方降级为不接 L2，不拦挖掘）。
    """
    import pyarrow.parquet as pq

    if not quantdb_dir:
        return []
    base = Path(quantdb_dir) / "6_ml_datasets" / _L2_LIBRARY
    if not base.is_dir():
        return []
    try:
        parts = sorted(
            (d for d in base.iterdir() if d.is_dir() and d.name.startswith("dt=")),
            key=lambda d: d.name,
        )
    except OSError:
        return []
    for part in reversed(parts):  # 最新分区优先；最新分区损坏则退到早先分区
        files = sorted(part.glob("*.parquet"))
        if not files:
            continue
        try:
            names = pq.read_schema(files[0]).names
        except Exception:
            continue
        return sorted(c for c in names if c not in _L2_SKIP_COLUMNS)
    return []


def _l2_symbol_keys(series: pd.Series) -> pd.Series:
    """symbol 列 → str 键（仅非 object dtype 时转换；值相等即可跨表匹配）。"""
    return series if series.dtype == object else series.astype(str)


def _malloc_trim() -> None:
    """把 glibc 空闲堆页归还 OS（大块分配/释放 churn 后 RSS 虚高的对治）。

    2026-10-14 h5 构建 OOM 复盘：L2 分块大分配/释放留下的空闲 arena 计入
    RSS，41GB 采样里数 GB 是无法自然回收的空堆。仅 Linux/glibc 生效，任何
    异常静默跳过——纯驻留优化，与正确性无关。
    """
    try:
        import ctypes

        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:  # noqa: BLE001
        pass


def _frame_from_two_blocks(
    f64_vals: Any,
    f64_names: list[str],
    f32_vals: Any,
    f32_names: list[str],
    mi: pd.MultiIndex,
) -> pd.DataFrame:
    """pandas internals 直构「恰好两块」帧（f64 + f32），零增拷贝。

    2026-10-15 写盘侧峰值复盘：逐列插入的末帧有 ~40 个块，
    ``BlockManagerFixed.write`` 写盘前对非 consolidated 帧做 ``consolidate()``
    ——整表再拷一份（全量 ~7GB）；且末帧的块多为 df 列的视图，``del df`` 后
    列内存被末帧间接钉住（不释放）。直构恰好两种 dtype 各一块使
    ``is_consolidated() → True``，写盘免合并；块值传 (n, k) C 序存储的 ``.T``
    视图 → block.values 为 F 序，``write_array`` 的 ``.T`` 恰回连续 buffer
    （无拷贝，实测写 4.1GB 仅 +9MB RSS）。

    口径（pandas 2.3.x 实测）：block.values 形状 (k, n) 列主序；
    ``new_block_2d`` 的 placement 必须包 ``BlockPlacement``。任一环节与版本
    行为不符即抛异常——由调用方回退逐列组装（正确性优先，只损失内存优化）。

    返回帧与传入存储共享内存（零拷贝）：帧存续期间调用方不得改写存储，且
    存储须保持存活引用（帧的块只持视图）。
    """
    import numpy as np

    from pandas._libs.internals import BlockPlacement
    from pandas.core.internals import BlockManager
    from pandas.core.internals.blocks import new_block_2d

    names = list(f64_names) + list(f32_names)
    blocks = [
        new_block_2d(f64_vals.T, placement=BlockPlacement(np.arange(0, len(f64_names))))
    ]
    if f32_names:
        base = len(f64_names)
        blocks.append(
            new_block_2d(
                f32_vals.T,
                placement=BlockPlacement(np.arange(base, base + len(f32_names))),
            )
        )
    mgr = BlockManager(blocks, axes=[pd.Index(names), mi])
    return pd.DataFrame._from_mgr(mgr, axes=mgr.axes)


#: 各市场 → ``shared.qlib_paths`` 的 provider key（回测日历所在目录）
_QLIB_PROVIDER_KEY_BY_MARKET: dict[str, str] = {
    "a_share": "CN",
    "hong_kong": "HK",
    "us_stock": "US",
    "crypto": "CRYPTO",
    "futures": "FUTURES",
}


def quantdb_coverage_from_dir(
    quantdb_dir: str | os.PathLike[str] | None,
) -> Window | None:
    """QuantDB 日线覆盖窗口（挖掘数据面同源）。

    覆盖 = ``1_kline_data/daily_forward`` 分区目录 ``dt=YYYYMMDD`` 的 min/max
    ——挖掘 h5 实际读取的库，不用交易日历冒充数据覆盖。

    ``quantdb_dir`` 为空 / 该目录下没有任何分区 → ``None``（语义 = 不做覆盖
    校验、不做末端保护：宁可放行由引擎侧失败，也不做假校验）；目录不存在或
    不可读时**抛 OSError**（不吞——由调用方决定是记日志放行还是拦），2000+ 个
    分区目录的现读由调用方缓存（路由侧 TTL 300s）。
    """
    if not quantdb_dir:
        return None
    daily_dir = Path(quantdb_dir) / "1_kline_data" / "daily_forward"
    days = sorted(
        d.name[3:]
        for d in daily_dir.iterdir()
        if d.is_dir() and d.name.startswith("dt=")
    )
    if not days:
        return None
    return Window(
        date.fromisoformat(f"{days[0][:4]}-{days[0][4:6]}-{days[0][6:8]}"),
        date.fromisoformat(f"{days[-1][:4]}-{days[-1][4:6]}-{days[-1][6:8]}"),
    )


def _read_calendar_days(provider_dir: Path) -> list[date]:
    """读 qlib provider 的 ``calendars/day.txt`` → 升序交易日列表（空 = 不可用）。"""
    text = (provider_dir / "calendars" / "day.txt").read_text(encoding="utf-8")
    return [date.fromisoformat(ln.strip()) for ln in text.splitlines() if ln.strip()]


def _last_trading_day_on_or_before(day: date, days: Sequence[date]) -> date | None:
    """``days`` 中 ≤ ``day`` 的最后一个交易日；没有回 ``None``。"""
    idx = bisect_right(list(days), day) - 1
    return days[idx] if idx >= 0 else None


def qlib_backtest_data_end(
    coverage: Window | None = None, *, market: str = "a_share"
) -> date | None:
    """回测可用末端（交易日）= qlib 日历倒数第二个交易日 ∩ 数据覆盖末端。

    **为什么是倒数第二个**：qlib 回测按 t+1 成交，``TradeCalendarManager
    .get_step_time`` 取「下一根 bar」的时间步（qlib/backtest/utils.py:131），
    回测终点压在日历最后一根 bar 上必然 ``IndexError: index N is out of
    bounds for axis 0 with size N``（2026-10-10 实证：test_end=2026-10-09 崩、
    前移一个交易日通过）。这个末端恰恰是默认窗口会锚到的位置，所以不是边角
    情形，是"默认配置必然跑挂"。

    再取 min 覆盖末端：qlib 缓存（provider_uri）可能落后于 QuantDB 覆盖，
    只锚覆盖会照旧把终点推到缓存日历的最后一根 bar 上（同一个 IndexError）；
    只锚日历则会在覆盖回退时读过界。覆盖末端落在非交易日（周末/假日）时
    向前贴到最近交易日——否则 min 出来的仍是「日历最后一根 bar」之外的日子。

    读不到日历（provider 未就绪）/ 市场无对应 provider / 覆盖整体早于日历
    → ``None`` = **不做末端保护**（与 ``validate_coverage`` 同一哲学：宁可放行
    由引擎侧失败，也不做假保护）。
    """
    provider_key = _QLIB_PROVIDER_KEY_BY_MARKET.get(market)
    if not provider_key:
        return None
    try:
        from backend.shared.qlib_paths import resolve_qlib_provider_uri

        days = _read_calendar_days(Path(resolve_qlib_provider_uri(provider_key)))
    except Exception as exc:  # noqa: BLE001 - 日历读不到不拦挖掘（只记一笔）
        logger.warning("[%s] qlib 回测日历读取失败，跳过末端保护: %s", market, exc)
        return None
    if len(days) < 2:
        return None
    guard = days[-2]
    if coverage is None or coverage.end >= guard:
        return guard
    return _last_trading_day_on_or_before(coverage.end, days)


class RDLoopWrapper:
    """封装 RD-Agent FactorRDLoop，提供 QuantMind 兼容接口"""

    def __init__(self, market: str = "a_share") -> None:
        self.adapter: MarketAdapter = get_adapter(market)
        if self.adapter is None:
            raise ValueError(
                f"Unknown market: {market}. Available: {[m['market_id'] for m in list_markets()]}"
            )
        self.market = market
        self._loop = None
        self._running = False
        self._cancelled = False

    @property
    def market_name(self) -> str:
        return self.adapter.market_name

    def _freq(self) -> str:
        """本任务挖掘频率（env ``QM_MINING_FREQ``；缺省 day；非法值/市场不支持显式抛）。"""
        from backend.services.engine.alpha_agent.mining_config import (
            freq_from_env,
            normalize_freq,
        )

        return normalize_freq(freq_from_env(), market=self.market)

    def _ensure_minute_provider(self) -> Path:
        """确保 1min provider 就绪且新鲜（本次重建则记录报告），返回目录。"""
        from backend.services.engine import minute_qlib_provider as MP

        provider_dir, report = MP.ensure_minute_provider(
            universe=MP.DEFAULT_UNIVERSE,  # conf 模板钉 csi300，两者必须同池
            log=lambda msg: logger.info("[%s] %s", self.market, msg),
        )
        if report is not None:
            logger.info(
                "[%s] minute provider 构建完成：%d 只 / %d bar / %d 日（%s~%s），警告 %d 条",
                self.market,
                report.symbols,
                report.bars,
                report.days,
                report.first,
                report.last,
                len(report.warnings),
            )
            for msg in report.warnings[:10]:
                logger.warning("[%s] minute provider 警告: %s", self.market, msg)
        return provider_dir

    def _generate_minute_h5(
        self,
        provider_dir: str | os.PathLike[str],
        output_path: str,
        *,
        debug: bool = False,
    ) -> bool:
        """从分钟 provider bins 生成任务数据面 ``daily_pv.h5``（1min MultiIndex）。

        - 列 = provider 的 8 个 1min 字段（$open/$high/$low/$close/$volume/
          $amount/$vwap/$factor），与因子表达式读到的是**同一份 bin**——写代码
          时看到的数据与评估时算的数据逐位一致；
        - 窗口 = [挖掘起点, 检验终点]（自定义窗口）。分钟挖掘**必须**显式给窗口：
          默认窗口是 2016/2020 年起的日线尺度，落在 121 天分钟覆盖之外——这里
          显式拒，不静默截断成"看上去成功、实际换了个区间"；
        - debug 变体取码表前 50 只（与日线 debug 同一惯例），LLM 先在 debug 上
          试代码、全量变体跑全池；
        - 共享缓存放 provider 目录内 ``h5_cache/``：provider 重建 = 整目录原子
          换入 → 缓存随之失效，不存在「h5 与 provider 口径漂移」的中间态。
        """
        from backend.services.engine import minute_qlib_provider as MP

        custom = self._custom_windows()
        if custom is None:
            raise MinuteDataError(
                "分钟挖掘必须显式指定挖掘数据区间（QM_MINING_WINDOW_* 未设置或非法）："
                "分钟数据覆盖只有约 121 个交易日，日线尺度的默认窗口不适用"
            )
        start = custom[0].start
        end = (custom[1] or custom[0]).end
        facts = MP.minute_data_facts(provider_dir)
        if facts is None:
            raise MinuteDataError(
                f"分钟 provider meta 不可用：{Path(provider_dir) / 'meta.json'}；"
                "先构建分钟数据面（minute_qlib_provider）再来挖"
            )
        cover_start, cover_end = facts.coverage.start, facts.coverage.end
        if start < cover_start or end > cover_end:
            raise MinuteDataError(
                f"数据窗口 {start.isoformat()} ~ {end.isoformat()} 超出分钟数据覆盖 "
                f"{cover_start.isoformat()} ~ {cover_end.isoformat()}；"
                "请把区间收进覆盖范围（前端会给出可用端点）"
            )
        if not facts.contains(start, end):
            # 起点落在覆盖内但跨过空洞（或整段落在空洞里）：按可用连续块拦
            raise MinuteDataError(
                f"数据窗口 {start.isoformat()} ~ {end.isoformat()} 跨过分钟数据空洞 "
                f"（{facts.gap_text()}）；可用连续块 "
                f"{facts.usable_start.isoformat()} ~ {cover_end.isoformat()}，"
                f"共 {facts.usable_days} 个交易日"
            )
        self._minute_window = (start, end)

        # 标的清单 / 缓存路径 / 构建逻辑全部走 minute_qlib_provider 公共函数：
        # 物化面（rd_mined_materialize）按同一命名取同一份文件，两份实现各拼一个
        # 文件名 = 缓存命中内容可能不同的两份 h5（口径漂移且全程无报错）。
        try:
            symbols = MP.resolve_face_symbols(provider_dir, debug=debug)
        except MP.MinuteProviderError as exc:
            raise MinuteDataError(str(exc)) from exc

        cache_path = MP.face_h5_cache_path(provider_dir, start, end, debug=debug)
        if cache_path.is_file():
            logger.info("[%s] minute h5 cache hit: %s", self.market, cache_path)
            self._link_h5(str(cache_path), output_path)
            return True

        try:
            cache_path = MP.build_face_h5(
                provider_dir, symbols, start, end, debug=debug
            )
            self._link_h5(str(cache_path), output_path)
            logger.info(
                "[%s] Generated minute h5: %s (%d symbols, %s~%s)",
                self.market,
                cache_path,
                len(symbols),
                start,
                end,
            )
            return True
        except Exception as exc:  # noqa: BLE001 —— 生成失败如实返回 False 由调用方响亮失败
            logger.error("[%s] Failed to generate minute h5: %s", self.market, exc)
            return False

    def _configure_env(self, task_log_dir: str) -> dict[str, str]:
        """从 MarketAdapter 构建环境变量"""
        env_overrides = self.adapter.get_env_overrides()
        env = {
            **os.environ,
            **env_overrides,
            "LOG_TRACE_PATH": task_log_dir,
            "PYTHONPATH": os.getenv("PYTHONPATH") or "/app",
        }
        # 设置数据文件路径环境变量
        data_file = "/app/alphaagent/scenarios/qlib/experiment/factor_data_template/daily_pv_all.h5"
        if os.path.exists(data_file):
            env["FACTOR_DATA_PATH"] = data_file
        # Ensure critical LLM settings are present
        if not env.get("OPENAI_BASE_URL"):
            env["OPENAI_BASE_URL"] = os.getenv("OPENAI_BASE_URL", "")
        if not env.get("OPENAI_API_KEY"):
            env["OPENAI_API_KEY"] = os.getenv("AI_IDE_LLM_API_KEY", "")
        if not env.get("CHAT_MODEL"):
            env["CHAT_MODEL"] = os.getenv("CHAT_MODEL", "")
        # litellm needs provider prefix for non-OpenAI models
        model = env.get("CHAT_MODEL", "")
        if model and not model.startswith(
            ("openai/", "azure/", "anthropic/", "huggingface/")
        ):
            env["CHAT_MODEL"] = f"openai/{model}"
        env["REASONING_MODEL"] = env.get("CHAT_MODEL", "")
        env["CHAT_STREAM"] = "false"
        # 自定义窗口任务（launcher 注入 QM_MINING_WINDOW_*）：旧 QLIB_FACTOR_*
        # 变量与 QM_* 同源派生，任何下游读取方拿到的都是同一套窗口。
        custom = self._custom_windows()
        if custom is not None:
            from backend.services.engine.alpha_agent.mining_config import task_env

            # data_end：子进程再算一次末端保护（渲染进 conf 的 test 段/回测窗
            # 必须留出次日行情）。请求入口已退让过，此处幂等；兜的是手工改写
            # QM_MINING_WINDOW_* 的进程——env 里写出的端点 = 实际消费的端点。
            env.update(
                task_env(
                    custom[0],
                    custom[1],
                    datasets_from_env(),
                    data_end=qlib_backtest_data_end(market=self.market),
                )
            )
        else:
            # 无自定义窗口：保持旧静态兜底（手动运行/直跑脚本场景）
            env.setdefault(
                "QLIB_FACTOR_TRAIN_START",
                os.getenv("QLIB_FACTOR_TRAIN_START", "2016-01-01"),
            )
            # TRAIN_END 曾缺失 → FactorBasePropSetting 落到类默认 2014-12-31，
            # 与这里的 train_start 2016-01-01 组成倒置区间（train 段恒为空）。
            # 2020-12-31 与既有 valid（2021~2022）/test（2023~2025）首尾相接。
            env.setdefault(
                "QLIB_FACTOR_TRAIN_END",
                os.getenv("QLIB_FACTOR_TRAIN_END", "2020-12-31"),
            )
            env.setdefault(
                "QLIB_FACTOR_VALID_START",
                os.getenv("QLIB_FACTOR_VALID_START", "2021-01-01"),
            )
            env.setdefault(
                "QLIB_FACTOR_VALID_END",
                os.getenv("QLIB_FACTOR_VALID_END", "2022-12-31"),
            )
            env.setdefault(
                "QLIB_FACTOR_TEST_START",
                os.getenv("QLIB_FACTOR_TEST_START", "2023-01-01"),
            )
            env.setdefault(
                "QLIB_FACTOR_TEST_END", os.getenv("QLIB_FACTOR_TEST_END", "2025-12-31")
            )
        # mlflow 3.x 默认拒绝 filesystem tracking backend（"maintenance mode"），而
        # qlib 的 MLflowExpManager（qrun）与 read_exp_res.py 用的就是 ./mlruns 文件
        # 后端 → 不开此开关，qrun 在建实验时即抛 MlflowException 退出（2026-10-10
        # 实证：日志里 qrun 全部 exit 1，全盘 0 个 qlib_res.csv）。文件后端本身仍可用。
        env.setdefault("MLFLOW_ALLOW_FILE_STORE", "true")
        # 因子处理并行数
        env.setdefault("MULTI_PROC_N", os.getenv("MULTI_PROC_N", "4"))
        # 补齐 RD-Agent litellm 后端需要的 LITELLM_ 前缀变量（deepseek 优先）
        from backend.services.engine.rd_agent.llm_env import build_llm_env

        build_llm_env(env)
        return env

    # 需要注入中文/研究方向指令的 prompt key（RD-Agent prompts.yaml 中的顶层键）。
    # 这些键在 Scenario 构造时被读进 ``_background`` 并冻结——只放**静态**内容
    # （语言/方向/评估口径/池记忆）；动态内容（阶段块）挂阶段键，见下。
    _INJECT_TARGET_KEYS = ("qlib_factor_background", "qlib_quant_background")

    #: 阶段块注入目标：假设生成规范。该键在 ``QlibFactorHypothesisGen
    #: .prepare_context`` 里**每轮实时加载**——阶段块必须走这里；挂到背景键上
    #: 会永远停在「第 1 轮」（背景在任务开始时冻结）。
    _STAGE_TARGET_KEYS = ("factor_hypothesis_specification",)

    #: 因子池注入文件路径（launcher 在 spawn 前写好、经 env 传入）
    _ENV_POOL_CONTEXT = "QMF_POOL_CONTEXT_PATH"

    #: 经验记忆/禁区注入文件路径（T-FM-04；launcher 在 spawn 前原子写好、经 env 传入）
    _ENV_EXPERIENCE_CONTEXT = "QMF_EXPERIENCE_CONTEXT_PATH"

    def _build_prompt_suffix(self) -> str:
        """构造追加到因子背景 prompt 末尾的静态指令（读时一次，随背景冻结）。"""
        suffix = (
            "\n\n====== 语言要求 / Language Requirement ======\n"
            "所有因子的 description 字段必须使用中文撰写。"
            "hypothesis 和 reason 也请用中文。\n"
            "All factor descriptions MUST be written in Chinese (中文). "
            "Hypothesis and reason should also be in Chinese.\n"
        )
        direction = getattr(self, "_direction", "")
        if direction:
            suffix += (
                "\n\n====== 研究方向 / Research Direction ======\n"
                f"用户的研究方向/假设: {direction}\n"
                f"User's research direction/hypothesis: {direction}\n"
                "请围绕此方向进行因子探索。Focus factor exploration on this theme.\n"
            )
        suffix += render_eval_criteria_block(self._freq())
        suffix += self._minute_prompt_block()
        suffix += self._pool_context_block()
        suffix += self._experience_context_block()
        return suffix

    def _minute_prompt_block(self) -> str:
        """分钟任务的静态数据面说明（不含它，LLM 会按日线语义写表达式）。

        日线任务返回空串——这段文字一旦误挂到日线任务上，LLM 会按分钟语义
        构造表达式（日线数据上算出来的是另一回事），是纯误导。
        """
        from backend.services.engine.alpha_agent.mining_config import FREQ_MINUTE

        if self._freq() != FREQ_MINUTE:
            return ""
        window = getattr(self, "_minute_window", None)
        cover = (
            f"{window[0].isoformat()} ~ {window[1].isoformat()}"
            if window
            else "见数据文件说明"
        )
        return (
            "\n\n====== 分钟级挖掘 / 1-Minute Frequency ======\n"
            "本任务在 **1 分钟 bar** 上做原生因子挖掘（不是日线！）：数据文件里的索引是分钟"
            "时间戳（含 09:25 集合竞价 bar），因子表达式与生成的因子代码都在分钟截面上计算。\n"
            f"本任务数据窗口：{cover}。\n"
            "可用字段：$open / $high / $low / $close / $volume / $amount / $vwap / $factor"
            "（价格已按日复权因子调整，同一交易日内 $factor 恒定；$vwap = 成交均价）。\n"
            "时间序列算子在分钟 bar 上滚动（如 Ref($close, 1) = 上一根 1min bar，"
            "Mean($close, 20) = 最近 20 根 1min bar）；IC 口径为 1 分钟前向收益。\n"
            "注意：日线富化列（技术指标 / 估值 / 资金流 / L2 微观结构等）在分钟数据面**不存在**，"
            "引用会得到空列——请只用上面列出的分钟字段与它们的派生量构造因子。\n"
        )

    def _build_stage_block(self) -> str:
        """读时计算的阶段块（当前轮次：RD-Agent 日志 tag 优先，目录数兜底）。

        RD-Agent 每轮都会重新经 ``load_content`` 读 ``factor_hypothesis_
        specification``，此时的 ContextVar tag 形如 ``Loop_{li}.{step}``——
        据此精确判定「当前第几轮」；tag 不在（例如手工调用）才退化为数
        ``Loop_*`` 目录。任何异常由 patched_load 兜住，不拦提示词。
        """
        from .stage_prompt import current_round, rdagent_tag, render_stage_block

        cur = current_round(
            tag=rdagent_tag(), log_dir=getattr(self, "_task_log_dir", "")
        )
        return render_stage_block(
            total_loops=getattr(self, "_loop_n", None), current_loop=cur
        )

    def _pool_context_block(self) -> str:
        """历史挖掘记忆段（因子池 top-k 摘要，launcher 交付的文件）。

        只读文件、不做 DB：本函数运行在 RD-Agent 子进程内，子进程里复用主
        进程 DB engine 会跨事件循环炸（旧坑）；池查询全部在 launcher
        （engine 进程）完成，这里只消费交付物。文件缺失/为空 → 空串，
        注入是增益层，任何情况下不拦挖掘。
        """
        path = os.getenv(self._ENV_POOL_CONTEXT, "").strip()
        if not path:
            return ""
        try:
            content = Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.warning("[%s] 池注入文件读取失败(%s): %s", self.market, path, exc)
            return ""
        if not content:
            return ""
        return f"\n\n====== 历史挖掘记忆 / Past Mining Memory ======\n{content}\n"

    def _experience_context_block(self) -> str:
        """历史经验/禁区段（T-FM-04 拒因聚类 + 饱和方向，launcher 交付的文件）。

        与 ``_pool_context_block`` 同纪律：只读文件、不做 DB（本函数运行在
        RD-Agent 子进程内），文件缺失/为空 → 空串，注入失败绝不拦挖掘。
        """
        path = os.getenv(self._ENV_EXPERIENCE_CONTEXT, "").strip()
        if not path:
            return ""
        try:
            content = Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.warning(
                "[%s] 经验记忆注入文件读取失败(%s): %s", self.market, path, exc
            )
            return ""
        if not content:
            return ""
        return f"\n\n====== 经验记忆 / Experience Memory ======\n{content}\n"

    def _patch_prompts_for_chinese(self):
        """注入中文/方向/评估口径（静态）与阶段化课程（动态）到 RD-Agent 提示词。

        RD-Agent 的 prompt 存于各包内的 prompts.yaml，经 `utils.agent.tpl.load_content`
        每次按需读取（无缓存），因此在该函数返回值上追加指令是唯一可靠的注入点 ——
        早期实现 import `...experiment.prompts` 模块，但该模块并不存在，注入从未生效。

        两类注入点（详见 ``stage_prompt`` 模块 docstring）：
        - 静态后缀（语言/方向/评估口径/池记忆）→ 背景键，随 ``_background``
          在 Scenario 构造时冻结；
        - 阶段块 → ``factor_hypothesis_specification``（每轮实时加载），
          读时按当前 loop 判定阶段（``_build_stage_block``）。
        """
        try:
            from rdagent.utils.agent import tpl as _tpl

            _tpl._qm_suffix = self._build_prompt_suffix()
            _tpl._qm_stage_fn = self._build_stage_block
            if getattr(_tpl, "_qm_patched", False):
                return

            original_load = _tpl.load_content
            suffix_keys = self._INJECT_TARGET_KEYS
            stage_keys = self._STAGE_TARGET_KEYS

            def patched_load(uri: str, *args, **kwargs):
                content = original_load(uri, *args, **kwargs)
                if not isinstance(content, str):
                    return content
                if any(uri.endswith(f":{k}") for k in suffix_keys):
                    if "语言要求" in content:
                        return content
                    return content + getattr(_tpl, "_qm_suffix", "")
                if any(uri.endswith(f":{k}") for k in stage_keys):
                    if STAGE_BLOCK_MARKER in content:
                        return content
                    stage_fn = getattr(_tpl, "_qm_stage_fn", None)
                    if stage_fn is None:
                        return content
                    try:
                        return content + stage_fn()
                    except Exception as exc:  # noqa: BLE001 — 阶段块失败不拦提示词
                        logger.warning(
                            "[%s] 阶段块渲染失败（本轮跳过阶段注入）: %s",
                            self.market,
                            exc,
                        )
                        return content
                return content

            _tpl.load_content = patched_load
            _tpl._qm_patched = True
            logger.info(
                "[%s] Patched RD-Agent prompt loader (Chinese + direction=%s + stages)",
                self.market,
                bool(getattr(self, "_direction", "")),
            )
        except Exception as e:
            logger.error("Failed to patch prompts for Chinese: %s", e)

    def _create_loop(self):
        """创建 FactorRDLoop 实例"""
        from rdagent.app.qlib_rd_loop.conf import FactorBasePropSetting
        from rdagent.app.qlib_rd_loop.factor import FactorRDLoop
        from rdagent.core.utils import import_class

        # 注入中文指令
        self._patch_prompts_for_chinese()

        # qrun 前渲染 conf_*.yaml 的 Jinja 占位符：vendored RD-Agent 缺这一步，
        # 未渲染的配置是非法 YAML（详见 qlib_conf_template 模块 docstring）。
        install_qlib_execute_patch()

        prop_setting_path = self.adapter.get_prop_setting_class()
        prop_cls = import_class(prop_setting_path)
        prop_setting = prop_cls()

        loop = FactorRDLoop(prop_setting)
        return loop

    async def run(
        self,
        loop_n: int = 3,
        task_log_dir: str = "",
        direction: str = "",
    ) -> dict[str, Any]:
        """执行因子挖掘循环

        Args:
            loop_n: 循环轮数
            task_log_dir: 日志输出目录
            direction: 挖掘方向/假设

        Returns:
            包含 factors 和 metadata 的结果字典
        """
        self._running = True
        self._cancelled = False
        self._direction = direction
        # 阶段块读时取用：总轮数进课程表进度，日志目录做 tag 缺失时的兜底
        self._loop_n = loop_n
        self._task_log_dir = task_log_dir

        try:
            # 配置环境变量
            env = self._configure_env(task_log_dir)
            for k, v in env.items():
                os.environ[k] = v

            # 确保 daily_pv.h5 数据文件可用
            self._ensure_data_file(task_log_dir)

            # RD-Agent workspace / 数据目录对齐：设绝对路径 env 变量 + 切 cwd。
            # RD-Agent 的 FACTOR_COSTEER_SETTINGS.data_folder 与 workspace_path 默认用
            # Path.cwd() 相对解析（import 时冻结），必须显式指到 task_log_dir，
            # 否则因子执行时找不到 daily_pv.h5（默认 workspace 在 /tmp/git_ignore_folder/...）。
            if task_log_dir:
                os.environ["WORKSPACE_PATH"] = task_log_dir
                os.environ["FACTOR_CoSTEER_data_folder"] = os.path.join(
                    task_log_dir,
                    "git_ignore_folder",
                    "factor_implementation_source_data",
                )
                os.environ["FACTOR_CoSTEER_data_folder_debug"] = os.path.join(
                    task_log_dir,
                    "git_ignore_folder",
                    "factor_implementation_source_data_debug",
                )
                os.chdir(task_log_dir)

            logger.info(
                "[%s] RDLoop starting: market=%s, loops=%d, log_dir=%s",
                self.market,
                self.adapter.market_name,
                loop_n,
                task_log_dir,
            )

            # 创建 RDLoop
            self._loop = self._create_loop()

            # 注入 base_features_path（L1/L2 因子集供 LLM 参考）
            base_features_path = getattr(self, "_base_features_path", None)
            if base_features_path:
                self._loop._init_base_features(base_features_path)
                logger.info(
                    "[%s] Loaded base features from %s", self.market, base_features_path
                )
            step_count = len(self._loop.steps)
            total_steps = loop_n * step_count

            logger.info(
                "[%s] Steps per loop: %d, total steps: %d",
                self.market,
                step_count,
                total_steps,
            )
            logger.info(
                "[%s] Step flow: %s",
                self.market,
                " → ".join(
                    getattr(s, "__name__", s.__class__.__name__)
                    for s in self._loop.steps
                ),
            )

            # 运行循环
            t0 = time.time()
            await self._loop.run(step_n=total_steps)
            elapsed = time.time() - t0

            logger.info("[%s] RDLoop completed in %.1fs", self.market, elapsed)

            # 提取结果
            factors = self._extract_factors(task_log_dir)

            return {
                "market": self.market,
                "market_name": self.adapter.market_name,
                "loop_n": loop_n,
                "elapsed_seconds": elapsed,
                "factors": factors,
                "total_factors": len(factors),
                "log_dir": task_log_dir,
                "success": True,
            }

        except asyncio.CancelledError:
            self._cancelled = True
            logger.info("[%s] RDLoop cancelled", self.market)
            return {
                "market": self.market,
                "cancelled": True,
                "factors": [],
                "success": False,
            }

        except Exception as e:
            logger.exception("[%s] RDLoop failed: %s", self.market, e)
            return {
                "market": self.market,
                "error": str(e),
                "factors": [],
                "success": False,
            }

        finally:
            self._running = False

    def cancel(self):
        """请求取消运行"""
        self._cancelled = True
        logger.info("[%s] Cancellation requested", self.market)

    def _ensure_data_file(self, task_log_dir: str = ""):
        """确保 daily_pv.h5 数据文件和 Qlib 数据目录在 RD-Agent 期望的位置可用

        RD-Agent 的 subprocess 以 task_log_dir 为 cwd 运行，
        FACTOR_COSTEER_SETTINGS.data_folder (git_ignore_folder/factor_implementation_source_data)
        相对于 cwd 解析。需要在 task_log_dir 下创建该目录并复制数据文件。

        同时确保对应的 Qlib provider_uri 目录可用。
        """
        import shutil

        # 根据市场选择数据源
        market_data_map = {
            "crypto": {
                "source_all": "/app/db/crypto_data/5min_pv.h5",
                "source_debug": "/app/db/crypto_data/5min_pv.h5",
                "qlib_source": "/app/db/qlib_data/crypto_data",
                "qlib_target_name": "crypto_data",
            },
            "hong_kong": {
                "source_all": "/app/db/hk_data/daily_pv.h5",
                "source_debug": "/app/db/hk_data/daily_pv.h5",
                "qlib_source": "/app/db/qlib_data/hk_data",
                "qlib_target_name": "hk_data",
            },
            "us_stock": {
                "source_all": "/app/db/us_data/daily_pv.h5",
                "source_debug": "/app/db/us_data/daily_pv.h5",
                "qlib_source": "/app/db/qlib_data/us_data",
                "qlib_target_name": "us_data",
            },
            "futures": {
                "source_all": "/app/db/futures_data/daily_pv.h5",
                "source_debug": "/app/db/futures_data/daily_pv.h5",
                "qlib_source": "/data/quantfutures/.qlib_cache/futures_data",
                "qlib_target_name": "futures_data",
            },
        }

        # 默认 A 股
        market_cfg = market_data_map.get(
            self.market,
            {
                "source_all": "/app/alphaagent/scenarios/qlib/experiment/factor_data_template/daily_pv_all.h5",
                "source_debug": "/app/alphaagent/scenarios/qlib/experiment/factor_data_template/daily_pv_debug.h5",
                "qlib_source": "/data/qlib/cn_data",
                "qlib_target_name": "cn_data",
            },
        )

        source_all = market_cfg["source_all"]
        source_debug = market_cfg["source_debug"]

        # base_dir: RD-Agent subprocess cwd (task log dir)
        base_dir = task_log_dir if task_log_dir else os.getcwd()

        # RD-Agent data folders (relative to subprocess cwd)
        target_all = os.path.join(
            base_dir, "git_ignore_folder/factor_implementation_source_data/daily_pv.h5"
        )
        target_debug = os.path.join(
            base_dir,
            "git_ignore_folder/factor_implementation_source_data_debug/daily_pv.h5",
        )

        # 分钟原生挖掘（P1.5）：数据面换成 1min h5（与 provider bins 同源）；
        # **不动** ~/.qlib 全局 symlink——分钟任务的 provider_uri 由 conf 渲染层
        # 直接改写指向分钟 provider，避免把并发日线任务共用的全局指向改掉。
        # 任何一环失败都显式抛，绝不静默退回日线数据。
        freq = self._freq()
        if self.market == "a_share" and freq != "day":
            # 日线富化列（技术指标/资金流/L2）在分钟数据面不存在：选择被路由层
            # 收敛为空；这里兜手工/env 直跑的进程，如实记一笔而不是让 LLM 以为
            # 这些列还在。**读原始 env 值**（不解析成 key）：本块只做"已忽略"
            # 告知，未登记 key 原样透出即可，不为一句日志炸掉整轮挖掘。
            raw_datasets = str(os.environ.get("QM_MINING_DATASETS") or "").strip()
            if raw_datasets:
                logger.warning(
                    "[%s] 分钟模式忽略数据集选择 %s（日线富化列不出现在 1min 数据面）",
                    self.market,
                    raw_datasets,
                )
            provider_dir = self._ensure_minute_provider()
            ok_all = self._generate_minute_h5(provider_dir, target_all, debug=False)
            ok_debug = self._generate_minute_h5(provider_dir, target_debug, debug=True)
            if not (ok_all and ok_debug):
                raise MinuteDataError(
                    f"分钟数据面生成失败（provider={provider_dir}，"
                    f"all={ok_all}, debug={ok_debug}）"
                )
            # 日线富化列不在分钟数据面：不给 LLM 喂日线种子因子（引用即空值）
            self._base_features_path = None
            logger.info(
                "[%s] 分钟数据面就绪：%s（窗口 %s ~ %s）",
                self.market,
                provider_dir,
                *self._minute_window,
            )
            return

        # A 股：优先从 QuantDB parquet 生成 .h5，失败则 fallback 到预生成文件
        if self.market == "a_share":
            quantdb_dir = self._resolve_quantdb_dir()
            h5_generated = False
            if quantdb_dir:
                ok_all = self._generate_h5_from_parquet(
                    quantdb_dir, target_all, debug=False
                )
                ok_debug = self._generate_h5_from_parquet(
                    quantdb_dir, target_debug, debug=True
                )
                h5_generated = ok_all and ok_debug
            if not h5_generated:
                logger.warning(
                    "[%s] H5 generation failed or QuantDB dir missing, falling back to copy",
                    self.market,
                )
                self._copy_h5_source(source_all, target_all, source_debug, target_debug)
        elif self.market == "futures":
            # 期货：从 QuantFutures parquet 生成（无预生成文件，fut_ 前缀 instrument）
            futures_dir = self._resolve_market_data_dir("/data/quantfutures")
            h5_generated = False
            if futures_dir.is_dir():
                ok_all = self._generate_futures_h5(futures_dir, target_all, debug=False)
                ok_debug = self._generate_futures_h5(
                    futures_dir, target_debug, debug=True
                )
                h5_generated = ok_all and ok_debug
            if not h5_generated:
                logger.warning(
                    "[%s] Futures h5 generation failed or QuantFutures dir missing, falling back to copy",
                    self.market,
                )
                self._copy_h5_source(source_all, target_all, source_debug, target_debug)
        else:
            self._copy_h5_source(source_all, target_all, source_debug, target_debug)

        # Ensure Qlib provider_uri data is available at ~/.qlib/qlib_data/<market>
        qlib_source = market_cfg["qlib_source"]
        qlib_target_name = market_cfg["qlib_target_name"]

        # parquet 单源市场：优先固定目录/各市场本地派生缓存（统一走 qlib_paths）
        market_cache = {
            "a_share": ("CN", ("/data/quantdb", ".qlib_cache", "cn_data")),
            "hong_kong": ("HK", ("/data/quanthk", ".qlib_cache", "hk_data")),
            "us_stock": ("US", ("/data/quantus", ".qlib_cache", "us_data")),
        }
        if self.market in market_cache:
            mkt_key, (base_dir_cfg, cache_sub, leaf) = market_cache[self.market]
            candidates = [
                Path(base_dir_cfg) / cache_sub / leaf,
                self._resolve_market_data_dir(base_dir_cfg) / cache_sub / leaf,
            ]
            try:
                from backend.shared.qlib_paths import resolve_qlib_provider_uri

                candidates.insert(0, Path(resolve_qlib_provider_uri(mkt_key)))
            except Exception:
                pass
            for candidate in candidates:
                if candidate.is_dir():
                    qlib_source = str(candidate)
                    break

        qlib_target = os.path.expanduser(f"~/.qlib/qlib_data/{qlib_target_name}")
        if os.path.isdir(qlib_source):
            # 修复过期 symlink：若指向错误路径则重建
            need_create = False
            if os.path.islink(qlib_target):
                current_target = os.readlink(qlib_target)
                if os.path.abspath(current_target) != os.path.abspath(qlib_source):
                    logger.info(
                        "[%s] Replacing stale symlink: %s -> %s (was %s)",
                        self.market,
                        qlib_target,
                        qlib_source,
                        current_target,
                    )
                    os.unlink(qlib_target)
                    need_create = True
            elif os.path.isdir(qlib_target):
                # 真实目录而非 symlink，rename 后建 symlink
                backup = qlib_target + "_backup"
                logger.info(
                    "[%s] Replacing real directory with symlink: %s -> %s (backup: %s)",
                    self.market,
                    qlib_target,
                    qlib_source,
                    backup,
                )
                try:
                    if os.path.exists(backup):
                        shutil.rmtree(backup)
                    os.rename(qlib_target, backup)
                    need_create = True
                except Exception as e:
                    logger.warning(
                        "[%s] Failed to replace directory with symlink: %s",
                        self.market,
                        e,
                    )
            else:
                need_create = True

            if need_create:
                try:
                    os.makedirs(os.path.dirname(qlib_target), exist_ok=True)
                    os.symlink(qlib_source, qlib_target)
                    logger.info(
                        "[%s] Created symlink: %s -> %s",
                        self.market,
                        qlib_target,
                        qlib_source,
                    )
                except Exception as e:
                    logger.warning(
                        "[%s] Failed to create Qlib symlink: %s", self.market, e
                    )

        # A 股：生成 base_factors.json 供 RD-Agent LLM 参考
        if self.market == "a_share" and hasattr(
            self.adapter, "generate_base_factors_json"
        ):
            data_dir = os.path.join(
                base_dir, "git_ignore_folder", "factor_implementation_source_data"
            )
            os.makedirs(data_dir, exist_ok=True)
            bf_path = self.adapter.generate_base_factors_json(data_dir)
            if bf_path:
                self._base_features_path = data_dir
                logger.info("[%s] base_factors.json ready at %s", self.market, data_dir)
            else:
                self._base_features_path = None
        else:
            self._base_features_path = None

    def _copy_h5_source(self, source_all, target_all, source_debug, target_debug):
        """从预生成的 h5 文件复制。"""
        for target, source in [(target_all, source_all), (target_debug, source_debug)]:
            if not os.path.exists(source):
                logger.warning(
                    "[%s] Source data file not found: %s", self.market, source
                )
                continue
            target_dir = os.path.dirname(target)
            os.makedirs(target_dir, exist_ok=True)
            if not os.path.exists(target) or os.path.getmtime(
                source
            ) > os.path.getmtime(target):
                try:
                    shutil.copy2(source, target)
                    logger.info(
                        "[%s] Copied data file: %s -> %s", self.market, source, target
                    )
                except Exception as e:
                    logger.warning(
                        "[%s] Failed to copy data file to %s: %s",
                        self.market,
                        target,
                        e,
                    )

    @staticmethod
    def _resolve_quantdb_dir() -> str | None:
        """解析 QuantDB 数据目录，返回存在的路径或 None。"""
        env_dir = os.getenv("QM_QUANTDB_DATA_DIR", "").strip()
        candidates = [env_dir] if env_dir else []
        candidates += ["/data/quantdb", "/app/data/quantdb"]
        for d in candidates:
            if d and os.path.isdir(d):
                return d
        return None

    def _custom_windows(self) -> tuple[Window, Window | None] | None:
        """任务自定义窗口（env ``QM_MINING_WINDOW_*``）→ (mining, validation)。

        未设 = ``None``（调用方走旧默认路径，行为零变化）。env 非法时记 error
        并退回 ``None``：请求入口（/evolve）已做同样校验，这里只是防手动运行
        的进程拿到半截/倒置配置后静默按错窗口挖。
        """
        try:
            from backend.services.engine.alpha_agent.mining_config import (
                window_from_env,
            )

            return window_from_env()
        except ValueError as exc:
            logger.error(
                "[%s] QM_MINING_WINDOW_* 配置非法，退回默认窗口: %s", self.market, exc
            )
            return None

    @staticmethod
    def _resolve_market_data_dir(container_path: str) -> Path:
        """解析市场本地 parquet 数据目录（容器挂载 /data/quantX）。

        支持 env 覆盖（QM_QUANTHK_DATA_DIR 等）；容器内为 /data/quantX，
        宿主机构回退到项目 data/quantX。返回不存在的候选也不报错，
        由调用方用 .is_dir() 判断。
        """
        env_map = {
            "/data/quanthk": "QM_QUANTHK_DATA_DIR",
            "/data/quantus": "QM_QUANTUS_DATA_DIR",
            "/data/quantbc": "QM_QUANTBC_DATA_DIR",
            "/data/quantdb": "QM_QUANTDB_DATA_DIR",
            "/data/quantfutures": "QM_QUANTFUTURES_DATA_DIR",
        }
        env_name = env_map.get(container_path)
        if env_name:
            env_val = os.getenv(env_name, "").strip()
            if env_val:
                return Path(env_val)
        project_data = (
            Path(__file__).resolve().parents[4] / "data" / Path(container_path).name
        )
        return project_data

    def _generate_h5_from_parquet(
        self,
        quantdb_dir: str,
        output_path: str,
        *,
        debug: bool = False,
        symbols_limit: int | None = None,
        end_date: date | None = None,
        start_date: date | None = None,
        use_cache: bool = True,
    ) -> bool:
        """从 QuantDB parquet 生成 RD-Agent 期望的 daily_pv.h5 文件。

        H5 格式:
        - Key: "data"
        - Index: MultiIndex [datetime, instrument]
        - Columns: ["$open", "$high", "$low", "$close", "$volume", "$amount",
          "$factor", <L2 微观结构因子列 ...>, <QuantDB 富化列 ...>]
          （L2 全量列见 ``l2_factor_columns``，T-MV-07 起并入；读取方
          一律按列名取数，两块的列序/块序不是契约）
        - instrument 格式: Qlib 格式 sh600036

        常规模式（``use_cache=True``，默认）：生成的富化文件先写入共享缓存
        ``<quantdb_dir>/.h5_cache/``（按最新分区自动失效），再硬链/软链到任务
        目录，避免每个挖掘任务重复生成 GB 级文件；生成失败回退旧缓存不阻断任务。

        探测模式（``use_cache=False``，T-MV-09 截断不变性探针用）：确定性抽样
        ``symbols_limit`` 只标的（排序后取前 N，两次生成必须同一批标的）、数据
        截断到 ``end_date``，**直写 ``output_path``**——不落共享缓存、不建链接、
        失败不降级旧缓存。旧全量缓存冒充「截断数据」会让「全量 vs 截断」对比
        退化成「全量 vs 全量」假通过，故此处失败即返回 False。

        尾窗模式（``start_date``）：把数据面起点前移到指定日（默认窗口仍为
        ``_H5_DEFAULT_START``）。缓存身份含窗口，因此尾窗缓存与全量缓存各占
        一个文件；调用方负责保证 ``start_date`` 覆盖因子最大回看。自定义挖掘
        窗口（``QM_MINING_WINDOW_*``）优先于 ``start_date``。

        Returns:
            True if h5 file was generated/already current, False on failure.
        """
        cache_dir = os.path.join(quantdb_dir, ".h5_cache")

        # 数据面窗口 = 数据必须覆盖的全跨度（挖掘起点 → 检验终点）：因子值要在
        # 实验分段（含检验段）上算得出来，只给挖掘窗会缺检验段。无自定义窗口时
        # 用旧默认跨度，行为逐位不变。
        custom = self._custom_windows()
        if custom is None:
            start_d = start_date or _H5_DEFAULT_START
            default_end = _H5_DEFAULT_END
        else:
            start_d = custom[0].start
            default_end = (custom[1] or custom[0]).end
        end_d = end_date or default_end
        datasets = datasets_from_env()
        cache_path = os.path.join(
            cache_dir, _h5_cache_name(debug, start_d, end_d, datasets)
        )

        # 共享缓存命中：直接链接到任务目录（避免重复生成）；探测模式禁用
        if (
            use_cache
            and os.path.exists(cache_path)
            and self._h5_cache_fresh(quantdb_dir, cache_path, datasets=datasets)
        ):
            logger.info("[%s] h5 cache hit: %s", self.market, cache_path)
            self._link_h5(cache_path, output_path)
            return True

        try:
            from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

            hub = QuantDBDataHub(quantdb_dir)
            if not hub.available:
                logger.error(
                    "[%s] QuantDBDataHub not available for h5 generation", self.market
                )
                return self._use_stale_cache(cache_path, output_path, allow=use_cache)

            # 获取股票列表
            df_stocks = hub.fetch_stock_list()
            if df_stocks.empty:
                logger.error("[%s] No stock list from QuantDB", self.market)
                return self._use_stale_cache(cache_path, output_path, allow=use_cache)

            symbol_col = "Symbol" if "Symbol" in df_stocks.columns else "symbol"
            symbols = df_stocks[symbol_col].dropna().unique()

            if debug:
                # Debug 模式：只取少量 symbol
                symbols = symbols[:50]

            import numpy as np

            symbols = [str(s) for s in symbols]
            if symbols_limit is not None:
                # 探测模式确定性抽样：排序后取前 N——全量/截断两次生成必须
                # 拿到同一批标的，否则探针对比无意义（set 迭代序不保证）
                symbols = sorted(symbols)[: int(symbols_limit)]

            # 批量读取：一次查全部 symbol，避免逐股票 N 次分区扫描
            # （早期实现对 ~5400 只股票各查 2 次，单次生成需 40 分钟以上）
            df = hub.fetch_daily_kline_batch(symbols, start_d, end_d, adjust="qfq")
            hub.release_resources()  # DuckDB 连接保活最后一次结果（~GB 级）
            if df is None or df.empty:
                logger.error("[%s] No K-line data read from QuantDB", self.market)
                return self._use_stale_cache(cache_path, output_path, allow=use_cache)
            df_unadj = hub.fetch_daily_kline_batch(
                symbols, start_d, end_d, adjust="none"
            )
            hub.release_resources()

            # 前复权价可能为负（高分红股票多年除权后 qfq 价转负），会污染 Qlib 因子
            # 计算，这里整体剔除这些行。
            valid = df["close"].to_numpy(dtype="float64") > 0
            dropped = int((~valid).sum())
            if dropped:
                logger.warning(
                    "[%s] Dropped %d rows with non-positive qfq close (negative 前复权价)",
                    self.market,
                    dropped,
                )
                df = df.loc[valid].reset_index(drop=True)
            if df.empty:
                logger.error(
                    "[%s] No positive-price K-line rows from QuantDB", self.market
                )
                return self._use_stale_cache(cache_path, output_path, allow=use_cache)

            # 按 (symbol, trade_date) 对齐不复权收盘价以计算 $factor
            if df_unadj is not None and not df_unadj.empty:
                unadj = df_unadj[["symbol", "trade_date", "close"]].rename(
                    columns={"close": "_close_unadj"}
                )
                df = df.merge(unadj, on=["symbol", "trade_date"], how="left")
                factor = np.where(
                    df["_close_unadj"].to_numpy(dtype="float64", na_value=0.0) > 0,
                    df["close"].to_numpy(dtype="float64")
                    / df["_close_unadj"].to_numpy(dtype="float64", na_value=1.0),
                    1.0,
                )
            else:
                factor = np.ones(len(df))

            # ── 行序预排（2026-10-14 OOM 修复）──────────────────────────
            # 终帧 index=(datetime, qlib-instrument) 必须有序。旧实现在组装
            # 末尾 sort_index()——对 L2 全量后 ~8GB 的整表再复制一份。改为在
            # K 线小表阶段（enrich/L2 并入前，~1.5GB）按同一排序键排一次：
            # merge/直写块都保持左表行序，组装即有序、免排序。排序键必须与
            # sort_index 语义逐位一致：instrument 是 qlib 形（bj… < sh… <
            # sz…，不能按原始后缀码排——跨交易所会错序），故二级键 = qlib 名
            # 全市场字典序位次；一级键 = trade_date 的 int64 纳秒。折算失败
            # （异常 dtype/NaN 符号）回退旧路径：组装末尾兜底 sort_index，
            # 正确性优先，只损失内存优化。
            if "_close_unadj" in df.columns:
                df = df.drop(columns=["_close_unadj"])
            sym_series = df["symbol"]
            if sym_series.dtype != object:
                sym_series = sym_series.astype(str)
            sym_codes, sym_uniq = pd.factorize(sym_series)
            qlib_sorted = np.array(
                sorted(self._to_qlib_symbol(str(s)) for s in sym_uniq), dtype=object
            )
            presorted = bool((sym_codes >= 0).all())
            if presorted:
                qlib_rank_of = {s: r for r, s in enumerate(qlib_sorted)}
                rank_by_code = np.fromiter(
                    (qlib_rank_of[self._to_qlib_symbol(str(s))] for s in sym_uniq),
                    dtype=np.int64,
                    count=len(sym_uniq),
                )
                row_sym_rank = rank_by_code[sym_codes]
                try:
                    date_i8 = (
                        pd.to_datetime(df["trade_date"]).astype("int64").to_numpy()
                    )
                except (TypeError, ValueError) as exc:
                    logger.warning(
                        "[%s] trade_date 无法折算 int64，组装回退 sort_index: %s",
                        self.market,
                        exc,
                    )
                    presorted = False
            if presorted:
                order = np.lexsort((row_sym_rank, date_i8))
                df = df.take(order).reset_index(drop=True)
                factor = factor[order]
                row_sym_rank = row_sym_rank[order]

            # 合并 QuantDB 富化列（技术指标/估值/资金流/筹码等），供因子直接引用
            # （子集模式=debug 或探测抽样：只拉这批标的的富化数据）
            enrich_symbols = symbols if (debug or symbols_limit is not None) else None
            df = self._merge_enrich(
                df, hub, start_d, end_d, enrich_symbols, datasets=datasets
            )

            # L2 微观结构因子全量接入（T-MV-07）：列清单现场读盘，分块 fetch →
            # 按键定位直写预分配 float32 大块（2026-10-14 起不再逐块 merge——
            # 整表级中间拷贝是 41GB 峰值的主源）；另按 symbol 分片 + 每片释放
            # 连接（二修：整市场单查询的在途瞬态与连接驻留合计 ~10GB）。数据集
            # 选择不含 L2 时整段跳过（选择必须真正控制喂给 LLM 的列）
            #
            # 组装侧 v4（2026-10-15）：f32 存储提前到此分配（宽度 = L2 现读
            # 列数 + 富化列数），L2 直写其前段、富化列补尾段——L2 值落进终帧
            # 存储零搬运；无 L2（未选/清单空）时宽度退化为富化列数，直构路径
            # 不受影响。清单竞态/跳块致 L2 未落存储 → 弃用（整体回退组装）。
            enrich_cols = [c for c in _ENRICH_OUTPUT_COLUMNS if c in df.columns]
            if L2_DATASET in datasets:
                planned = l2_factor_columns(quantdb_dir)
                if not planned:
                    logger.info("[%s] l2_factors 列清单为空，跳过 L2 接入", self.market)
            else:
                planned = []
                logger.info(
                    "[%s] 数据集选择不含 %s，跳过 L2 接入", self.market, L2_DATASET
                )
            f32_vals = np.full(  # (n, l2+enrich) float32 终帧存储（预分配直写）
                (len(df), len(planned) + len(enrich_cols)), np.nan, dtype="float32"
            )
            l2_block, l2_cols = None, []
            if planned:
                l2_view = f32_vals[:, : len(planned)]
                l2_block, l2_cols = self._merge_l2_factors(
                    df, hub, quantdb_dir, start_d, end_d, symbols, block=l2_view
                )
                if l2_block is not l2_view:
                    # L2 值未落预分配存储 → 弃用；整体回退逐列组装
                    f32_vals = None
                    l2_view = None

            # ── 组装 v4（2026-10-15）────────────────────────────────────
            # v3 教训（微探针实测）：逐列插入的末帧 ~40 块 → to_hdf 前
            # consolidate() 把整表再拷一份（~7GB）；且末帧块多为 df 列的
            # 视图、del df 不解钉（「CoW 让 del df 省一份」的旧注释不成立：
            # pandas 2.3 该开关默认关，列是真拷贝/视图钉住）。v4：f64/f32
            # 两块存储前置预分配（L2 直写前段 + 富化补尾段），再经 internals
            # 直构恰好两块帧——is_consolidated()=True 写盘免合并，块值为
            # (n,k) 存储的 .T 视图（F 序）写盘零拷贝；df 只被读取、真释放。
            # internals 私有 API 失配时回退逐列插入——正确性优先，损失内存
            # 优化不损失语义。
            if presorted:
                instruments = qlib_sorted[row_sym_rank]
            else:
                instruments = [self._to_qlib_symbol(str(s)) for s in df["symbol"]]
            mi = pd.MultiIndex.from_arrays(
                [pd.to_datetime(df["trade_date"]), instruments],
                names=["datetime", "instrument"],
            )
            f64_names = ["$open", "$high", "$low", "$close", "$volume"]
            f64_src = ["open", "high", "low", "close", "volume"]
            if "amount" in df.columns:
                f64_names.append("$amount")
                f64_src.append("amount")
            f64_names.append("$factor")
            f64_vals = np.empty((len(df), len(f64_names)), dtype="float64")
            for j, c in enumerate(f64_src):
                f64_vals[:, j] = df[c].to_numpy(dtype="float64")
            f64_vals[:, -1] = factor

            combined = None
            if f32_vals is not None:
                # 快路径：L2 已直写 f32_vals 前段，这里只补富化尾段
                for i, c in enumerate(enrich_cols):
                    f32_vals[:, len(l2_cols) + i] = df[c].to_numpy(dtype="float32")
                try:
                    combined = _frame_from_two_blocks(
                        f64_vals,
                        f64_names,
                        f32_vals,
                        [f"${c}" for c in l2_cols] + [f"${c}" for c in enrich_cols],
                        mi,
                    )
                except Exception as exc:  # noqa: BLE001 私有 API 失配即回退
                    logger.warning(
                        "[%s] pandas internals 直构失败，回退逐列组装: %s",
                        self.market,
                        exc,
                    )
                    combined = None
            if combined is None:
                # 回退：逐列插入（与 v3 同形；罕见路径，内存贵）
                if l2_block is not None:
                    combined = pd.DataFrame(
                        l2_block,
                        index=mi,
                        columns=[f"${c}" for c in l2_cols],
                        copy=False,
                    )
                else:
                    combined = pd.DataFrame(index=mi)
                for j, name in enumerate(f64_names):
                    combined[name] = f64_vals[:, j]
                for c in enrich_cols:
                    combined[f"${c}"] = df[c].to_numpy(dtype="float32")
            del df, instruments
            if not mi.is_monotonic_increasing:
                combined = combined.sort_index()

            if use_cache:
                target = cache_path
                os.makedirs(cache_dir, exist_ok=True)
            else:
                # 探测模式：直写 output_path（原子替换），不落共享缓存不建链接
                target = output_path
                os.makedirs(
                    os.path.dirname(os.path.abspath(target)) or ".", exist_ok=True
                )
            # 写盘前把组装期释放的空堆归还 OS（v4 直构后写盘本身零拷贝，
            # 这里回收的是 L2 分块 fetch/回退路径留下的空闲 arena）
            _malloc_trim()
            tmp_path = target + ".tmp"
            combined.to_hdf(tmp_path, key="data", mode="w")
            os.replace(tmp_path, target)
            if use_cache:
                self._link_h5(cache_path, output_path)
            logger.info(
                "[%s] Generated enriched h5 from parquet: %s (%d rows, %d cols)",
                self.market,
                target,
                len(combined),
                combined.shape[1],
            )
            return True

        except Exception as exc:
            logger.error(
                "[%s] Failed to generate h5 from parquet: %s", self.market, exc
            )
            return self._use_stale_cache(cache_path, output_path, allow=use_cache)

    def _merge_enrich(
        self,
        df: pd.DataFrame,
        hub: Any,
        start: date,
        end: date,
        symbols: list[str] | None,
        *,
        datasets: Sequence[str] | None = None,
    ) -> pd.DataFrame:
        """把 QuantDB 富化列按 (symbol, trade_date) 左连接到 K 线 DataFrame。

        ``datasets`` = 任务选择的数据集（None 时读 env ``QM_MINING_DATASETS``，
        未设=全选）；只合并选中数据集，未选中的整段不读不并——选择要真正控制
        喂给 LLM 的列，而非只记 provenance。
        """
        selected = datasets_from_env() if datasets is None else tuple(datasets)
        for dataset in ("features_daily", "l1_factors"):
            mapping = ENRICH_BY_DATASET[dataset]
            if dataset not in selected or mapping is None:
                continue  # 未选中的数据集不读不并
            try:
                sub = hub.fetch_ml_columns(
                    dataset, list(mapping.keys()), start, end, symbols=symbols
                )
                hub.release_resources()  # 连接保活查询结果 ~GB 级，取完即还
            except Exception as exc:
                logger.warning(
                    "[%s] Enrich from %s failed: %s", self.market, dataset, exc
                )
                continue
            if sub is None or sub.empty:
                logger.warning("[%s] No enrich data from %s", self.market, dataset)
                continue
            sub = sub.rename(columns=mapping)
            sub["trade_date"] = pd.to_datetime(sub["trade_date"])
            keep = [
                c
                for c in ("symbol", "trade_date", *mapping.values())
                if c in sub.columns
            ]
            df = df.merge(sub[keep], on=["symbol", "trade_date"], how="left")
            logger.info(
                "[%s] Enriched h5 with %d cols from %s",
                self.market,
                len(keep) - 2,
                dataset,
            )
        return df

    def _merge_l2_factors(
        self,
        df: pd.DataFrame,
        hub: Any,
        quantdb_dir: str,
        start: date,
        end: date,
        symbols: list[str] | None,
        block: Any = None,
    ) -> tuple[Any, list[str]]:
        """把 l2_factors 全量因子列按 (symbol, trade_date) 左对齐进预分配大块（T-MV-07）。

        2026-10-14 OOM 事故后重写：旧版逐块 ``df.merge``——每块都把**已累计的
        整表**复制一份（全量后 df ~9GB，尾块瞬时 ×2），叠加组装阶段两份整表拷贝，
        实测把 h5 构建峰值顶到 41GB+（45GiB 容器两次 -9）。新版改为
        ``Index.get_indexer`` 按键定位：每块 fetch 后只做一次键查找，把列值花式
        索引直写进一次预分配的 (nrows × ncols) float32 大块——全流程无整表级
        中间拷贝（峰值 ≈ 大块 6.7GB + df ~1.5GB + 单块 ~0.6GB）。

        2026-10-14 二修（连接驻留 + 在途瞬态）：实测整市场单查询的 DuckDB
        fetchdf 会在连接里驻留结果 ~4.4GB（del/gc/malloc_trim 均不还），且
        查询在途另有 ~6GB 瞬态——两者叠加会把构建峰值重新顶高。修法：按
        ``_L2_SYMBOL_SHARD`` 分片取数（在途瞬态 ÷ 分片数），每片 fetch 后立即
        ``hub.release_resources()`` 关闭连接（驻留归零）；`symbols` 传全市场
        清单（None 时不带 WHERE，兼容旧调用）。

        2026-10-15 三修（组装侧直写，v4）：``block`` 传入调用方预分配存储的
        前段视图（形状必须 =(nrows, 现场列数)、float32），L2 值直接落终帧
        f32 存储、组装零搬运。命中时返回块 **is** 传入视图；形状不符（列清单
        竞态）或分块有跳过（收窄拷贝）时改用内部块——调用方以
        ``返回块 is 传入视图`` 判预分配路径是否成立，不成立即回退。

        语义与原左连接逐位等价：以 df 行为准、未覆盖行 NaN；重复右键（HK
        daily_forward 双来源旧坑同型）令 get_indexer 抛 InvalidIndexError →
        整块跳过（等价旧版按行数膨胀的判据）；任一块失败只跳过该块，跳过的列
        不出现在返回清单（= 不进缓存）。

        Returns:
            (block, merged)：block 行序与 df 一一对应、列序同 merged，dtype
            float32；无列可并时 (None, [])。
        """
        import numpy as np

        cols = l2_factor_columns(quantdb_dir)
        if not cols:
            logger.warning("[%s] l2_factors 列清单为空，跳过 L2 接入", self.market)
            return None, []
        n_rows = len(df)
        if (
            block is None
            or getattr(block, "shape", None) != (n_rows, len(cols))
            or getattr(block, "dtype", None) != np.dtype("float32")
        ):
            # 未给存储 / 宽度按调用方清单预留但现场列数已变（竞态）→ 自分配；
            # 身份判据（返回块 is 视图）由此自然失效，调用方走回退组装
            block = np.full((n_rows, len(cols)), np.nan, dtype="float32")
        keys_df = pd.MultiIndex.from_arrays(
            [pd.to_datetime(df["trade_date"]), _l2_symbol_keys(df["symbol"])]
        )
        shards: list[list[str] | None]
        if symbols:
            shards = [
                list(symbols[i : i + _L2_SYMBOL_SHARD])
                for i in range(0, len(symbols), _L2_SYMBOL_SHARD)
            ]
        else:
            shards = [None]
        kept: list[int] = []
        kept_seen: set[int] = set()
        merged: list[str] = []
        for i in range(0, len(cols), _L2_FETCH_CHUNK):
            chunk = cols[i : i + _L2_FETCH_CHUNK]
            for shard in shards:
                try:
                    sub = hub.fetch_ml_columns(
                        _L2_LIBRARY, chunk, start, end, symbols=shard
                    )
                    hub.release_resources()  # 连接保活查询结果 ~GB 级，取完即还
                except Exception as exc:
                    logger.warning(
                        "[%s] Enrich from %s failed (chunk %d): %s",
                        self.market,
                        _L2_LIBRARY,
                        i,
                        exc,
                    )
                    continue
                if sub is None or sub.empty:
                    logger.warning(
                        "[%s] No enrich data from %s (chunk %d)",
                        self.market,
                        _L2_LIBRARY,
                        i,
                    )
                    continue
                sub["trade_date"] = pd.to_datetime(sub["trade_date"])
                present = [c for c in chunk if c in sub.columns]
                try:
                    idx = pd.MultiIndex.from_arrays(
                        [sub["trade_date"], _l2_symbol_keys(sub["symbol"])]
                    ).get_indexer(keys_df)
                except Exception as exc:  # InvalidIndexError（右键重复）等
                    logger.error(
                        "[%s] %s chunk %d key lookup failed (dup keys?); skipped: %s",
                        self.market,
                        _L2_LIBRARY,
                        i,
                        exc,
                    )
                    continue
                missing = idx < 0
                hit = ~missing
                for c in present:
                    # 花式索引总产出可写新数组；-1（查无）先回绕写入错值再整段置 NaN
                    vals = sub[c].to_numpy(dtype="float32")[idx]
                    j = i + chunk.index(c)
                    if missing.any():
                        vals[missing] = np.nan
                        # 分片取数：片外行（本片查无）由其它片供给——只写本片
                        # 命中行；整列覆写会把前一片已写入的结果抹回 NaN
                        block[hit, j] = vals[hit]
                    else:
                        block[:, j] = vals
                    if j not in kept_seen:  # 同一列可能出现在多个分片
                        kept_seen.add(j)
                        kept.append(j)
                        merged.append(c)
                del sub, idx
                _malloc_trim()
        if not merged:
            return None, []
        if len(kept) == len(cols):
            slim = block
        else:
            # 有块被跳过：整宽块裁成实际并入列（罕见路径，一次拷贝可接受）
            slim = block[:, kept]
            del block
        _malloc_trim()
        logger.info(
            "[%s] Enriched h5 with %d L2 factor cols from %s",
            self.market,
            len(merged),
            _L2_LIBRARY,
        )
        return slim, merged

    @staticmethod
    def _latest_partition_mtime(quantdb_dir: str, rel_path: str) -> float:
        """返回某数据集最新分区内 parquet 的最大 mtime（无则 0）。"""
        base = Path(quantdb_dir) / rel_path
        if not base.is_dir():
            return 0.0
        try:
            dirs = [
                d for d in base.iterdir() if d.is_dir() and d.name.startswith("dt=")
            ]
            if not dirs:
                return 0.0
            latest = max(dirs, key=lambda d: d.name)
            mtimes = [f.stat().st_mtime for f in latest.glob("*.parquet")]
            return max(mtimes) if mtimes else latest.stat().st_mtime
        except OSError:
            return 0.0

    @staticmethod
    def _h5_columns(cache_path: str) -> set[str]:
        """读 fixed 格式 h5 的列名集合（pytables 轴数组；损坏/缺失返回空集）。

        axis0 在本仓缓存布局里即列名（含 $ 前缀）；union 两轴是防布局漂移的
        兜底——读空 → 覆盖检查失败 → 重生成（安全方向：宁重算不放行）。
        """
        import tables

        names: set[str] = set()
        try:
            with tables.open_file(cache_path, mode="r") as f:
                for axis in ("axis0", "axis1"):
                    node = getattr(f.root.data, axis, None)
                    if node is None:
                        continue
                    try:
                        names.update(
                            c.decode() if isinstance(c, bytes) else str(c)
                            for c in node[:]
                        )
                    except Exception:
                        continue
        except Exception:
            return set()
        return names

    @staticmethod
    def _h5_cache_fresh(
        quantdb_dir: str,
        cache_path: str,
        *,
        datasets: Sequence[str] | None = None,
    ) -> bool:
        """判断共享缓存是否仍是最新（``datasets`` 缺省=全选，即迁移前语义）。

        mtime 维度：K 线 + **已选数据集**的最新分区均不晚于缓存（未选中的数据
        集更新不影响这份缓存的构成，不参与判定）；列覆盖维度：L2 接入
        （T-MV-07）改的是「数据面有哪些列」——存量缓存在此之前生成时 mtime 仍
        可能新鲜但缺整批 L2 列，必须按 schema 重生成。L2 覆盖检查只在 **L2 被
        选中且本机有 L2 库**时生效（未选 L2 的缓存没有 L2 列是正确形态；部分块
        失败留下的残面会在下次启动重试补齐）。
        """
        try:
            cache_mtime = os.path.getmtime(cache_path)
        except OSError:
            return False
        selected = datasets_from_env() if datasets is None else tuple(datasets)
        sources = ["1_kline_data/daily_forward"]
        sources += [
            f"6_ml_datasets/{name}"
            for name in ("features_daily", "l1_factors", L2_DATASET)
            if name in selected
        ]
        latest = max(
            RDLoopWrapper._latest_partition_mtime(quantdb_dir, rel) for rel in sources
        )
        if not (latest > 0 and cache_mtime >= latest):
            return False
        if L2_DATASET not in selected:
            return True  # 未选 L2：这份缓存不含 L2 列是正确的
        l2_expected = l2_factor_columns(quantdb_dir)
        if not l2_expected:
            return True  # 本机无 L2 库：无覆盖义务
        present = RDLoopWrapper._h5_columns(cache_path)
        return all(f"${c}" in present for c in l2_expected)

    @staticmethod
    def _link_h5(src: str, dst: str) -> None:
        """把共享缓存的 h5 链接（优先硬链）到任务目录，失败则复制。"""
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            if os.path.islink(dst) or os.path.exists(dst):
                os.remove(dst)
        except OSError:
            pass
        for linker in (
            lambda: os.link(src, dst),
            lambda: os.symlink(os.path.abspath(src), dst),
            lambda: shutil.copy2(src, dst),
        ):
            try:
                linker()
                return
            except OSError:
                continue
            except Exception:
                continue
        logger.warning("[RDLoopWrapper] Failed to link/copy h5 %s -> %s", src, dst)

    def _use_stale_cache(
        self, cache_path: str, output_path: str, *, allow: bool = True
    ) -> bool:
        """生成失败时回退到已有（可能过期）缓存，尽量不阻断任务。

        ``allow=False``（探测 h5 直建，``use_cache=False``）绝不降级：调用方
        要的是「截断到探针日」的数据面，递旧的全量缓存会让截断探针对比变成
        「全量 vs 全量」→ 假通过。此处是防假通过的收口点。
        """
        if not allow:
            return False
        if os.path.exists(cache_path):
            logger.warning("[%s] Reusing stale h5 cache: %s", self.market, cache_path)
            self._link_h5(cache_path, output_path)
            return True
        return False

    @staticmethod
    def _to_qlib_symbol(symbol: str) -> str:
        """suffix 格式 600036.SH -> Qlib 格式 sh600036。"""
        s = symbol.strip()
        if "." in s:
            code, exchange = s.split(".", 1)
            return f"{exchange.lower()}{code}"
        return s.lower()

    def _generate_futures_h5(
        self, futures_dir: Path | str, output_path: str, *, debug: bool = False
    ) -> bool:
        """从 QuantFutures parquet 生成 RD-Agent 的 daily_pv.h5。

        与 A 股版区别:
        - instrument 用 fut_ 前缀（对齐 QlibDataBuilder._MARKET_QLIB_PREFIX）
        - 期货无复权概念，$factor 恒为 1
        - 行情从 2016 年起量价才完整，起点对齐 Qlib 因子训练窗口
        """
        import numpy as np

        if os.path.exists(output_path):
            try:
                h5_mtime = os.path.getmtime(output_path)
                kline_dir = Path(futures_dir) / "1_kline_data" / "daily_forward"
                if kline_dir.is_dir():
                    partitions = sorted(
                        d for d in os.listdir(kline_dir) if d.startswith("dt=")
                    )
                    if partitions:
                        latest_parquet = kline_dir / partitions[-1] / "data.parquet"
                        if (
                            os.path.exists(latest_parquet)
                            and os.path.getmtime(latest_parquet) > h5_mtime
                        ):
                            logger.info(
                                "[%s] Parquet newer than h5, regenerating: %s",
                                self.market,
                                output_path,
                            )
                        else:
                            return True
                    else:
                        return True
                else:
                    return True
            except Exception:
                return True

        try:
            from backend.services.engine.data_platform.quantfutures_hub import (
                QuantFuturesDataHub,
            )

            hub = QuantFuturesDataHub(Path(futures_dir))
            if not hub.available:
                logger.error(
                    "[%s] QuantFuturesDataHub not available for h5 generation",
                    self.market,
                )
                return False

            # 期货无 instrument_detail parquet，从 daily_forward 分区推导 symbol 列表
            symbols: set[str] = set()
            kline_dir = Path(futures_dir) / "1_kline_data" / "daily_forward"
            if not kline_dir.is_dir():
                logger.error("[%s] QuantFutures daily_forward missing", self.market)
                return False
            import duckdb

            con = duckdb.connect(config={"memory_limit": "4GB", "threads": "2"})
            try:
                df_syms = con.execute(
                    f"SELECT DISTINCT symbol FROM read_parquet('{kline_dir / 'dt=*' / 'data.parquet'}', hive_partitioning=1)"
                ).fetchdf()
            finally:
                con.close()
            symbols = {str(s) for s in df_syms["symbol"].dropna().unique()}
            if not symbols:
                logger.error("[%s] No futures symbols found in parquet", self.market)
                return False

            symbol_list = sorted(symbols)
            if debug:
                symbol_list = symbol_list[:50]

            # 行情起点对齐 Qlib 因子训练窗口（2016 起量价完整）
            df = hub.fetch_daily_kline_batch(
                symbol_list, date(2016, 1, 1), date(2026, 12, 31), adjust="qfq"
            )
            if df is None or df.empty:
                logger.error("[%s] No futures K-line data read", self.market)
                return False

            # 剔除无效行情行（价格 <= 0 或非有限值会污染因子计算）
            cols = ["open", "high", "low", "close"]
            valid = np.isfinite(df[cols].to_numpy(dtype="float64")).all(axis=1) & (
                df[cols].to_numpy(dtype="float64") > 0
            ).all(axis=1)
            dropped = int((~valid).sum())
            if dropped:
                logger.warning(
                    "[%s] Dropped %d invalid futures rows", self.market, dropped
                )
                df = df.loc[valid].reset_index(drop=True)
            if df.empty:
                logger.error("[%s] No valid futures K-line rows", self.market)
                return False

            instruments = [f"fut_{s}" for s in df["symbol"]]
            combined = pd.DataFrame(
                {
                    "$open": df["open"].to_numpy(dtype="float64"),
                    "$high": df["high"].to_numpy(dtype="float64"),
                    "$low": df["low"].to_numpy(dtype="float64"),
                    "$close": df["close"].to_numpy(dtype="float64"),
                    "$volume": df["volume"].to_numpy(dtype="float64"),
                    "$factor": np.ones(len(df)),
                },
                index=pd.MultiIndex.from_arrays(
                    [pd.to_datetime(df["trade_date"]), instruments],
                    names=["datetime", "instrument"],
                ),
            )
            combined = combined.sort_index()

            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            combined.to_hdf(output_path, key="data", mode="w")
            logger.info(
                "[%s] Generated futures h5 from parquet: %s (%d rows, %d symbols)",
                self.market,
                output_path,
                len(combined),
                len(symbol_list),
            )
            return True

        except Exception as exc:
            logger.error(
                "[%s] Failed to generate futures h5 from parquet: %s", self.market, exc
            )
            return False

    @property
    def is_running(self) -> bool:
        return self._running

    def _extract_factors(self, log_dir: str) -> list[dict[str, Any]]:
        """从 RD-Agent 日志目录提取因子

        RDLoop 输出结构 (pickle):
        - experiment generation/**/*.pkl → 实验任务 (因子名 + 表达式)
        - coder result/**/*.pkl → 编码结果 (因子代码)
        - feedback/**/*.pkl → 反馈 (IC 等指标)
        """
        log_path = Path(log_dir)
        if not log_path.exists():
            logger.warning("[%s] Log dir not found: %s", self.market, log_dir)
            return []

        pkl_count = len(list(log_path.glob("**/*.pkl")))
        logger.info(
            "[%s] Scanning log dir: %s (%d pkl files)", self.market, log_dir, pkl_count
        )

        # 1. Factor metadata
        factor_meta: dict[str, dict] = {}
        for pkl_path in sorted(log_path.glob("**/experiment generation/**/*.pkl")):
            try:
                tasks = load_pickle(pkl_path)
                if not isinstance(tasks, list):
                    tasks = [tasks]
                for t in tasks:
                    name = getattr(t, "factor_name", None) or getattr(t, "name", None)
                    if not name:
                        continue
                    factor_meta[name] = {
                        "name": name,
                        "formulation": getattr(t, "factor_formulation", "") or "",
                        "description": getattr(t, "description", "") or "",
                        "category": getattr(t, "category", "") or "",
                    }
            except Exception as e:
                logger.debug("Failed to read %s: %s", pkl_path, e)

        # 2. Factor code —— 直接用 workspace.target_task.factor_name 映射，
        #    不再依赖函数名与因子名一致（LLM 函数命名可能与因子名不同）。
        factor_code: dict[str, str] = {}
        for pkl_path in sorted(log_path.glob("**/coder result/**/*.pkl")):
            try:
                workspaces = load_pickle(pkl_path)
                if not isinstance(workspaces, list):
                    workspaces = [workspaces]
                for ws in workspaces:
                    file_dict = getattr(ws, "file_dict", None) or {}
                    code = file_dict.get("factor.py", "")
                    if not code:
                        for v in file_dict.values():
                            if isinstance(v, str) and "def " in v:
                                code = v
                                break
                    if not code:
                        continue
                    name = ""
                    target = getattr(ws, "target_task", None)
                    if target is not None:
                        name = str(
                            getattr(target, "factor_name", None)
                            or getattr(target, "name", None)
                            or ""
                        ).strip()
                    if not name:
                        fn_match = re.search(r"def\s+(\w+)\s*\(", code)
                        name = (
                            fn_match.group(1).removeprefix("calculate_")
                            if fn_match
                            else f"factor_{len(factor_code)}"
                        )
                    factor_code[name] = code
            except Exception as e:
                logger.debug("Failed to read coder result %s: %s", pkl_path, e)

        # 3. Feedback
        feedback_text = ""
        for pkl_path in sorted(log_path.glob("**/feedback/**/*.pkl")):
            try:
                fb = load_pickle(pkl_path)
                if isinstance(fb, str):
                    feedback_text += fb + "\n"
                elif isinstance(fb, (list, tuple)):
                    for item in fb:
                        if isinstance(item, str):
                            feedback_text += item + "\n"
            except Exception as e:
                logger.debug("Failed to read feedback %s: %s", pkl_path, e)

        # 4. Merge —— 只保留**已完成 coding 阶段**的因子（有代码）。
        #    半成品（如 loop_n 预算截断时第二轮只有 experiment generation、无 coder result）
        #    不落库，避免因子库里出现无代码、无法回测的条目。
        factors: list[dict] = []
        for name, code in sorted(factor_code.items()):
            meta = factor_meta.get(name, {})
            factors.append(
                {
                    "name": meta.get("name", name),
                    "formulation": meta.get("formulation", ""),
                    "description": meta.get("description", ""),
                    "category": meta.get("category", ""),
                    "code": code,
                    "market": self.market,
                    "feedback": feedback_text[:5000] if feedback_text else "",
                }
            )

        missing = sorted(set(factor_meta.keys()) - set(factor_code.keys()))
        if missing:
            logger.info(
                "[%s] %d factors generated without code (partial/incomplete loop), skipped: %s",
                self.market,
                len(missing),
                missing,
            )

        logger.info("[%s] Extracted %d factors", self.market, len(factors))
        return factors


# ── Runner script entry point (subprocess) ──


def run_factor_mining_subprocess(
    market: str,
    task_id: str,
    user_id: str,
    loop_n: int,
    log_dir: str,
    direction: str = "",
) -> dict[str, Any]:
    """在子进程中执行因子挖掘（用于 launcher 调用）

    这是同步入口点，由 launcher 的 subprocess 调用。
    """
    wrapper = RDLoopWrapper(market=market)
    logger.info(
        "Starting factor mining: market=%s, task=%s, loops=%d", market, task_id, loop_n
    )
    result = asyncio.run(
        wrapper.run(
            loop_n=loop_n,
            task_log_dir=log_dir,
            direction=direction,
        )
    )
    result["task_id"] = task_id
    result["user_id"] = user_id
    return result
