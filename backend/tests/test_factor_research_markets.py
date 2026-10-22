"""因子研究工作台（factor_research）**市场分层**契约（2026-10-16）。

锁死四件事：
1. ``store.artifact_dir(dataset, market)``：目录按市场根解析（CN 走存量候选表，
   零行为变化）；``classic`` 为 A 股专属 → 非 CN 抛 ``MarketUnsupported``
   （路由译 422 的判据）；未知市场/未知数据集一律抛错，**绝不落 CN**；
2. 全部读取函数（factors_meta / stock_snapshot / screening …）市场透传 ——
   CN/HK 两根同时在场时各读各的，不允许跨市场串数据；
3. ``discovery``：非 CN 扫描剔除 10 个原始行情/估值列（RAW_MARKET_COLS，
   不剔会让「市值本身」进因子轴压掉真因子）；kept/l1l2 两种 A 股口径来源
   在非 CN **诚实抛错**；
4. ``screening`` 非 CN 诚实缺席（available:false + reason），CN 缺产物时
   结构形状逐字不变（前端已按它渲染「暂未筛选」）；
5. ``factor_detail`` 的 ``variants[].excess`` 按键跟随市场基准表 —— 缺
   ``bench_order`` 会按 CN 三码出键（全 None），前端按本市场基准码取数取空。
"""

from __future__ import annotations

from pathlib import Path

import json

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from backend.services.engine.factor_research import discovery, scorecard, service, store

# RAW_MARKET_COLS 唯一出处（报告链同款）——断言时逐字引用，防两处名单漂移
from backend.services.engine.factor_report.datasets import RAW_MARKET_COLS


# ─────────────────────────── 目录解析 ───────────────────────────


def _cn_root(tmp_path: Path) -> Path:
    """建一个「非空」的 CN 根（resolve_quantdb_dir 候选表要求非空）。"""
    root = tmp_path / "quantdb"
    (root / "2_base_sector").mkdir(parents=True)
    return root


def test_cn目录走存量候选表_零行为变化(tmp_path, monkeypatch):
    root = _cn_root(tmp_path)
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(root))

    assert store.artifact_dir("classic") == root / "factor_research"
    assert store.artifact_dir("private") == root / "factor_research_private"
    assert store.artifact_dir("private", "CN") == store.artifact_dir(
        "private"
    )  # 缺省即 CN


def test_data层市场路径_CN走存量候选表_非CN走市场根(tmp_path, monkeypatch):
    """``frdata.market_path`` 是脚本侧拼财务目录的唯一入口（2026-10-22 修断链）。"""
    from backend.services.engine.factor_research import data as frdata

    root = _cn_root(tmp_path)
    hk = tmp_path / "quanthk"
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(root))
    monkeypatch.setenv("QM_QUANTHK_DATA_DIR", str(hk))

    assert (
        frdata.market_path(frdata.INCOME_PARTS) == root / "3_financial_data" / "income"
    )
    assert (
        frdata.market_path(frdata.HOLDER_PARTS, "HK")
        == hk / "3_financial_data" / "holder_num"
    )


def test_build_factor_research不引用已删的重导出():
    """四市场分层删了 data 模块的 ``resolve_quantdb_subdir`` 重导出，构建脚本仍
    ``frdata.resolve_quantdb_subdir(...)`` 动态取属性——静态检查看不见，构建跑到
    财务因子才 AttributeError。钉死脚本源码不出现该名，且替代入口真实存在。"""
    from backend.services.engine.factor_research import data as frdata

    src = (
        Path(__file__).resolve().parents[1] / "scripts" / "build_factor_research.py"
    ).read_text(encoding="utf-8")
    assert "frdata.resolve_quantdb_subdir" not in src
    assert callable(frdata.market_path)


def test_hk目录走市场根(tmp_path, monkeypatch):
    hk = tmp_path / "quanthk"
    monkeypatch.setenv("QM_QUANTHK_DATA_DIR", str(hk))

    assert store.artifact_dir("private", "HK") == hk / "factor_research_private"
    assert store.artifact_dir("private", "hk") == store.artifact_dir(
        "private", "HK"
    )  # 大小写归一


