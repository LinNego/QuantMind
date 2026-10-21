"""因子报告：快照重建入口（build/status）与滞后探测（freshness）服务层单测。

用户现场（2026-10-09）：因子挖掘已把 22 个新因子毕业到 CN 根，报告页却永远停在
旧快照 —— 页面只有「刷新」（重读同一份快照），没有「重建快照」入口，也看不出
「盘上已比快照新」。本测试锁死新入口的行为契约：

- ``build_status``：快照存在性 / 构建中 / 元信息 / 日志最新一行；
- ``start_build``：按注册表 ``build_start`` 显式传 ``--start``（构建器默认
  ``--years 5`` 会把 2020-2021 静默砍掉）；构建中不重复启动；
- ``dataset_freshness``：盘上因子数 / 最新日期 > 快照 → stale + 人话原因；
  快照缺失时**不**叠「落后」噪声（available=False 已说明问题）。
"""

from __future__ import annotations

import json
from pathlib import Path

from backend.services.engine.factor_report import datasets as ds
from backend.services.engine.factor_report import service


# ─────────────────────────── build_status ───────────────────────────

def _write_snapshot(d: Path, meta: dict) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / "factor_report.json").write_text(
        json.dumps({"meta": meta, "factors": []}), encoding="utf-8"
    )


def test_build_status_reports_missing_snapshot(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "report_dir", lambda _ds, *_a: tmp_path / "report")

    st = service.build_status("pytest_missing")

    assert st["exists"] is False
    assert st["running"] is False
    assert st["built_at"] is None


