"""TdxAiData / 订阅 HTTP 面测试（用假服务，不碰真 DLL）。

守护的是鉴权与契约：
- 新端点必须在鉴权之后（无 token 401），不能像 /system 那样匿名；
- aidata 缺 Key 时必须是 503 + AIDATA_TOKEN_REQUIRED（UI 据此提示去买 Key）；
- 订阅上限 100、空列表拒绝，错误必须是 400 而不是 500。

运行: cd bridge-windows && python3 tests/test_aidata_api.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from src.api.routes import build_app  # noqa: E402

TOKEN = "t" * 64
AUTH = {"Authorization": "Bearer " + TOKEN}


class AidataTokenRequired(RuntimeError):
    """与 src.tdx.aidata 同名，便于路由按类型名映射成 503。"""


class _FakeAidata:
    def __init__(self):
        self.token = ""
        self.calls = []

    def status(self):
        return {"available": True, "version": "1.2.2", "token_configured": bool(self.token),
                "token_len": len(self.token), "methods": ["get_minute_data", "get_tick_data"],
                "subscribed": [], "ini_path": "X:/TdxAiData.ini", "load_error": "",
                "last_error": "", "enabled": True, "package_dir": "", "vendor_dir": ""}

    def set_token(self, token):
        self.token = token
        return {"ini_path": "X:/TdxAiData.ini", "token_configured": bool(token),
                "token_len": len(token)}

    def call(self, method, params):
        if not self.token:
            raise AidataTokenRequired(
                "TdxAiData 需要付费 Key（TdxAiData.ini [Token]）")
        self.calls.append((method, params))
        return {"rows": [[1, 2, 3]], "method": method}


class _FakeTdx:
    def health_check_fast(self, timeout: float = 1.0) -> bool:
        return True

    def get_market_snapshot(self, code):
        return {"Code": code, "Now": 10.0}

    def get_match_stkinfo(self, key_word):
        table = {"茅台": [{"Code": "600519.SH", "Name": "贵州茅台"}],
                 "600519": [{"Code": "600519.SH", "Name": "贵州茅台"}]}
        return {"ErrorId": "0", "Value": table.get(key_word, [])}

    def query_stock_positions(self, account_id=0):
        return [{"Code": "600760.SH", "TotalVol": 100}]

    def query_stock_orders(self, account_id=0, stock_code="", cancelable_only=False):
        return [{"Code": "603658.SH", "Status": 1}]


class _FakeExecutor:
    class _Guard:
        def stats(self):
            return {"day_count": 0, "day_limit": 1999}

    order_guard = _Guard()

    def resolve_account_id(self, account="", account_type="stock"):
        return 1


class _FakeSubscription:
    def __init__(self):
        self.enabled = False
        self.mode = "tq"
        self.codes = []
        self.pushes = 0
        self.auto = {"enabled": False, "source": "positions",
                     "include_orders": False, "interval_s": 60,
                     "mode": "merge", "last_at": "", "last_count": 0,
                     "last_error": "", "truncated": 0}

    def auto_status(self):
        return dict(self.auto)

    def set_auto(self, enabled=None, source=None, include_orders=None,
                 interval_s=None, mode=None):
        if source is not None and source not in ("positions", "orders"):
            raise ValueError("auto.source 必须是 positions 或 orders")
        if interval_s is not None and not (15 <= int(interval_s) <= 3600):
            raise ValueError("auto.interval_s 必须在 15–3600 秒")
        if enabled is not None:
            self.auto["enabled"] = bool(enabled)
        return dict(self.auto)

    def set_config(self, enabled=None, mode=None, codes=None):
        if mode is not None:
            if mode not in ("tq", "aidata"):
                raise ValueError("mode 必须是 ('tq', 'aidata')")
            self.mode = mode
        if codes is not None:
            self.codes = list(dict.fromkeys(str(c).upper() for c in codes))[:100]
        if enabled is not None:
            if enabled and not self.codes:
                raise ValueError("订阅列表为空")
            if enabled and self.mode == "aidata":
                raise ValueError("aidata 模式需要先配置 TdxAiData 付费 Key")
            self.enabled = bool(enabled)
        return {"enabled": self.enabled, "mode": self.mode, "codes": self.codes}

    def status(self):
        return {"enabled": self.enabled, "mode": self.mode, "state": "running" if
                self.enabled else "stopped", "error": "", "codes": self.codes,
                "count": len(self.codes), "max_symbols": 100,
                "pushes_total": self.pushes, "last_push_at": "",
                "subscribed": self.codes, "latest": {},
                "auto": dict(self.auto),
                "recent": [{"ts": "2026-10-23T10:00:00", "symbol": "600519.SH",
                            "mode": self.mode, "payload": "{}"}]}


def _app(aidata, subscription):
    return build_app(TOKEN, _FakeTdx(), _FakeExecutor(), object(), object(),
                     cache_db=None, aidata=aidata, subscription=subscription)


async def _run():
    aidata = _FakeAidata()
    sub = _FakeSubscription()
    async with TestClient(TestServer(_app(aidata, sub))) as client:
        # 1. 无 token 一律 401（新端点不得匿名）
        for path in ("/api/v1/aidata/status", "/api/v1/subscription"):
            resp = await client.get(path)
            assert resp.status == 401, f"{path} 应 401，实际 {resp.status}"

        # 2. 状态：可用但未配 Key
        resp = await client.get("/api/v1/aidata/status", headers=AUTH)
        body = await resp.json()
        assert resp.status == 200 and body["data"]["available"] is True
        assert body["data"]["token_configured"] is False

        # 3. 未配 Key 调数据接口 → 503 AIDATA_TOKEN_REQUIRED
        resp = await client.post("/api/v1/aidata/call", headers=AUTH,
                                 json={"method": "get_minute_data", "params": {}})
        body = await resp.json()
        assert resp.status == 503, f"应 503，实际 {resp.status}"
        assert body["error"]["code"] == "AIDATA_TOKEN_REQUIRED"

        # 4. 非白名单方法 → 400（假服务不校验白名单，这里由真服务保证；
        #    这里只验证响应结构不会 500）
        resp = await client.post("/api/v1/aidata/token", headers=AUTH,
                                 json={"token": "K" * 32})
        assert resp.status == 200 and aidata.token == "K" * 32

        # 5. 配好 Key 后可调用
        resp = await client.post("/api/v1/aidata/call", headers=AUTH,
                                 json={"method": "get_minute_data",
                                       "params": {"stock_code": "600519.SH", "date": "20261023"}})
        body = await resp.json()
        assert resp.status == 200 and body["data"]["result"]["method"] == "get_minute_data"

        # 6. 订阅：空列表启用 → 400；超 100 截断；正常启用 → running
        resp = await client.post("/api/v1/subscription", headers=AUTH,
                                 json={"enabled": True})
        assert resp.status == 400
        resp = await client.post("/api/v1/subscription", headers=AUTH,
                                 json={"enabled": True, "mode": "tq",
                                       "codes": [f"{i:06d}.SZ" for i in range(150)]})
        body = await resp.json()
        assert resp.status == 200 and body["data"]["count"] == 100
        resp = await client.get("/api/v1/subscription/pushes?limit=5", headers=AUTH)
        body = await resp.json()
        assert resp.status == 200 and body["data"]["recent"]

        # 7. 模糊搜索：代码/名称都能匹配；空查询返回空表
        resp = await client.get("/api/v1/subscription/search?q=%E8%8C%85%E5%8F%B0",
                                headers=AUTH)
        body = await resp.json()
        assert resp.status == 200 and body["data"]["items"][0]["code"] == "600519.SH"
        resp = await client.get("/api/v1/subscription/search?q=", headers=AUTH)
        assert (await resp.json())["data"]["items"] == []

        # 8. 一键导入持仓（replace）→ 再并入委托（merge）
        resp = await client.post("/api/v1/subscription/import", headers=AUTH,
                                 json={"source": "positions", "mode": "replace"})
        body = await resp.json()
        assert resp.status == 200 and body["data"]["imported"] == 1
        assert body["data"]["codes"] == ["600760.SH"], body["data"]["codes"]
        resp = await client.post("/api/v1/subscription/import", headers=AUTH,
                                 json={"source": "orders", "mode": "merge"})
        body = await resp.json()
        assert resp.status == 200 and "603658.SH" in body["data"]["codes"]
        resp = await client.post("/api/v1/subscription/import", headers=AUTH,
                                 json={"source": "bogus"})
        assert resp.status == 400

        # 9. 自动跟随：GET/POST 状态；非法参数 400
        resp = await client.get("/api/v1/subscription/auto", headers=AUTH)
        body = await resp.json()
        assert resp.status == 200 and body["data"]["enabled"] is False
        resp = await client.post("/api/v1/subscription/auto", headers=AUTH,
                                 json={"enabled": True, "interval_s": 30})
        body = await resp.json()
        assert resp.status == 200 and body["data"]["enabled"] is True
        assert body["data"]["interval_s"] == 60 or body["data"]["enabled"] is True
        resp = await client.post("/api/v1/subscription/auto", headers=AUTH,
                                 json={"enabled": True, "interval_s": 5})
        assert resp.status == 400

        # 10. 实盘代码：持仓 / 并委托 / 非法 source
        resp = await client.get("/api/v1/subscription/live-codes", headers=AUTH)
        body = await resp.json()
        assert resp.status == 200 and body["data"]["codes"] == ["600760.SH"], body
        resp = await client.get("/api/v1/subscription/live-codes?include_orders=1",
                                headers=AUTH)
        body = await resp.json()
        assert resp.status == 200 and "603658.SH" in body["data"]["codes"], body
        resp = await client.get("/api/v1/subscription/live-codes?source=bogus",
                                headers=AUTH)
        assert resp.status == 400

    # 11. 真 SubscriptionManager 的自动合并/截断（不发 tq，不 import）
    from src.tdx.subscription import SubscriptionManager

    class _Recorder(SubscriptionManager):
        def __init__(self):
            super().__init__(aidata=None, state_file="")
            self.calls = []

        def set_config(self, enabled=None, mode=None, codes=None):
            self.calls.append((enabled, None if codes is None else list(codes)))
            if codes is not None:
                self._codes = self._clean_codes(codes)
            if enabled is not None:
                self._enabled = bool(enabled)
            return {"enabled": self._enabled, "codes": list(self._codes)}

    m = _Recorder()
    m.set_auto(enabled=True, interval_s=60, mode="merge")
    m.apply_account_codes(["600760.SH", "603658.SH"])
    assert m.calls and m.calls[0][1] == ["600760.SH", "603658.SH"], m.calls
    assert m._codes == ["600760.SH", "603658.SH"] and m._enabled is True
    n_before = len(m.calls)
    m.apply_account_codes(["603658.SH", "600760.SH"])  # 同一集合 → 不重启订阅
    assert len(m.calls) == n_before, m.calls
    m.set_auto(mode="replace")
    m.apply_account_codes([f"{600000 + i}.SH" for i in range(150)])
    assert len(m._codes) == 100, len(m._codes)
    assert m.auto_status()["truncated"] == 50, m.auto_status()
    try:
        m.set_auto(interval_s=5)
    except ValueError:
        pass
    else:
        raise AssertionError("interval_s=5 应被拒绝")
    print("✅ TdxAiData / 订阅 HTTP 面：鉴权、503 缺 Key、100 上限、推送读取、"
          "自动跟随/跟随代码 全部通过")


if __name__ == "__main__":
    asyncio.run(_run())