def test_classic非cn抛_market_unsupported(tmp_path, monkeypatch):
    """classic 是 A 股专属 demo 复刻 —— 非 CN 必须显式抛错（路由译 422），
    而不是去拼一个不存在的目录、随后以「快照缺失」误指用户点一键构建。"""
    monkeypatch.setenv("QM_QUANTHK_DATA_DIR", str(tmp_path / "quanthk"))

    with pytest.raises(store.MarketUnsupported, match="A 股专属"):
        store.artifact_dir("classic", "HK")
    # 路由需要靠类型先判它再判 ValueError（子类是刻意的，便于既有 except ValueError 兜底）
    assert issubclass(store.MarketUnsupported, ValueError)


def test_未知市场绝不落_cn(tmp_path, monkeypatch):
    """CN 根已在场也不许兜底：查 HK 拼错成 XX 却静默读 A 股 = 数据污染。"""
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(_cn_root(tmp_path)))

    with pytest.raises(ValueError, match="未知市场"):
        store.artifact_dir("private", "XX")


def test_未知数据集仍旧抛错(tmp_path, monkeypatch):
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(_cn_root(tmp_path)))
    with pytest.raises(ValueError, match="未知因子数据集"):
        store.artifact_dir("../etc", "CN")


# ─────────────────────────── 读取函数市场透传 ───────────────────────────


def _write_factors_json(root: Path, dirname: str, marker: str) -> None:
    d = root / dirname
    d.mkdir(parents=True, exist_ok=True)
    (d / "factors.json").write_text(
        json.dumps({"factors": [{"code": marker, "name_cn": marker}], "meta": {}}),
        encoding="utf-8",
    )


def test_两市场同在场各读各的(tmp_path, monkeypatch):
    """CN 与 HK 根同时可用时，factors_meta 按 market 取到各自目录 —— 串市场
    在界面上不会报错，只会把 A 股的目录戴到港股页面。"""
    cn, hk = _cn_root(tmp_path), tmp_path / "quanthk"
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(cn))
    monkeypatch.setenv("QM_QUANTHK_DATA_DIR", str(hk))
    _write_factors_json(cn, "factor_research_private", "cn_factor")
    _write_factors_json(hk, "factor_research_private", "hk_factor")

    cn_meta = store.factors_meta("private", "CN")
    hk_meta = store.factors_meta("private", "HK")

    assert cn_meta["factors"][0]["code"] == "cn_factor"
    assert hk_meta["factors"][0]["code"] == "hk_factor"


def test_stock_snapshot非cn只读私人目录(tmp_path, monkeypatch):
    """HK 根里放一份 **classic 毒饵** —— 非 CN 只许读私人目录，
    返回毒饵即证明去碰了 classic 路径（A 股快照戴到港股头上）。"""
    hk = tmp_path / "quanthk"
    monkeypatch.setenv("QM_QUANTHK_DATA_DIR", str(hk))
    classic = hk / "factor_research"  # 即便盘上存在，也不许读
    classic.mkdir(parents=True)
    pq.write_table(
        pa.table({"symbol": ["POISON"], "industry": ["毒饵"], "total_mv_yi": [1.0]}),
        classic / "stock_snapshot.parquet",
    )
    private = hk / "factor_research_private"
    private.mkdir(parents=True)
    pq.write_table(
        pa.table(
            {"symbol": ["00700.HK"], "industry": ["资讯科技"], "total_mv_yi": [100.0]}
        ),
        private / "stock_snapshot.parquet",
    )

    df = store.stock_snapshot("HK")

    assert df is not None and df["symbol"].tolist() == ["00700.HK"]


def test_panel_stamp按市场分键(tmp_path, monkeypatch):
    """缓存键 = 路径 + mtime：两个市场的同名产物必须键不同（否则先构建的
    市场会被另一市场命中缓存，整页数据静默错市场）。"""
    cn, hk = _cn_root(tmp_path), tmp_path / "quanthk"
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(cn))
    monkeypatch.setenv("QM_QUANTHK_DATA_DIR", str(hk))
    (cn / "factor_research_private").mkdir(parents=True)
    (hk / "factor_research_private").mkdir(parents=True)
    (cn / "factor_research_private" / "factor_panel.parquet").write_bytes(b"cn")
    (hk / "factor_research_private" / "factor_panel.parquet").write_bytes(b"hk")

    s_cn = store.panel_stamp("private", "CN")
    s_hk = store.panel_stamp("private", "HK")

    assert s_cn != s_hk
    assert str(hk) in s_hk and str(cn) in s_cn


# ─────────────────────────── screening 诚实面 ───────────────────────────


