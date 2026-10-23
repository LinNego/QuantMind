"""行情字段字典与各源归一化。

**唯一出处**：桥对外所有"统一行情"字段都在 `FIELD_DEFS` 里定义——含义、单位、
以及"哪些源能提供"。新增源/字段只改这里；网页的字段字典也读它。

三个源（实际取值位置）：
- tdx    : 通达信客户端快照  get_market_snapshot  → Now/LastClose/Open/Max/Min/
           Volume/Amount/Buyp/Buyv/Sellp/Sellv/Inside/Outside
- aidata : 通达信付费快照    get_market_snapshot  → 字段同上（同源不同通道）
- ths    : 同花顺            /api/quote + /api/depth(10) + /api/quotes_ext
           quote: price/pre_close/open/high/low/volume
           depth: bids/asks [{level, price, volume}]（5 或 10 档）
           ext  : main_inflow(元) / dde_main(亿) / market_cap(元) / seal_amount(元)
                  / chg_pct / auction_chg_pct / auction_amount / amount / speed_4m
"""
from __future__ import annotations

from typing import Any

#: (字段, 中文, 单位, 说明, 可提供的源按优先级)
FIELD_DEFS: list[tuple[str, str, str, str, tuple[str, ...]]] = [
    ("last", "最新价", "元", "最后一笔成交价", ("ths", "aidata", "tdx")),
    ("prev_close", "昨收", "元", "前一交易日收盘价", ("ths", "aidata", "tdx")),
    ("open", "今开", "元", "当日第一笔成交价", ("ths", "aidata", "tdx")),
    ("high", "最高", "元", "当日最高价", ("ths", "aidata", "tdx")),
    ("low", "最低", "元", "当日最低价", ("ths", "aidata", "tdx")),
    ("volume", "成交量", "手（源而异）", "当日累计成交量；三源单位可能不同，跨源比较前先对齐",
     ("ths", "aidata", "tdx")),
    ("amount", "成交额", "元", "当日累计成交金额", ("ths", "aidata", "tdx")),
    ("change_pct", "涨跌幅", "%", "相对昨收；源内直取或本地计算", ("ths", "tdx")),
    ("bids", "买盘", "档位数组", "[{level,price,volume}]；TDX 5 档，THS 可 10 档",
     ("ths", "aidata", "tdx")),
    ("asks", "卖盘", "档位数组", "[{level,price,volume}]；TDX 5 档，THS 可 10 档",
     ("ths", "aidata", "tdx")),
    ("inside", "内盘", "手", "主动卖成交量（TDX 快照 Inside）", ("aidata", "tdx")),
    ("outside", "外盘", "手", "主动买成交量（TDX 快照 Outside）", ("aidata", "tdx")),
    ("main_inflow", "主力净流入", "元", "THS 0xc4 金额表 dt250", ("ths",)),
    ("dde_main", "DDE 主力", "亿", "THS 0xc4 金额表 dt248", ("ths",)),
    ("market_cap", "总市值", "元", "THS 0xc4 金额表 dt202", ("ths",)),
    ("seal_amount", "封单额", "元", "THS 金额表 dt44", ("ths",)),
    ("auction_chg_pct", "竞价涨幅", "%", "THS 统一列表派生", ("ths",)),
    ("auction_amount", "竞价金额", "元", "THS 统一列表派生", ("ths",)),
    ("speed_4m", "4分钟涨速", "%", "THS 统一列表派生", ("ths",)),
]

FIELD_ORDER = [f[0] for f in FIELD_DEFS]

SOURCE_DEFS: dict[str, dict] = {
    "tdx": {
        "label": "通达信（免费客户端）",
        "endpoint": "TQ 17709 get_market_snapshot（经桥 /api/v1/tdx/call）",
        "fields": ["last", "prev_close", "open", "high", "low", "volume",
                   "amount", "bids", "asks", "inside", "outside"],
        "cost": "0",
        "notes": "5 档；未订阅/未查看的标的可能滞后",
    },
    "aidata": {
        "label": "通达信（TdxAiData 付费）",
        "endpoint": "tdxaidata get_market_snapshot（桥 /api/v1/aidata/call）",
        "fields": ["last", "prev_close", "open", "high", "low", "volume",
                   "amount", "bids", "asks", "inside", "outside"],
        "cost": "按 200 条/词元",
        "notes": "与 tdx 同源不同通道；批量取更省",
    },
    "ths": {
        "label": "同花顺（thspypc 旁路）",
        "endpoint": "/api/quote + /api/depth(10) + /api/quotes_ext",
        "fields": ["last", "prev_close", "open", "high", "low", "volume",
                   "amount", "change_pct", "bids", "asks", "main_inflow",
                   "dde_main", "market_cap", "seal_amount", "auction_chg_pct",
                   "auction_amount", "speed_4m"],
        "cost": "0（需同花顺账号）",
        "notes": "10 档；资金面唯一来源；需扫码/账号登录",
    },
}


def _f(value: Any):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_tdx(raw: dict) -> dict:
    """TDX / TdxAiData 快照 → 统一字段。"""
    raw = raw or {}

    def levels(prices, volumes):
        out = []
        for i, (p, v) in enumerate(zip(prices or [], volumes or []), 1):
            price, volume = _f(p), _f(v)
            if price is None:
                continue
            out.append({"level": i, "price": price, "volume": volume})
        return out or None

    last = _f(raw.get("Now"))
    prev = _f(raw.get("LastClose"))
    change_pct = None
    if last is not None and prev:
        change_pct = round((last - prev) / prev * 100, 4)
    return {
        "last": last,
        "prev_close": prev,
        "open": _f(raw.get("Open")),
        "high": _f(raw.get("Max")),
        "low": _f(raw.get("Min")),
        "volume": _f(raw.get("Volume")),
        "amount": _f(raw.get("Amount")),
        "change_pct": change_pct,
        "bids": levels(raw.get("Buyp"), raw.get("Buyv")),
        "asks": levels(raw.get("Sellp"), raw.get("Sellv")),
        "inside": _f(raw.get("Inside")),
        "outside": _f(raw.get("Outside")),
    }


def normalize_ths(quote: dict | None, depth: dict | None,
                  ext: dict | None) -> dict:
    """THS（quote + depth + quotes_ext）→ 统一字段。缺失一律 None，不填 0。"""
    q = quote or {}
    d = depth or {}
    e = ext or {}

    def levels(items):
        out = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            price = _f(item.get("price"))
            if price is None:
                continue
            out.append({"level": item.get("level"),
                        "price": price,
                        "volume": _f(item.get("volume"))})
        return out or None

    return {
        "last": _f(q.get("price")),
        "prev_close": _f(q.get("pre_close")),
        "open": _f(q.get("open")),
        "high": _f(q.get("high")),
        "low": _f(q.get("low")),
        "volume": _f(q.get("volume")),
        "amount": _f(e.get("amount", q.get("amount"))),
        "change_pct": _f(e.get("chg_pct")),
        "bids": levels(d.get("bids")),
        "asks": levels(d.get("asks")),
        "main_inflow": _f(e.get("main_inflow")),
        "dde_main": _f(e.get("dde_main")),
        "market_cap": _f(e.get("market_cap")),
        "seal_amount": _f(e.get("seal_amount")),
        "auction_chg_pct": _f(e.get("auction_chg_pct")),
        "auction_amount": _f(e.get("auction_amount")),
        "speed_4m": _f(e.get("speed_4m")),
    }
