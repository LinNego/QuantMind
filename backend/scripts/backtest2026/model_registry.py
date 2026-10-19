"""13 个模型 × T+5/T+10 的运行目录登记表（另有 T+20 对照子集）。

训练任务目录名带时间戳，且同一模型可能重训过（例如 T+5 MLP 的第一版失败）。
因此 26 个组合全部**显式钉扎 run_id**。钉扎避免报告生成时误取到后来的
重训版本；新增组合或重训时同步改这里。

T+20 只跑 `T20_PINNED_RUN_IDS` 里的对照子集；调用方通过
`resolve_registry(horizons=(5, 10, 20))` 显式纳入，缺省仍是 5/10 全矩阵。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .config import DATA_VOLUME

JOB_TEMPLATE = "T{horizon}_Cross300_{key}_Split20261018"
HORIZONS = (5, 10)

MODEL_ORDER = (
    "LGB",
    "XGB",
    "CAT",
    "RF",
    "LIN",
    "MLP",
    "TAB",
    "TFT",
    "GRU",
    "LSTM",
    "ALSTM",
    "TCN",
    "TRF",
)

MODEL_NAMES = {
    "LGB": "LightGBM",
    "XGB": "XGBoost",
    "CAT": "CatBoost",
    "RF": "随机森林",
    "LIN": "线性回归（岭）",
    "MLP": "多层感知机",
    "TAB": "TabNet",
    "TFT": "NativeTFT",
    "GRU": "GRU",
    "LSTM": "LSTM",
    "ALSTM": "注意力 LSTM",
    "TCN": "时间卷积网络",
    "TRF": "Transformer",
}

MODEL_FAMILIES = {
    "LGB": "树模型",
    "XGB": "树模型",
    "CAT": "树模型",
    "RF": "树模型",
    "LIN": "线性模型",
    "MLP": "浅层网络",
    "TAB": "表格深度",
    "TFT": "序列深度",
    "GRU": "序列深度",
    "LSTM": "序列深度",
    "ALSTM": "序列深度",
    "TCN": "序列深度",
    "TRF": "序列深度",
}

PINNED_RUN_IDS: dict[tuple[int, str], str | None] = {
    (5, "LGB"): "train_20261018034514_7e9b6d1d",
    (5, "XGB"): "train_20261018043550_39a52f2e",
    (5, "CAT"): "train_20261018050004_0acf54a8",
    (5, "TFT"): "train_20261018052719_46b9e366",
    (5, "RF"): "train_20261018065159_4ac19f54",
    (5, "GRU"): "train_20261018084119_7aa564bf",
    (5, "LSTM"): "train_20261018094907_faaf37b3",
    (5, "ALSTM"): "train_20261018105627_670b9741",
    (5, "TCN"): "train_20261018122058_52e44ede",
    (5, "TRF"): "train_20261018133146_b8100551",
    (5, "LIN"): "train_20261018150245_2a37a6a7",
    (5, "MLP"): "train_20261018152228_e51b12a4",
    (5, "TAB"): "train_20261018154336_e4817d21",
    (10, "LGB"): "train_20261018162000_182d5dcf",
    (10, "XGB"): "train_20261018171928_336c7ede",
    (10, "CAT"): "train_20261018174314_be41e1bf",
    (10, "TFT"): "train_20261018180839_ca3168a5",
    (10, "RF"): "train_20261018193330_2d8f9300",
    (10, "LIN"): "train_20261018191506_a23b4458",
    (10, "MLP"): "train_20261018205127_80201cd7",
    (10, "GRU"): "train_20261018211215_dc4aa420",
    (10, "LSTM"): "train_20261018221703_ee0dd9cb",
    (10, "ALSTM"): "train_20261018232148_93d1c51f",
    (10, "TAB"): "train_20261019002855_08734e9b",
    (10, "TCN"): "train_20261019010100_2c769493",
    (10, "TRF"): "train_20261019021411_fff3c7af",
}

#: T+20 只训练对照子集（树族三个代表 + 序列/随机森林对照），不参与 13 模型全矩阵。
#: 单独一张表，保持 PINNED_RUN_IDS 的「26 组合全覆盖」不变量不被稀释。
T20_PINNED_RUN_IDS: dict[tuple[int, str], str | None] = {
    (20, "LGB"): "train_20261019111759_ed5c3699",
    (20, "XGB"): "train_20261019115827_f5346da1",
    (20, "CAT"): "train_20261019122330_21225069",
    (20, "TFT"): "train_20261019124917_ec429052",
    (20, "RF"): "train_20261019135823_8cf74b3d",
}


def pins_for_horizons(horizons: tuple[int, ...]) -> dict[tuple[int, str], str | None]:
    merged = dict(PINNED_RUN_IDS)
    if 20 in horizons:
        merged.update(T20_PINNED_RUN_IDS)
    return merged


@dataclass(frozen=True)
class ModelRun:
    key: str
    name: str
    family: str
    horizon: int
    run_dir: Path | None
    job_name: str
    model_type: str
    ready: bool
    reason: str = ""

    @property
    def label(self) -> str:
        return f"{self.key} · T+{self.horizon}"


def _read_metadata(run_dir: Path) -> dict | None:
    path = run_dir / "metadata.json"
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _job_matches(metadata: dict, horizon: int, key: str) -> bool:
    job_name = str(metadata.get("job_name", ""))
    if not job_name:
        return False
    return job_name.startswith(f"T{horizon}_Cross300_{key}_")


def _ready_reason(run_dir: Path, horizon: int, key: str) -> tuple[bool, str, dict]:
    metadata = _read_metadata(run_dir)
    if metadata is None:
        return False, "metadata.json 未生成", {}
    if int(metadata.get("target_horizon_days", -1)) != horizon:
        return False, "target_horizon_days 不匹配", metadata
    if not _job_matches(metadata, horizon, key):
        return False, "job_name 不匹配", metadata
    if not (run_dir / "pred.parquet").is_file():
        return False, "pred.parquet 未生成", metadata
    return True, "", metadata


def _discover(root: Path, horizon: int, key: str) -> Path | None:
    candidates: list[Path] = []
    for run_dir in sorted(root.glob("train_*")):
        if not run_dir.is_dir():
            continue
        metadata = _read_metadata(run_dir)
        if metadata is None:
            continue
        if int(metadata.get("target_horizon_days", -1)) != horizon:
            continue
        if _job_matches(metadata, horizon, key):
            candidates.append(run_dir)
    if not candidates:
        return None
    # run_id 前缀是训练启动时间；重训时取最新一次。
    return max(candidates, key=lambda path: path.name)


def resolve_model_run(
    horizon: int,
    key: str,
    *,
    root: Path | None = None,
    use_pins: bool = True,
    pins: dict[tuple[int, str], str | None] | None = None,
) -> ModelRun:
    jobs_root = root or (DATA_VOLUME / "training_jobs")
    pin_map = PINNED_RUN_IDS if pins is None else pins
    pinned = pin_map.get((horizon, key)) if use_pins else None
    run_dir = jobs_root / pinned if pinned else _discover(jobs_root, horizon, key)
    job_name = JOB_TEMPLATE.format(horizon=horizon, key=key)
    if run_dir is None:
        return ModelRun(
            key=key,
            name=MODEL_NAMES[key],
            family=MODEL_FAMILIES[key],
            horizon=horizon,
            run_dir=None,
            job_name=job_name,
            model_type="",
            ready=False,
            reason="尚未找到训练任务",
        )
    ready, reason, metadata = _ready_reason(run_dir, horizon, key)
    return ModelRun(
        key=key,
        name=MODEL_NAMES[key],
        family=MODEL_FAMILIES[key],
        horizon=horizon,
        run_dir=run_dir,
        job_name=str(metadata.get("job_name", job_name)),
        model_type=str(metadata.get("model_type", "")),
        ready=ready,
        reason=reason,
    )


def resolve_registry(
    *,
    root: Path | None = None,
    require_all: bool = False,
    use_pins: bool = True,
    horizons: tuple[int, ...] = HORIZONS,
) -> dict[int, dict[str, ModelRun]]:
    pin_map = pins_for_horizons(horizons) if use_pins else {}
    registry = {
        horizon: {
            key: resolve_model_run(
                horizon,
                key,
                root=root,
                use_pins=use_pins,
                pins=pin_map,
            )
            for key in MODEL_ORDER
        }
        for horizon in horizons
    }
    if require_all:
        missing = [
            run.label
            for horizon in horizons
            for run in registry[horizon].values()
            if not run.ready
        ]
        if missing:
            raise RuntimeError(f"模型尚未就绪：{', '.join(missing)}")
    return registry


def inventory_rows(registry: dict[int, dict[str, ModelRun]]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for horizon in registry:
        for key in MODEL_ORDER:
            run = registry[horizon][key]
            rows.append(
                {
                    "horizon": horizon,
                    "key": key,
                    "name": run.name,
                    "family": run.family,
                    "run_id": run.run_dir.name if run.run_dir else "",
                    "job_name": run.job_name,
                    "model_type": run.model_type,
                    "ready": run.ready,
                    "reason": run.reason,
                }
            )
    return rows