def test_screening非cn诚实缺席(tmp_path, monkeypatch):
    monkeypatch.setenv("QM_QUANTHK_DATA_DIR", str(tmp_path / "quanthk"))

    out = store.screening("HK")

    assert out["available"] is False
    assert out["market"] == "HK"
    assert "A 股口径" in out["reason"] and "暂未接入" in out["reason"]
    assert out["counts"]["kept"] == 0 and out["kept"] == []


def test_screening_cn缺产物时结构形状不变(tmp_path, monkeypatch):
    """CN 存量行为：文件缺失 → 空结构（**不带** available 键，前端按它渲染
    「暂未筛选」）；不要顺手加 available:true —— 那是把「没建」谎报成「已建」。"""
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(_cn_root(tmp_path)))

    out = store.screening("CN")

    assert "available" not in out
    assert out["counts"]["candidates"] == 0 and out["generated_at"] is None


# ─────────────────────────── discovery 市场化 ───────────────────────────


def _write_ds(root: Path, name: str, cols: list[str], day: str = "20240102") -> None:
    d = root / name / f"dt={day}"
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table({"symbol": ["0001.HK", "0700.HK"], **{c: [1.0, 2.0] for c in cols}}),
        d / "data.parquet",
    )


def _latest(root: Path, name: str) -> Path:
    return sorted((root / name).glob("dt=*/data.parquet"))[-1]


def test_非cn扫描剔除原始行情估值列(tmp_path):
    """HK 表首的 pb/total_mv 是编码输入不是因子 —— 不剔会让「市值本身」霸榜。"""
    root = tmp_path / "quanthk" / "6_ml_datasets"
    _write_ds(
        root, "l1_factors", ["a1", "a2", "a3", "a4", "a5", "a6", "pb", "total_mv"]
    )

    names = discovery.numeric_factor_names(_latest(root, "l1_factors"), "HK")

    assert names == ["a1", "a2", "a3", "a4", "a5", "a6"]
    for c in ("pb", "total_mv", "symbol"):
        assert c not in names


def test_cn扫描不叠加剔除_零行为变化(tmp_path):
    """CN 库不含这 10 列（119 列表头实测）；若 CN 某库真有自己的 ``pb``，
    那是该库的因子而不是「原始列」，不得被市场化改动误伤。"""
    root = tmp_path / "quantdb" / "6_ml_datasets"
    _write_ds(root, "l1_factors", ["a1", "a2", "a3", "a4", "pb"])

    names = discovery.numeric_factor_names(_latest(root, "l1_factors"), "CN")

    assert "pb" in names and "symbol" not in names


def test_scan_libraries_按市场根扫描(tmp_path, monkeypatch):
    """qroot 缺省时必须按 market 解析根 —— 两个根的库名不同，取错根即串市场。"""
    cn, hk = tmp_path / "quantdb", tmp_path / "quanthk"
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(cn))
    monkeypatch.setenv("QM_QUANTHK_DATA_DIR", str(hk))
    _write_ds(
        cn / "6_ml_datasets", "alpha_library", ["cn1", "cn2", "cn3", "cn4", "cn5"]
    )
    _write_ds(hk / "6_ml_datasets", "l1_factors", ["hk1", "hk2", "hk3", "hk4", "hk5"])

    hk_libs = discovery.scan_libraries(market="HK")
    cn_libs = discovery.scan_libraries(market="CN")

    assert set(hk_libs) == {"l1_factors"} and hk_libs["l1_factors"][0] == "hk1"
    assert set(cn_libs) == {"alpha_library"} and cn_libs["alpha_library"][0] == "cn1"


def test_kept与l1l2来源非cn诚实抛错(tmp_path):
    """两种来源都是 A 股口径产物（七库筛选 / L2 逐笔）—— 非 CN 抛错并指路
    auto；静默返回空集会被下游读成「盘上没有因子」。"""
    root = tmp_path / "quanthk"

    with pytest.raises(FileNotFoundError, match="A 股口径"):
        discovery.load_kept("HK")
    with pytest.raises(FileNotFoundError, match="A 股口径"):
        discovery.load_l1l2(root, "HK")
    with pytest.raises(FileNotFoundError, match="A 股口径"):
        discovery.load_sources("kept", root, "HK")


def test_kept_cn缺清单报错文案不变(tmp_path, monkeypatch):
    """CN 行为逐字保留：清单缺失 → 提示先跑 screen_factors.py。"""
    cn = _cn_root(tmp_path)
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(cn))

    with pytest.raises(FileNotFoundError, match="先跑 screen_factors.py"):
        discovery.load_kept("CN")


