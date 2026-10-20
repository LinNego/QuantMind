#!/usr/bin/env python3
"""QuantDB 证券主表 → 代码 → 板块名（宿主机脚本共用）。

数据源：``data/quantdb/2_base_sector/instrument_detail/instrument_detail.parquet``
（QuantDB 主表）。板块列口径：

* ``rs_hyname`` —— 申万/聚源二级行业中文名（如 ``全国性银行``、``房地产开发``），
  有值优先；
* ``tdx_dyname`` —— 通达信行业名，主表缺 ``rs_hyname`` 时兜底。

只做一次全表读（duckdb 惰性 import，宿主机无 duckdb 时返回空表——板块是装饰，
不得拖垮产池/复盘）。夜间研究产池与收盘复盘卡片共用本函数，避免两处各写一份
路径与列名。
"""

from __future__ import annotations

import glob
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 容器内（``/data`` 卷）与宿主机（仓库 ``data/``）两种布局。
_INSTRUMENT_DETAIL = (
    ROOT / "data/quantdb/2_base_sector/instrument_detail/instrument_detail.parquet",
    Path("/data/quantdb/2_base_sector/instrument_detail/instrument_detail.parquet"),
)


def _first_existing() -> Path | None:
    for path in _INSTRUMENT_DETAIL:
        if path.is_file():
            return path
        hits = glob.glob(str(path))
        if hits:
            return Path(hits[0])
    return None


def load_industry_map() -> dict[str, str]:
    """``{后缀码: 板块名}``；主表不可读返回 ``{}``（调用方显示 ``—``，不编造）。"""
    path = _first_existing()
    if path is None:
        return {}
    try:
        import duckdb
    except ImportError:
        return {}
    con = None
    try:
        con = duckdb.connect()
        rows = con.execute(
            "SELECT Symbol, rs_hyname, tdx_dyname FROM read_parquet(?)",
            [str(path)],
        ).fetchall()
    except Exception:  # noqa: BLE001 主表列缺失/文件损坏都降级为空表
        return {}
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:  # noqa: BLE001
                pass
    out: dict[str, str] = {}
    for symbol, rs_name, tdx_name in rows:
        code = str(symbol or "").strip()
        if not code:
            continue
        industry = str(rs_name or "").strip() or str(tdx_name or "").strip()
        if industry:
            out[code] = industry
    return out


__all__ = ["load_industry_map"]
