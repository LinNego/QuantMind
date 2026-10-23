"""多源行情聚合（QuoteHub）单测：合并 / 回落 / 源归属 / 缺失 / 统计。

运行: cd bridge-windows && python3 tests/test_market_hub.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.market.quote_hub import QuoteHub  # noqa: E402
from src.market.schema import normalize_tdx, normalize_ths  # noqa: E402


async def _run():
    async def tdx(code):
        return {"last": 10.0, "prev_close": 9.8, "volume": 100.0,
                "bids": [{"level": 1, "price": 9.99, "volume": 5}],
                "asks": [{"level": 1, "price": 10.01, "volume": 6}],
                "inside": 40.0, "outside": 60.0}

    async def aidata(code):
        raise RuntimeError("付费 Key 未配置")

    async def ths(code):
        return {"last": 10.5, "change_pct": 1.2, "main_inflow": 123.0,
                "bids": [{"level": 1, "price": 10.49, "volume": 7}]}

    hub = QuoteHub({"tdx": tdx, "aidata": aidata, "ths": ths})
    r = await hub.quote("600519.SH", prefer=["ths", "aidata", "tdx"])

    # 优先级：ths 的 last 覆盖 tdx；tdx 独有的 volume/inside 回落生效
    assert r["data"]["last"] == 10.5 and r["source"]["last"] == "ths", r
    assert r["data"]["volume"] == 100.0 and r["source"]["volume"] == "tdx", r
    assert r["data"]["main_inflow"] == 123.0 and r["source"]["main_inflow"] == "ths"
    assert r["timings"]["aidata"]["ok"] is False
    assert "dde_main" in r["missing"] and "seal_amount" in r["missing"]
    assert r["fastest"] in ("tdx", "ths")

    # prefer 覆盖优先级
    r2 = await hub.quote("600519.SH", prefer=["tdx"])
    assert r2["data"]["last"] == 10.0 and r2["source"]["last"] == "tdx", r2
    assert "main_inflow" in r2["missing"]

    # 全部失败 → 抛错
    hub2 = QuoteHub({"tdx": aidata})
    try:
        await hub2.quote("600519.SH")
    except RuntimeError:
        pass
    else:
        raise AssertionError("全部源失败时应抛 RuntimeError")

    # 统计：失败计数与最近错误
    st = hub.stats()
    assert st["aidata"]["err"] >= 1 and st["aidata"]["last_error"], st
    assert st["tdx"]["ok"] >= 2

    # 单位归一：TDX Amount 原始万元 → 统一字段为元
    norm = normalize_tdx({"Now": "100", "LastClose": "99", "Amount": "12.34",
                          "Volume": "5"})
    assert norm["amount"] == 123400.0, norm

    # 盘口单位归一：THS depth 的 qty 是「股」，统一字段用「手」（÷100）
    nth = normalize_ths({}, {"buy": [{"level": "买一", "price": 10.0,
                                      "qty": 1900}]}, {})
    assert nth["bids"][0]["volume"] == 19.0, nth
    print("✅ 多源聚合：字段合并 / 源归属 / 回落 / 缺失 / prefer / 统计 / 单位归一 全部通过")


if __name__ == "__main__":
    asyncio.run(_run())
