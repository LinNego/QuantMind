"""模型下拉选项合并口径（`/stock-terminal/list` 的 `models` 字段）单元测试。

2026-10-20 用户复报「候选信号选模型显示不全」：下拉原先只列近 90 天**有信号**的
模型，注册表里尚未跑推理的新模型（例如 10-18/19 刚训练的 T+5/T+10/T+20 批量）
全部消失。这里锁定修复后的口径：

- 注册表（非归档）全量可选，信号事实只用于分组与「近 30 天无信号」标注；
- 有信号的模型保持原有排序（默认模型/最新信号优先），注册表新增项排在其后；
- 市场按页面收口（CN 收 CN + CUSTOM，HK 收 HK + CUSTOM），避免 A 股下拉塞满
  港美股模型。
"""

from datetime import date, datetime

from backend.services.api.stock_terminal_models import merge_model_options

RECENT_CUT = date(2026, 9, 20)


def sig(model_id: str, latest):
    return {"model_id": model_id, "latest": latest}


def reg(model_id: str, name: str = "", market: str = "CN", updated=None):
    return {
        "model_id": model_id,
        "display_name": name,
        "market": market,
        "updated_at": updated or datetime(2026, 10, 19, 12, 0),
    }


def test_signal_models_keep_order_and_recent_flag():
    """有信号的模型保持输入顺序，并用注册表里的显示名。"""
    out = merge_model_options(
        [sig("m_new", date(2026, 10, 20)), sig("m_old", date(2026, 9, 1))],
        [reg("m_new", "新模型"), reg("m_old", "旧模型")],
        recent_cut=RECENT_CUT,
        market_scope="CN",
    )
    assert [o["model_id"] for o in out] == ["m_new", "m_old"]
    assert out[0]["display_name"] == "新模型"
    assert out[0]["has_signals_30d"] is True
    assert out[1]["has_signals_30d"] is False
    assert all(o["has_signals_90d"] is True for o in out)


def test_registry_only_models_are_appended_and_selectable():
    """注册表里近 90 天没信号的模型必须出现在下拉中（修复前被整段丢掉）。"""
    out = merge_model_options(
        [sig("m_sig", date(2026, 10, 20))],
        [
            reg("m_sig", "有信号", updated=datetime(2026, 10, 20, 9, 0)),
            reg("m_new_t5", "T+5 新训练", updated=datetime(2026, 10, 19, 20, 0)),
            reg("m_new_t20", "T+20 新训练", updated=datetime(2026, 10, 19, 13, 0)),
        ],
        recent_cut=RECENT_CUT,
        market_scope="CN",
    )
    assert [o["model_id"] for o in out] == ["m_sig", "m_new_t5", "m_new_t20"]
    for o in out[1:]:
        assert o["has_signals_30d"] is False
        assert o["has_signals_90d"] is False
        assert o["display_name"]


def test_registry_market_scope_cn_and_hk():
    """CN 页面收 CN/CUSTOM，HK 页面收 HK/CUSTOM，互不串台。"""
    rows = [
        reg("cn", market="CN"),
        reg("hk", market="HK"),
        reg("us", market="US"),
        reg("custom", market="CUSTOM"),
        reg("unknown", market=""),
    ]
    cn = merge_model_options([], rows, recent_cut=RECENT_CUT, market_scope="CN")
    hk = merge_model_options([], rows, recent_cut=RECENT_CUT, market_scope="HK")
    assert [o["model_id"] for o in cn] == ["cn", "custom", "unknown"]
    assert [o["model_id"] for o in hk] == ["hk", "custom", "unknown"]


def test_signal_models_are_never_hidden_by_market_scope():
    """已有信号的外市场模型沿用旧行为（仍然可见），市场收口只作用于注册表新增项。"""
    out = merge_model_options(
        [sig("hk_sig", date(2026, 10, 15))],
        [reg("hk_sig", "港股模型", market="HK")],
        recent_cut=RECENT_CUT,
        market_scope="CN",
    )
    assert [o["model_id"] for o in out] == ["hk_sig"]
    assert out[0]["has_signals_90d"] is True


def test_no_signal_day_still_returns_registry_models():
    """连信号日都没有时，下拉也不能是空的——注册表就是完整选项集。"""
    out = merge_model_options(
        [],
        [reg("m1", "模型一"), reg("m2", "模型二")],
        recent_cut=None,
        market_scope="CN",
    )
    assert [o["model_id"] for o in out] == ["m1", "m2"]
    assert all(o["has_signals_30d"] is False for o in out)
    assert all(o["has_signals_90d"] is False for o in out)


def test_signal_model_missing_from_registry_keeps_model_id():
    """系统内置模型可能不在用户注册表里；保留选项，显示名留空由前端回退 model_id。"""
    out = merge_model_options(
        [sig("sys-model_qlib", date(2026, 8, 31))],
        [],
        recent_cut=RECENT_CUT,
        market_scope="CN",
    )
    assert out == [
        {
            "model_id": "sys-model_qlib",
            "display_name": "",
            "has_signals_30d": False,
            "has_signals_90d": True,
        }
    ]


def test_datetime_latest_is_compared_as_date():
    """asyncpg 可能回 datetime；与 date 比较前必须归一化，否则 TypeError。"""
    out = merge_model_options(
        [sig("m", datetime(2026, 10, 20, 15, 0))],
        [reg("m", "模型")],
        recent_cut=RECENT_CUT,
        market_scope="CN",
    )
    assert out[0]["has_signals_30d"] is True


def test_duplicate_registry_rows_keep_first():
    """同 model_id 的注册表重复行只保留第一条（SQL 已按 updated_at DESC）。"""
    out = merge_model_options(
        [],
        [reg("m", "最新名"), reg("m", "旧名")],
        recent_cut=RECENT_CUT,
        market_scope="CN",
    )
    assert len(out) == 1
    assert out[0]["display_name"] == "最新名"
