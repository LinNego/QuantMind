"""Docker 出厂形态：文档挖掘（因子挖掘「上传文档」）默认开启。

为什么钉这条
------------
2026-10-24 用户拍板：**Docker 服务器版第一次安装、第一次启动就要能打开
「上传文档」链**，不允许「装完没有、还得再重建/重启一轮」。这条承诺由四层
配置共同构成，任何一层回归都会让入口在客户机上凭空消失，而且全链无报错：

1. ``docker/Dockerfile.web`` —— 离线镜像的前端构建**缺省**内联
   ``VITE_ENABLE_DOC_MINING=true``。前端开关是 Vite 构建期内联，容器重启、
   浏览器强刷都补不出来：镜像构建时没开，客户装多少次都没有入口。
2. ``docker-compose.yml`` —— 后端 ``ENABLE_DOC_MINING`` 缺省 true，
   否则界面可见但每个文档调用都 403 ``doc_mining_disabled``。
3. ``.env.example`` —— 客户复制出的 ``.env`` 缺省也是 true。
   compose 会自动读同目录 ``.env``，**``.env`` 里的值会覆盖 ``:-`` 缺省**，
   这一层最容易漏（改了 compose 缺省、客户一复制又关回去）。
4. ``scripts/deploy_frontend.sh`` —— 源码部署路径（``npm run build``）
   缺省也是开，否则「镜像装的有、源码装的没有」两副面孔。

反方向仍然保留：任一层显式 ``false`` 都是安全的失败方向；便携包与
Windows 桌面客户端**不在本约定内**（仍默认关闭，见启用指南第一节）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

#: Docker 出厂默认 = 开。四层任何一层改了这里都要同步改测试与启用指南。
EXPECTED_DEFAULT = "true"


def _repo_root() -> Path:
    """定位仓库根（需同时有 docker-compose.yml 与 .env.example）。"""
    for base in (Path(__file__).resolve(), Path.cwd().resolve()):
        for candidate in (base, *base.parents):
            if (candidate / "docker-compose.yml").is_file() and (
                candidate / ".env.example"
            ).is_file():
                return candidate
    pytest.fail("找不到仓库根（docker-compose.yml + .env.example 所在目录）")


def _read(path: Path) -> str:
    """读出厂配置文件；容器内未挂载时 skip 并说明原因（不静默空转）。"""
    if not path.is_file():
        pytest.skip(f"缺少 {path}（容器内未挂载该文件，本护栏在宿主/CI 执行）")
    return path.read_text(encoding="utf-8")


def _service_environment(compose_text: str, service: str) -> dict[str, str]:
    """把 compose 某服务的 environment 统一成 {key: value}。

    compose 支持两种写法：``- KEY=value`` 列表 与 ``KEY: value`` 映射。解析时
    ``${VAR:-default}`` 原样保留——本测试断言的是**缺省形态**，不是某个 .env。
    """
    config = yaml.safe_load(compose_text) or {}
    entry = (config.get("services") or {}).get(service) or {}
    raw = entry.get("environment") or {}
    if isinstance(raw, dict):
        return {str(k): "" if v is None else str(v) for k, v in raw.items()}
    out: dict[str, str] = {}
    for item in raw:
        text = str(item)
        key, _, value = text.partition("=")
        out[key.strip()] = value
    return out


def test_compose_defaults_doc_mining_on() -> None:
    """后端闸门缺省开：``ENABLE_DOC_MINING=${ENABLE_DOC_MINING:-true}``。"""
    root = _repo_root()
    env = _service_environment(_read(root / "docker-compose.yml"), "quantmind")

    assert env.get("ENABLE_DOC_MINING") == (
        f"${{ENABLE_DOC_MINING:-{EXPECTED_DEFAULT}}}"
    ), (
        "docker-compose.yml 的 quantmind 服务必须缺省开启文档挖掘 "
        "（否则客户装完第一次启动，上传文档端点全 403 doc_mining_disabled）"
    )


def test_env_example_defaults_doc_mining_on() -> None:
    """``.env.example`` 缺省开：客户复制成 ``.env`` 不会把功能关回去。"""
    root = _repo_root()
    text = _read(root / ".env.example")

    matches = re.findall(r"^ENABLE_DOC_MINING=(.*)$", text, flags=re.MULTILINE)
    assert matches, ".env.example 缺少 ENABLE_DOC_MINING（客户无从发现该开关）"
    assert matches[-1].strip() == EXPECTED_DEFAULT, (
        ".env.example 的 ENABLE_DOC_MINING 必须缺省 true——compose 会自动读 "
        "同目录 .env，客户一复制就把 compose 的 :- 缺省覆盖成关闭"
    )


def test_web_image_build_defaults_doc_mining_on() -> None:
    """离线镜像的前端构建缺省内联开关（构建期开关，重启补不出来）。"""
    root = _repo_root()
    text = _read(root / "docker" / "Dockerfile.web")

    assert re.search(
        rf"^ARG\s+VITE_ENABLE_DOC_MINING={EXPECTED_DEFAULT}\s*$",
        text,
        flags=re.MULTILINE,
    ), "docker/Dockerfile.web 必须以 ARG VITE_ENABLE_DOC_MINING=true 缺省构建镜像"
    assert re.search(
        r"^ENV\s+VITE_ENABLE_DOC_MINING=\$\{VITE_ENABLE_DOC_MINING\}\s*$",
        text,
        flags=re.MULTILINE,
    ), "docker/Dockerfile.web 必须把 ARG 导出为构建期环境变量（vite 只认 process env）"

    env_index = text.index("ENV VITE_ENABLE_DOC_MINING=")
    build_index = text.index("npx vite build")
    assert env_index < build_index, "开关必须在 vite build 之前生效"


def test_deploy_frontend_script_defaults_doc_mining_on() -> None:
    """源码部署入口缺省开：``deploy_frontend.sh`` 不带 env 也不能关掉入口。"""
    root = _repo_root()
    text = _read(root / "scripts" / "deploy_frontend.sh")

    assert re.search(
        r'VITE_ENABLE_DOC_MINING="\$\{VITE_ENABLE_DOC_MINING:-true\}"', text
    ), (
        "scripts/deploy_frontend.sh 必须把 VITE_ENABLE_DOC_MINING 缺省设为 true"
        "（否则源码部署的 Docker 服务器版与离线镜像两副面孔）"
    )
    assert re.search(
        r"^export\s+VITE_ENABLE_DOC_MINING\s*$", text, flags=re.MULTILINE
    ), "缺省值必须 export 出去，npm/vite 子进程才看得到"
