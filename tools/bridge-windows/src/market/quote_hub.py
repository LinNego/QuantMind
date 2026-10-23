"""多源行情动态平衡（tdx / aidata / ths）。

设计
----
- **并行**拉取所有可用源，每源独立超时——最慢的源不会拖住整次请求；
- **按字段合并**：`FIELD_DEFS` 定义每个字段的源优先级，逐字段取第一个非空值，
  并在 `source` 里记录它来自哪个源；
- **可观测**：每源延迟/成败/最近错误进滚动统计（`stats()`），响应里带
  `timings` / `fastest` / `missing`，一眼看出"哪个快、哪个没有"；
- **故障隔离**：任一源异常只影响它自己（该字段回落到下一个源）；
  全部失败才抛错，由路由映射成 502。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Awaitable, Callable

from .schema import FIELD_DEFS, normalize_ths, normalize_tdx  # noqa: F401

log = logging.getLogger(__name__)

#: 默认源优先级：THS 字段最全（10 档 + 资金面），其次免费 TDX。
#: **默认不含 aidata**：付费通道按次计费（200 条/词元），统一行情如果是
#: 2s 轮询会分钟级烧光日预算；需要时显式 `?prefer=ths,aidata,tdx`。
PREFERENCE = ("ths", "tdx")
SOURCE_TIMEOUT_S = 12.0


class QuoteHub:
    def __init__(self, fetchers: dict[str, Callable[[str], Awaitable[dict]]] | None = None,
                 timeout_s: float = SOURCE_TIMEOUT_S):
        self.fetchers = {k: v for k, v in (fetchers or {}).items() if v}
        self.timeout_s = float(timeout_s or SOURCE_TIMEOUT_S)
        self._stats = {
            k: {"calls": 0, "ok": 0, "err": 0, "last_ms": 0.0,
                "last_error": "", "lat": deque(maxlen=50)}
            for k in self.fetchers
        }

    def sources(self) -> list[str]:
        return list(self.fetchers)

    async def _one(self, name: str, fn, code: str):
        t0 = time.monotonic()
        st = self._stats[name]
        st["calls"] += 1
        try:
            data = await asyncio.wait_for(fn(code), timeout=self.timeout_s)
            ms = (time.monotonic() - t0) * 1000.0
            st["ok"] += 1
            st["last_ms"] = round(ms, 1)
            st["lat"].append(ms)
            return name, True, data or {}, ms, ""
        except Exception as exc:  # noqa: BLE001
            ms = (time.monotonic() - t0) * 1000.0
            st["err"] += 1
            st["last_error"] = f"{type(exc).__name__}: {exc}"[:200]
            return name, False, {}, ms, st["last_error"]

    async def quote(self, code: str, prefer: list[str] | None = None) -> dict:
        order = [s for s in (prefer or PREFERENCE) if s in self.fetchers]
        if not order:
            raise RuntimeError("没有可用的行情源")
        results = await asyncio.gather(
            *(self._one(n, self.fetchers[n], code) for n in order))
        by_name = {r[0]: r for r in results}
        if not any(by_name[n][1] for n in order):
            errs = "; ".join(f"{n}: {by_name[n][4]}" for n in order)
            raise RuntimeError(f"所有行情源均失败（{errs}）"[:300])

        data: dict = {}
        source_map: dict = {}
        missing: list[str] = []
        for field, _label, _unit, _desc, allowed in FIELD_DEFS:
            value = None
            for name in order:
                if name not in allowed:
                    continue
                ok, payload = by_name[name][1], by_name[name][2]
                if not ok:
                    continue
                candidate = payload.get(field)
                if candidate is not None:
                    value, source_map[field] = candidate, name
                    break
            if value is not None:
                data[field] = value
            else:
                missing.append(field)

        timings = {
            n: {"ok": by_name[n][1], "ms": round(by_name[n][3], 1),
                "error": by_name[n][4]}
            for n in order
        }
        fastest = min((n for n in order if by_name[n][1]),
                      key=lambda n: by_name[n][3], default=None)
        return {
            "code": code,
            "prefer": order,
            "data": data,
            "source": source_map,
            "missing": missing,
            "timings": timings,
            "fastest": fastest,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }

    def stats(self) -> dict:
        out = {}
        for name, st in self._stats.items():
            lat = sorted(st["lat"])
            p50 = round(lat[len(lat) // 2], 1) if lat else 0.0
            out[name] = {"calls": st["calls"], "ok": st["ok"], "err": st["err"],
                         "last_ms": st["last_ms"], "p50_ms": p50,
                         "last_error": st["last_error"]}
        return out