def test_港股库标签已登记(tmp_path):
    """HK 的 ccass/south 库有中文标签（未登记会回退目录名，不报错但丢信息）。"""
    assert discovery.lib_label("ccass_factors") != "ccass_factors"
    assert discovery.lib_label("south_factors") != "south_factors"


def test_剔除名单唯一出处为报告链常量():
    """discovery 的剔除集引用报告链 RAW_MARKET_COLS（唯一出处），不是本地副本 ——
    本用例在二者脱钩（如被改成硬编码列表）时立刻变红。"""
    root = Path(discovery.__file__).resolve().parent / "discovery.py"
    assert "RAW_MARKET_COLS" in root.read_text(encoding="utf-8")
    assert len(RAW_MARKET_COLS) == 10


# ──────────────── 单因子详情：excess 按键跟随市场基准表 ────────────────


def _detail_panel() -> scorecard.Panel:
    """两月两票的单因子面板（HK 代码空间）。"""
    months = ["2026-08-31", "2026-09-30"]
    syms = ["00700.HK", "00005.HK"]
    rows = [
        {
            "factor_code": "F1",
            "trade_date": pd.Timestamp(m),
            "rank": rk,
            "symbol": sym,
            "score": float(10 - rk - mi),
            "raw": float(rk),
            "fwd_ret": 0.01 * rk,
        }
        for mi, m in enumerate(months)
        for rk, sym in enumerate(syms, 1)
    ]
    return scorecard.Panel(pd.DataFrame(rows))


@pytest.fixture()
def detail_store(monkeypatch):
    """内存 store：面板/元数据注入、基准表置空（本组用例只看键位，不看数值）。"""
    service._ctx_cache.clear()
    service._derived_cache.clear()
    p = _detail_panel()
    monkeypatch.setattr(service.store, "panel", lambda *a, **k: p)
    monkeypatch.setattr(service.store, "panel_stamp", lambda *a, **k: "snap-mk")
    monkeypatch.setattr(service.store, "benchmark_table", lambda *a, **k: None)
    monkeypatch.setattr(service.store, "ic_table", lambda *a, **k: None)
    monkeypatch.setattr(
        service.store,
        "factors_meta",
        lambda *a, **k: {
            "factors": [
                {
                    "code": "F1",
                    "name_cn": "因子F1",
                    "display_name": "",
                    "l1": "L1",
                    "l2": "L2",
                    "direction": 1,
                    "description": "",
                    "formula": "",
                    "wind_source": "",
                    "available": True,
                }
            ]
        },
    )
    monkeypatch.setattr(service.store, "stock_snapshot", lambda *a, **k: None)
    yield
    service._ctx_cache.clear()
    service._derived_cache.clear()


def test_详情超额按键跟随本市场基准(detail_store):
    """非 CN：variants[].excess 必须按**本市场**基准表出键（首位=主基准）。

    缺 ``ctx["bench_order"]`` 时缺省是 CN 三码：港股详情里 excess 变成
    ``{000300.SH: None, 000906.SH: None, 000905.SH: None}`` —— 前端按本市场
    基准码（HSI.HK）取数永远取空，「超额」整列静默无数据，全链零报错。
    """
    d = service.factor_detail("F1", ns=[1], dataset="private", market="HK")

    assert d is not None
    keys = list(d["variants"][0]["excess"].keys())
    assert keys == list(scorecard.bench_order_for_market("HK"))
    assert keys[0] == "HSI.HK"  # 首位 = 主基准
    assert "000300.SH" not in keys  # 绝不落 CN 键


def test_详情期货超额为空表(detail_store):
    """期货无指数基准（bench_order 空）→ excess 空表，而不是 CN 三码全 None。"""
    d = service.factor_detail("F1", ns=[1], dataset="private", market="FUTURES")

    assert d is not None
    assert d["variants"][0]["excess"] == {}


def test_详情cn超额键仍是存量三码(detail_store):
    """CN 零行为变化：键仍是工作台存量三码（000300/000906/000905，逐字锁定）。"""
    d = service.factor_detail("F1", ns=[1], dataset="private", market="CN")

    assert d is not None
    assert list(d["variants"][0]["excess"].keys()) == [
        "000300.SH",
        "000906.SH",
        "000905.SH",
    ]
