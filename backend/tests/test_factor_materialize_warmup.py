"""公式预热窗分析：嵌套窗口相加、窗口实参位置、真实清单回归。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.scripts.factor_materialize import warmup

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.unit
def test_nested_rolling_windows_add_up() -> None:
    # REF(504) 先消耗 504 日，外层 TSRANK(504) 再滚 504 日 → 1008
    formula = "0.5 * (R(TSRANK(c / REF(c, 504) - 1, 504)) + R(TSRANK(c, 21)))"
    assert warmup.required_days(formula) == 1008


@pytest.mark.unit
@pytest.mark.parametrize(
    ("formula", "expected"),
    [
        ("MEAN(x, 60)", 60),
        ("CORR(a, b, 20)", 20),  # 窗口在第 3 个实参
        ("COV(a, b, 5)", 5),
        ("QTL(x, 120, 0.1)", 120),  # 窗口在第 2 个实参，分位在第 3 个
        ("DELTA(REF(c, 30), 5)", 35),  # 滞后与窗口都消费历史
        ("SUM(MEAN(x, 10), 4)", 14),
        ("R(c) + Z(v) - IN(x)", 0),  # 当日截面算子不消费历史
        ("EWMA(x, 0.0952)", 0),  # alpha 不是天数
        ("MAX2(TSRANK(a, 3), TSRANK(b, 7))", 7),
    ],
)
def test_window_argument_positions(formula: str, expected: int) -> None:
    assert warmup.required_days(formula) == expected


@pytest.mark.unit
def test_regression_attr_counts_inner_window() -> None:
    formula = "REG(R(c), 250).resid"
    assert warmup.required_days(formula) == 250


@pytest.mark.unit
def test_windows_for_skips_unparsable_formula(caplog: pytest.LogCaptureFixture) -> None:
    records = [
        {"name": "good", "formula": "MEAN(c, 10)"},
        {"name": "bad", "formula": "MEAN(c, )"},
        {"name": "", "formula": "MEAN(c, 10)"},
    ]

    windows = warmup.windows_for(records)

    assert windows == {"good": 10}
    assert "bad" in caplog.text
    assert "不参与预热分析" in caplog.text


@pytest.mark.unit
def test_select_long_and_longest_requirement() -> None:
    windows = {"fast": 300, "edge": 907, "long": 1008, "longest": 1220}

    long_names = warmup.select_long(windows, 907)

    assert long_names == ["long", "longest"]
    assert warmup.longest_requirement(windows, long_names) == 1220
    assert warmup.longest_requirement(windows, []) == 0


@pytest.mark.unit
def test_real_formula_sets_cover_known_long_factors() -> None:
    # 2026-10-20 回填实测：这两条在新日期上整列空，根因是嵌套窗口复利
    defs_path = REPO / "data" / "factor_defs" / "formulas.json"
    if not defs_path.is_file():
        pytest.skip("factor_defs 清单不在本机")
    defs = [
        record
        for record in json.loads(defs_path.read_text(encoding="utf-8"))
        if record.get("feasibility") == "FULL"
    ]
    windows = warmup.windows_for(defs)
    assert windows["risk_fac_momentum"] == 1008
    assert warmup.select_long(windows, 907) == ["risk_fac_momentum"]

    batches = sorted(
        (REPO / "data" / "factor1300" / "batches").glob("batch_*.out.json")
    )
    if not batches:
        pytest.skip("factor1300 批次清单不在本机")
    records: list[dict] = []
    for path in batches:
        records.extend(json.loads(path.read_text(encoding="utf-8")))
    windows = warmup.windows_for(
        [record for record in records if record.get("formula")]
    )
    expected = {
        "f13_0261": 960,
        "f13_0390": 480,
        "f13_0422": 480,
        "f13_0423": 480,
        "f13_0645": 1220,
    }
    for name, days in expected.items():
        assert windows[name] == days