def test_build_status_reads_snapshot_meta_and_log_step(monkeypatch, tmp_path):
    d = tmp_path / "report"
    _write_snapshot(d, {
        "generated_at": "2026-10-08T01:02:03",
        "n_factors": 80, "start": "20200102", "end": "20261008",
    })
    (d / "build.log").write_text(
        "2026-10-09 10:00:00,000 [INFO] [pytest] 因子 80 个；跳过非数值列 []\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(service, "report_dir", lambda _ds, *_a: d)

    st = service.build_status("pytest_done")

    assert st["exists"] is True
    assert st["built_at"] == "2026-10-08T01:02:03"
    assert st["n_factors"] == 80
    assert "因子 80 个" in st["step"]


# ─────────────────────────── start_build ───────────────────────────

def test_start_build_passes_registry_build_start(monkeypatch, tmp_path):
    calls: dict = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, **kw):
        calls["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(service, "report_dir", lambda _ds, *_a: tmp_path / "report")
    monkeypatch.setattr(service, "build_running", lambda _ds, *_a: None)
    monkeypatch.setattr(service.subprocess, "Popen", fake_popen)
    monkeypatch.setitem(ds.DATASETS, "pytest_build", {
        "label": "t", "dir_parts": ("pytest", "t"), "label_mode": "close_fwd_donor",
        "donor_parts": ("6_ml_datasets", "l1_factors"),
        "meta_cols": ("symbol",), "library_rule": "fixed:t", "universe": "t",
        "build_start": "20200102",
    })

    out = service.start_build("pytest_build")

    assert out["started"] is True and out["pid"] == 4242
    assert "--dataset" in calls["cmd"] and "pytest_build" in calls["cmd"]
    # build_start 必须在命令里 —— 默认 --years 5 会静默砍掉 2020-2021
    assert calls["cmd"][calls["cmd"].index("--start") + 1] == "20200102"
    assert (tmp_path / "report" / "build.pid").read_text().strip() == "4242"


def test_l1_factors_rebuild_keeps_snapshot_window():
    """l1_factors 的快照评估期自 20170103 起，注册表必须显式钉住起点。

    缺 ``build_start`` 时一键重建走构建器默认 ``--years 5``：盘上 L1 数据自
    2016 起，重建会把 2017–2021 静默砍掉，页面上的「评估期」从 2017 变成
    2021 且无任何报错。构建器的注释只点名了 rd_mined，这里补上 l1_factors。
    """
    assert ds.DATASETS["l1_factors"].get("build_start") == "20170103"


def test_start_build_short_circuits_when_already_running(monkeypatch, tmp_path):
    def no_popen(*a, **k):
        raise AssertionError("已在构建时不得再启动第二个子进程")

    monkeypatch.setattr(service, "report_dir", lambda _ds, *_a: tmp_path / "report")
    monkeypatch.setattr(service, "build_running", lambda _ds, *_a: 777)
    monkeypatch.setattr(service.subprocess, "Popen", no_popen)

    out = service.start_build("alpha_library")

    assert out == {"started": False, "running": True, "pid": 777, "dataset": "alpha_library"}


# ─────────────────────────── freshness ───────────────────────────

def test_freshness_flags_new_disk_factors(monkeypatch):
    """用户的原始现场：盘上 102 个、快照 80 个 → 必须自曝「新增 22」。"""
    monkeypatch.setattr(service, "disk_state",
                        lambda _ds, *_a: {"n_factors": 102, "first_dt": "20200102", "last_dt": "20261008"})
    monkeypatch.setattr(service, "load_snapshot",
                        lambda _ds, *_a: {"meta": {"n_factors": 80, "end": "20261008"}})

    fr = service.dataset_freshness("rd_mined")

    assert fr["stale"] is True
    assert "新增 22" in fr["stale_reason"]
    assert fr["disk_n_factors"] == 102 and fr["snapshot_n_factors"] == 80


def test_freshness_flags_new_dates(monkeypatch):
    monkeypatch.setattr(service, "disk_state",
                        lambda _ds, *_a: {"n_factors": 80, "first_dt": "20200102", "last_dt": "20261009"})
    monkeypatch.setattr(service, "load_snapshot",
                        lambda _ds, *_a: {"meta": {"n_factors": 80, "end": "20261008"}})

    fr = service.dataset_freshness("rd_mined")

    assert fr["stale"] is True
    assert "20261009" in fr["stale_reason"]


def test_freshness_is_quiet_when_snapshot_current(monkeypatch):
    monkeypatch.setattr(service, "disk_state",
                        lambda _ds, *_a: {"n_factors": 80, "first_dt": "20200102", "last_dt": "20261008"})
    monkeypatch.setattr(service, "load_snapshot",
                        lambda _ds, *_a: {"meta": {"n_factors": 80, "end": "20261008"}})

    fr = service.dataset_freshness("rd_mined")

    assert fr["stale"] is False
    assert fr["stale_reason"] is None


def test_freshness_is_quiet_when_snapshot_missing(monkeypatch):
    """快照都没有时 available=False 已足够 —— 不能再叠一层「落后」噪声。"""
    monkeypatch.setattr(service, "disk_state",
                        lambda _ds, *_a: {"n_factors": 80, "first_dt": "20200102", "last_dt": "20261008"})
    monkeypatch.setattr(service, "load_snapshot", lambda _ds, *_a: None)

    fr = service.dataset_freshness("rd_mined")

    assert fr["stale"] is False


# ─────────────────────────── disk_state（盘上计数口径） ───────────────────────────

def test_disk_state_counts_numeric_factor_columns_only(monkeypatch, tmp_path):
    """字符串血缘列（release_id 之类）不算因子 —— 与构建器的 dtype 过滤同口径。"""
    import numpy as np
    import pandas as pd
    import pyarrow as pa
    import pyarrow.parquet as pq

    root = tmp_path / "fac"
    for dt in ("20240102", "20240103"):
        d = root / f"dt={dt}"
        d.mkdir(parents=True)
        pq.write_table(pa.Table.from_pandas(pd.DataFrame({
            "symbol": ["S1", "S2"], "date": [dt, dt],
            "f_a": np.array([1.0, 2.0]), "f_b": np.array([3.0, 4.0]),
            "release_id": ["r1", "r2"],
        }), preserve_index=False), d / "data.parquet")
    monkeypatch.setitem(ds.DATASETS, "pytest_disk", {
        "label": "t", "dir_parts": ("pytest", "t"), "label_mode": "close_fwd",
        "meta_cols": ("symbol", "date", "time", "dt"),
        "library_rule": "fixed:t", "universe": "t",
    })
    monkeypatch.setattr(ds, "dataset_dir", lambda _ds, *_a: root)

    st = ds.disk_state("pytest_disk")

    assert st == {"n_factors": 2, "first_dt": "20240102", "last_dt": "20240103"}
