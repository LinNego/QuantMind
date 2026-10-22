"""采集器只读 HTTP 面：/health 开放，其余需 Bearer token。"""
from __future__ import annotations

import hmac
import logging

from aiohttp import web

log = logging.getLogger(__name__)


def _ok(data) -> web.Response:
    return web.json_response({"success": True, "data": data})


def _err(status: int, code: str, message: str) -> web.Response:
    return web.json_response(
        {"success": False, "error": {"code": code, "message": message}},
        status=status)


def _parse_codes(raw: str) -> list[str]:
    return [x.strip().upper() for x in (raw or "").split(",") if x.strip()]


async def _guarded(handler, request):
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - 读侧不能把进程打挂
        log.warning("HTTP 处理失败 %s: %s", request.path, exc, exc_info=True)
        return _err(500, "INTERNAL", "采集器内部错误，详见日志")


def build_app(service) -> web.Application:
    app = web.Application(client_max_size=64 * 1024)

    @web.middleware
    async def auth_middleware(request: web.Request, handler):
        if request.path == "/health":
            return await _guarded(handler, request)
        auth = request.headers.get("Authorization", "")
        provided = auth.removeprefix("Bearer ").strip() if auth else ""
        expected = service.config.token
        if not provided or not hmac.compare_digest(provided, expected):
            return _err(401, "AUTH_FAILED", "无效或缺失 token")
        return await _guarded(handler, request)

    app.middlewares.append(auth_middleware)

    async def health(request: web.Request):
        return _ok(await service.health_public())

    async def stats(request: web.Request):
        return _ok(await service.health())

    async def snapshot(request: web.Request):
        codes = _parse_codes(request.query.get("codes", ""))
        include_all = request.query.get("all", "").lower() in ("1", "true", "yes")
        hot = request.query.get("hot", "").lower() in ("1", "true", "yes")
        if hot:
            if not codes:
                return _err(400, "MISSING_CODES", "hot=1 时必须给 codes")
            return _ok(service.hot_payload(codes))
        return _ok(service.snapshot_payload(codes or None, include_all))

    async def bars(request: web.Request):
        codes = _parse_codes(request.query.get("codes", "")) or None
        start_date = request.query.get("start", "").replace("-", "")
        end_date = request.query.get("end", "").replace("-", "")
        try:
            limit = int(request.query.get("limit", "5000"))
        except ValueError:
            return _err(400, "BAD_LIMIT", "limit 必须是整数")
        data = await service.bars_payload(codes, start_date, end_date, limit)
        return _ok({"count": len(data), "bars": data})

    app.router.add_get("/health", health)
    app.router.add_get("/stats", stats)
    app.router.add_get("/snapshot", snapshot)
    app.router.add_get("/bars", bars)
    return app


async def start_http_server(service) -> web.AppRunner:
    app = build_app(service)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, service.config.host, service.config.port)
    await site.start()
    log.info("采集器 HTTP 已监听 http://%s:%s", service.config.host, service.config.port)
    return runner
