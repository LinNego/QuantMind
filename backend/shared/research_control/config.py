"""研究总控 LLM 配置：独立端点 / 模型 / API key。

**为什么单独一套**：研究总控要写长报告（多面事实 + 次日推演），与盘中决策模型
的 token 预算、温度、供应商都不同。共用 ``GLM_*`` / ``OPENAI_*`` 会出现「换研究
模型把交易模型一起换掉」——配置面必须能各改各的。

**存储**：``config/runtime.env``（``backend.shared.runtime_secrets``），与决策名册
同纪律——key 只写不回，读侧只给掩码。API 进程写文件的同时写自己的进程环境；
宿主 cron 脚本每次新起进程，读同一份挂载文件，所以保存后下一档即生效，
不需要重启任何服务。

**回落**：``QM_RESEARCH_LLM_*`` 缺 key 时按 ``provider`` 回落现有供应商变量
（glm → ``GLM_API_BASE/GLM_API_KEY``；deepseek → ``DEEPSEEK_BASE_URL``；
其余 → ``OPENAI_API_BASE/OPENAI_API_KEY``）。回落不是静默：``describe()`` 会
明确标注 ``source=fallback``，前端据此提示「当前在用交易模型那套 key」。

**不做的事**：不打印 key；异常文案只点名变量名；不把 key 写进报告/日志/对话流。
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from dataclasses import dataclass

from backend.shared.runtime_secrets import (
    delete_secret,
    get_secret,
    mask_secret,
    read_runtime_env,
    set_secret,
)

#: 五档复盘（顺序即一天里的时间顺序）
SLOTS: tuple[str, ...] = ("midday", "close", "inference", "news", "night")

#: 未配置模型时的默认值：与 night_pool_agent 现行大脑一致，避免升级后行为跳变。
DEFAULT_MODEL = "glm-5.3-flash"
DEFAULT_TIMEOUT_S = 420.0
DEFAULT_MAX_TOKENS = 16000
DEFAULT_TEMPERATURE = 0.2

ENV_BASE = "QM_RESEARCH_LLM_BASE_URL"
ENV_KEY = "QM_RESEARCH_LLM_API_KEY"
ENV_MODEL = "QM_RESEARCH_LLM_MODEL"
ENV_PROVIDER = "QM_RESEARCH_LLM_PROVIDER"
ENV_TIMEOUT = "QM_RESEARCH_LLM_TIMEOUT"
ENV_MAX_TOKENS = "QM_RESEARCH_LLM_MAX_TOKENS"
ENV_TEMPERATURE = "QM_RESEARCH_LLM_TEMPERATURE"
ENV_REASONING = "QM_RESEARCH_LLM_REASONING_EFFORT"

#: 保存时写入 runtime.env 的四件套（顺序即前端表单顺序）。
MANAGED_KEYS: tuple[str, ...] = (ENV_BASE, ENV_MODEL, ENV_PROVIDER, ENV_KEY)

#: 占位符判据（与 ``decision_llm_client.looks_like_placeholder`` 同口径，本包
#: 不 import 那个模块——研究总控在宿主 cron 里跑，少背一层交易依赖）。
_PLACEHOLDER_MARKERS = ("your-", "mock-", "xxx", "在此", "changeme", "<", ">")


class ResearchLLMNotConfigured(RuntimeError):
    """研究总控 LLM 未配置（只点名变量，不带值）。"""


def looks_like_placeholder(value: object) -> bool:
    low = str(value or "").strip().lower()
    return (not low) or any(m in low for m in _PLACEHOLDER_MARKERS)


def effective_env() -> dict[str, str]:
    """``runtime.env`` 覆盖进程环境（文件优先，空值不覆盖）。

    与 ``decision_llm_client.effective_env`` 同取向：管理台保存的值必须在
    子进程重启后仍然生效；纯读 ``os.environ`` 会静默回退到 supervisor 启动
    那一刻的旧快照。
    """
    src = dict(os.environ)
    try:
        src.update({k: v for k, v in read_runtime_env().items() if str(v).strip()})
    except Exception:  # noqa: BLE001 - 运维面读失败按进程环境跑，不拖垮复盘
        pass
    return src


def _provider_of(model: str, provider: str = "") -> str:
    p = (provider or "").strip().lower()
    if p:
        return p
    low = (model or "").strip().lower()
    if low.startswith("glm"):
        return "glm"
    if low.startswith("deepseek"):
        return "deepseek"
    return "openai"


def _fallback_credentials(provider: str, src: Mapping[str, str]) -> tuple[str, str]:
    """按供应商取现有变量（不回落到「另一家」的 key）。"""
    if provider == "glm":
        return (
            str(src.get("GLM_API_BASE") or "").strip(),
            str(src.get("GLM_API_KEY") or "").strip(),
        )
    if provider == "deepseek":
        base = str(
            src.get("DEEPSEEK_BASE_URL") or src.get("OPENAI_API_BASE") or ""
        ).strip()
        key = str(
            src.get("DEEPSEEK_API_KEY") or src.get("OPENAI_API_KEY") or ""
        ).strip()
        return base, key
    return (
        str(src.get("OPENAI_API_BASE") or "").strip(),
        str(src.get("OPENAI_API_KEY") or "").strip(),
    )


@dataclass(frozen=True)
class ResearchLLMConfig:
    """一次研究总控调用的供应商配置（``base_url`` 含 ``/v1``）。"""

    base_url: str
    api_key: str
    model: str
    provider: str = "openai"
    timeout: float = DEFAULT_TIMEOUT_S
    max_tokens: int = DEFAULT_MAX_TOKENS
    temperature: float = DEFAULT_TEMPERATURE
    reasoning_effort: str = ""
    source: str = "research"  # research | fallback

    @property
    def chat_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    def masked_key(self) -> str:
        return mask_secret(self.api_key)


def _num(src: Mapping[str, str], key: str, default, cast):
    raw = str(src.get(key) or "").strip()
    if not raw:
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        raise ResearchLLMNotConfigured(f"{key} 不是合法数字：{raw!r}") from None


def resolve_config(env: Mapping[str, str] | None = None) -> ResearchLLMConfig:
    """读配置；缺件 → :class:`ResearchLLMNotConfigured`（文案只点名变量）。"""
    src = effective_env() if env is None else dict(env)
    model = str(src.get(ENV_MODEL) or DEFAULT_MODEL).strip()
    provider = _provider_of(model, str(src.get(ENV_PROVIDER) or ""))

    research_base = str(src.get(ENV_BASE) or "").strip()
    research_key = str(src.get(ENV_KEY) or "").strip()
    fallback_base, fallback_key = _fallback_credentials(provider, src)

    base_url = research_base or fallback_base
    api_key = research_key or fallback_key
    source = "research" if research_base and research_key else "fallback"

    missing: list[str] = []
    if looks_like_placeholder(base_url):
        missing.append(ENV_BASE)
    if looks_like_placeholder(api_key):
        missing.append(ENV_KEY)
    if looks_like_placeholder(model):
        missing.append(ENV_MODEL)
    if missing:
        raise ResearchLLMNotConfigured(
            "研究总控 LLM 未配置："
            + ", ".join(missing)
            + " 为空或仍是占位符（也可先在「设置 → 研究总控」保存独立 key；"
            "未保存时按模型前缀回落 GLM_*/OPENAI_*/DEEPSEEK_*）"
        )

    return ResearchLLMConfig(
        base_url=base_url,
        api_key=api_key,
        model=model,
        provider=provider,
        timeout=_num(src, ENV_TIMEOUT, DEFAULT_TIMEOUT_S, float),
        max_tokens=_num(src, ENV_MAX_TOKENS, DEFAULT_MAX_TOKENS, int),
        temperature=_num(src, ENV_TEMPERATURE, DEFAULT_TEMPERATURE, float),
        reasoning_effort=str(src.get(ENV_REASONING) or "").strip(),
        source=source,
    )


def describe(env: Mapping[str, str] | None = None) -> dict:
    """配置现状（**永不抛**：坏配置的时候正是最需要看这一页的时候）。"""
    src = effective_env() if env is None else dict(env)
    model = str(src.get(ENV_MODEL) or DEFAULT_MODEL).strip()
    provider = _provider_of(model, str(src.get(ENV_PROVIDER) or ""))
    research_key = str(src.get(ENV_KEY) or "").strip()
    fallback_base, fallback_key = _fallback_credentials(provider, src)
    base_url = str(src.get(ENV_BASE) or "").strip() or fallback_base
    key = research_key or fallback_key
    source = (
        "research"
        if str(src.get(ENV_BASE) or "").strip() and research_key
        else "fallback"
    )
    out: dict = {
        "model": model,
        "provider": provider,
        "base_url": base_url,
        "key_configured": not looks_like_placeholder(key),
        "key_mask": mask_secret(key) if key else "",
        "source": source,
        "timeout": _safe_num(src, ENV_TIMEOUT, DEFAULT_TIMEOUT_S, float),
        "max_tokens": _safe_num(src, ENV_MAX_TOKENS, DEFAULT_MAX_TOKENS, int),
        "temperature": _safe_num(src, ENV_TEMPERATURE, DEFAULT_TEMPERATURE, float),
        "slots": list(SLOTS),
        "ready": False,
        "error": "",
    }
    try:
        resolve_config(src)
        out["ready"] = True
    except ResearchLLMNotConfigured as exc:
        out["error"] = str(exc)
    return out


def _safe_num(src: Mapping[str, str], key: str, default, cast):
    try:
        return _num(src, key, default, cast)
    except ResearchLLMNotConfigured:
        return default


def save_config(body: Mapping[str, object]) -> dict:
    """保存独立配置。``api_key`` 留空 = 沿用原 key（只写不回）。

    校验不过抛 ``ValueError``（一条人话），**一条都不落盘**。返回 ``describe()``。
    """
    base_url = str(body.get("base_url") or "").strip()
    model = str(body.get("model") or "").strip() or DEFAULT_MODEL
    provider = str(body.get("provider") or "").strip() or _provider_of(model)
    api_key = str(body.get("api_key") or "").strip()
    clear_key = bool(body.get("clear_api_key"))

    if "base_url" in body and looks_like_placeholder(base_url):
        raise ValueError(
            "base_url 不能为空（例：https://api.deepseek.com/v1 或 GLM 端点）"
        )
    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        raise ValueError("base_url 必须以 http:// 或 https:// 开头（含 /v1）")
    if looks_like_placeholder(model):
        raise ValueError("model 不能为空或占位符")
    if provider not in ("glm", "openai", "deepseek", "custom"):
        raise ValueError("provider 只支持 glm / openai / deepseek / custom")

    set_secret(ENV_BASE, base_url)
    set_secret(ENV_MODEL, model)
    set_secret(ENV_PROVIDER, provider)
    if api_key:
        set_secret(ENV_KEY, api_key)
    elif clear_key:
        delete_secret(ENV_KEY)
    return describe()


def clear_config() -> dict:
    """清空独立配置（回到按模型前缀回落供应商变量）。"""
    for key in MANAGED_KEYS:
        delete_secret(key)
    return describe()


def _chat_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/chat/completions"


def test_connection(
    overrides: Mapping[str, object] | None = None,
    *,
    timeout_s: float = 40.0,
) -> dict:
    """发一次最小请求验证端点/key/模型。**不落盘**。

    ``overrides`` 可带 ``base_url/model/provider/api_key``（前端「先测试再保存」）；
    ``api_key`` 留空时用已保存的 key。返回 ``{ok, model, provider, latency_ms,
    reply, error, source}``——错误只给状态码/供应商错误文本，**不含 key**。
    """
    body = dict(overrides or {})
    saved = describe()
    base_url = str(body.get("base_url") or saved.get("base_url") or "").strip()
    model = str(body.get("model") or saved.get("model") or DEFAULT_MODEL).strip()
    provider = _provider_of(
        model, str(body.get("provider") or saved.get("provider") or "")
    )
    api_key = str(body.get("api_key") or "").strip()
    if not api_key:
        # describe 不回 key；用解析器拿已保存/回落的那把（只在本进程内使用）
        try:
            api_key = resolve_config().api_key
        except ResearchLLMNotConfigured:
            api_key = ""
    started = time.monotonic()
    out: dict = {
        "ok": False,
        "model": model,
        "provider": provider,
        "base_url": base_url,
        "latency_ms": None,
        "reply": "",
        "error": "",
        "source": "override" if body else saved.get("source", ""),
    }
    if looks_like_placeholder(base_url) or not (
        base_url.startswith("http://") or base_url.startswith("https://")
    ):
        out["error"] = "base_url 未配置或格式不对（需含 http(s):// 与 /v1）"
        return out
    if looks_like_placeholder(api_key):
        out["error"] = "API key 未配置（保存时只写不回；此处请重新输入一次再测试）"
        return out

    payload: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是连通性测试助手。"},
            {"role": "user", "content": "只回复四个字：连接正常"},
        ],
        "temperature": 0.0,
        "max_tokens": 64,
    }
    if provider == "glm":
        payload["reasoning_effort"] = "low"
    try:
        import httpx

        resp = httpx.post(
            _chat_url(base_url),
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=timeout_s,
        )
        out["latency_ms"] = int((time.monotonic() - started) * 1000)
        if resp.status_code >= 400:
            out["error"] = f"HTTP {resp.status_code}：{resp.text[:300]}"
            return out
        data = resp.json()
        choices = data.get("choices") or []
        msg = (
            (choices[0] or {}).get("message")
            if choices and isinstance(choices[0], dict)
            else {}
        )
        out["reply"] = str((msg or {}).get("content") or "").strip()[:200]
        out["ok"] = bool(out["reply"])
        if not out["ok"]:
            out["error"] = f"响应无正文：{str(data)[:300]}"
    except Exception as exc:  # noqa: BLE001 - 测试面如实回报，不抛给路由层
        out["latency_ms"] = int((time.monotonic() - started) * 1000)
        out["error"] = f"{type(exc).__name__}: {exc}"[:500]
    return out


def get_saved_key_present() -> bool:
    """管理面辅助：key 是否已存在（不返回值）。"""
    try:
        return not looks_like_placeholder(get_secret(ENV_KEY))
    except Exception:  # noqa: BLE001
        return False
