"""ModelScope 初始化数据（魔搭 → 本地 QuantDB 覆盖）的单元测试。

网络全部用 fake httpx.Client 拦截：tree 枚举走 .get()，文件下载走 .stream()，
校验 sha256/size 后原子落盘并写同步状态库。
"""

import hashlib
import sqlite3
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.services.engine.data_platform import modelscope_dataset_sync as ms
from backend.shared.quantdb_datasets import DATASETS


# ---------------------------------------------------------------------------
# fake httpx
# ---------------------------------------------------------------------------
class _JsonResp:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


class _StreamResp:
    def __init__(self, content: bytes):
        self._content = content

    def raise_for_status(self):
        return None

    def iter_bytes(self, chunk_size=0):
        step = chunk_size or len(self._content) or 1
        for i in range(0, len(self._content), step):
            yield self._content[i : i + step]


class _StreamCtx:
    def __init__(self, resp):
        self._resp = resp

    def __enter__(self):
        return self._resp

    def __exit__(self, *exc):
        return False


class _FakeClient:
    tree_pages: list = []
    payloads: dict = {}
    stream_calls: int = 0

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, params=None):
        page = int((params or {}).get("PageNumber", 1))
        return _JsonResp(self.tree_pages[page - 1])

    def stream(self, method, url):
        path = parse_qs(urlparse(url).query)["FilePath"][0]
        type(self).stream_calls += 1
        return _StreamCtx(_StreamResp(self.payloads[path]))


def _blob(path: str, content: bytes) -> dict:
    return {
        "Type": "blob",
        "Path": path,
        "Size": len(content),
        "Sha256": hashlib.sha256(content).hexdigest(),
    }


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------
def test_classify_maps_rel_dir_and_layout():
    assert ms._classify("1_kline_data/daily_forward/dt=20260101/data.parquet") == (
        "daily_forward",
        "v2_daily_partition",
    )
    assert ms._classify("3_financial_data/income/000001.SZ.parquet") == (
        "income",
        "v1_symbol",
    )
    assert ms._classify("README.md") == (None, "v2_manifest")
    assert ms._classify("1_kline_data/daily_forward") == (None, "v2_manifest")


def test_classify_covers_all_dataset_specs():
    for spec in DATASETS:
        dataset, layout = ms._classify(f"{spec.rel_dir}/x/data.parquet")
        assert dataset == spec.dataset
        expected = "v2_daily_partition" if spec.layout == "partition" else "v1_symbol"
        assert layout == expected


def test_target_path_rejects_traversal(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        ms._target_path(tmp_path, "../evil.parquet")
    with pytest.raises(ValueError):
        ms._target_path(tmp_path, "/etc/passwd")
    assert ms._target_path(tmp_path, "1_kline_data/a.parquet").is_relative_to(tmp_path)


def test_local_breakdown(tmp_path):
    content = b"x" * 32
    remote = ms.RemoteFile(
        path="1_kline_data/daily_forward/dt=20260101/data.parquet",
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        dataset="daily_forward",
        layout="v2_daily_partition",
    )
    missing = ms.RemoteFile(
        path="1_kline_data/daily_forward/dt=20260102/data.parquet",
        size=8,
        sha256="",
        dataset="daily_forward",
        layout="v2_daily_partition",
    )
    changed = ms.RemoteFile(
        path="1_kline_data/daily_forward/dt=20260103/data.parquet",
        size=16,
        sha256="",
        dataset="daily_forward",
        layout="v2_daily_partition",
    )
    # 未落地：全部计入 missing
    assert ms._local_breakdown(tmp_path, [remote, missing, changed]) == (
        0,
        32 + 8 + 16,
        0,
    )

    target = tmp_path / remote.path
    target.parent.mkdir(parents=True)
    target.write_bytes(content)  # size 一致 → present
    stale = tmp_path / changed.path
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"y")  # size 不同 → changed

    assert ms._local_breakdown(tmp_path, [remote, missing, changed]) == (32, 8, 16)


# ---------------------------------------------------------------------------
# list_remote_files
# ---------------------------------------------------------------------------
def test_list_remote_files_paginates_and_dedupes(monkeypatch):
    page1 = {
        "Code": 200,
        "Data": {
            "TotalCount": 3,
            "Files": [
                {"Type": "tree", "Path": "1_kline_data/daily_forward"},
                _blob("1_kline_data/daily_forward/dt=20260101/data.parquet", b"aa"),
            ],
        },
    }
    page2 = {
        "Code": 200,
        "Data": {
            "TotalCount": 3,
            "Files": [_blob("3_financial_data/income/000001.SZ.parquet", b"bb")],
        },
    }
    _FakeClient.tree_pages = [page1, page2]
    _FakeClient.payloads = {}
    monkeypatch.setattr(ms.httpx, "Client", _FakeClient)

    files = ms.list_remote_files(endpoint="https://example.test", repo_id="ns/repo")
    paths = sorted(f.path for f in files)
    assert paths == [
        "1_kline_data/daily_forward/dt=20260101/data.parquet",
        "3_financial_data/income/000001.SZ.parquet",
    ]
    assert files[0].dataset == "daily_forward"
    assert files[1].dataset == "income"


# ---------------------------------------------------------------------------
# init_from_modelscope
# ---------------------------------------------------------------------------
def _make_remote():
    c1 = b"parquet-one" * 4
    c2 = b"parquet-two" * 8
    return (
        c1,
        c2,
        [
            ms.RemoteFile(
                path="1_kline_data/daily_forward/dt=20260101/data.parquet",
                size=len(c1),
                sha256=hashlib.sha256(c1).hexdigest(),
                dataset="daily_forward",
                layout="v2_daily_partition",
            ),
            ms.RemoteFile(
                path="1_kline_data/daily_forward/dt=20260102/data.parquet",
                size=len(c2),
                sha256=hashlib.sha256(c2).hexdigest(),
                dataset="daily_forward",
                layout="v2_daily_partition",
            ),
        ],
    )


def test_init_downloads_and_writes_state(tmp_path, monkeypatch):
    root = tmp_path / "quantdb"
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(root))
    monkeypatch.setenv("QUANTDB_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("MODELSCOPE_SYNC_WORKERS", "2")

    c1, c2, remote = _make_remote()
    _FakeClient.payloads = {remote[0].path: c1, remote[1].path: c2}
    _FakeClient.stream_calls = 0
    monkeypatch.setattr(ms.httpx, "Client", _FakeClient)
    monkeypatch.setattr(ms, "list_remote_files", lambda **kw: remote)

    summary = ms.init_from_modelscope(["daily_forward"])

    assert summary["downloaded"] == 2
    assert summary["errors"] == 0
    assert (root / remote[0].path).read_bytes() == c1
    assert (root / remote[1].path).read_bytes() == c2
    assert summary["state"]["status"] == "ok"

    from backend.scripts.quantdb_daily_sync import _state_path

    conn = sqlite3.connect(str(_state_path(root)))
    rows = conn.execute(
        "SELECT key, sha256, size FROM objects WHERE dataset='daily_forward' ORDER BY key"
    ).fetchall()
    conn.close()
    assert [r[0] for r in rows] == [remote[0].path, remote[1].path]
    assert rows[0][1] == remote[0].sha256
    assert rows[0][2] == remote[0].size


def test_init_redownloads_without_state(tmp_path, monkeypatch):
    """状态库无登记（或内容不符）时不跳过，重下并原地覆盖。"""
    root = tmp_path / "quantdb"
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(root))
    monkeypatch.setenv("QUANTDB_STATE_DIR", str(tmp_path / "state"))

    c1, c2, remote = _make_remote()
    # 预先放好第一个文件，内容为旧数据（且状态库无登记）
    target = root / remote[0].path
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old-content")

    _FakeClient.payloads = {remote[0].path: c1, remote[1].path: c2}
    _FakeClient.stream_calls = 0
    monkeypatch.setattr(ms.httpx, "Client", _FakeClient)
    monkeypatch.setattr(ms, "list_remote_files", lambda **kw: remote)

    summary = ms.init_from_modelscope(["daily_forward"])

    assert summary["downloaded"] == 2
    assert summary["skipped"] == 0
    assert _FakeClient.stream_calls == 2
    assert target.read_bytes() == c1  # 已被魔搭内容覆盖


def test_init_resumes_skipping_completed_files(tmp_path, monkeypatch):
    """断点续传：size 一致且状态库 sha256 == 远端 sha256 的文件跳过。"""
    root = tmp_path / "quantdb"
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(root))
    monkeypatch.setenv("QUANTDB_STATE_DIR", str(tmp_path / "state"))

    c1, c2, remote = _make_remote()
    # 文件 1 已完整下载：本地存在且 size 一致 + 状态库登记 sha256 与远端一致
    t1 = root / remote[0].path
    t1.parent.mkdir(parents=True)
    t1.write_bytes(c1)
    ms._upsert_state_rows(
        root,
        [
            (
                remote[0].path,
                remote[0].sha256,
                remote[0].sha256,
                remote[0].size,
                str(t1),
                remote[0].layout,
                "daily_forward",
            )
        ],
    )

    # 文件 2 缺失 → 需下载
    _FakeClient.payloads = {remote[1].path: c2}
    _FakeClient.stream_calls = 0
    monkeypatch.setattr(ms.httpx, "Client", _FakeClient)
    monkeypatch.setattr(ms, "list_remote_files", lambda **kw: remote)

    summary = ms.init_from_modelscope(["daily_forward"])

    assert summary["skipped"] == 1
    assert summary["downloaded"] == 1
    assert _FakeClient.stream_calls == 1
    assert summary["datasets"]["daily_forward"]["skipped"] == 1


def test_init_redownloads_when_state_sha_differs(tmp_path, monkeypatch):
    """状态库 sha256 与远端不一致 → 不跳过，重下覆盖。"""
    root = tmp_path / "quantdb"
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(root))
    monkeypatch.setenv("QUANTDB_STATE_DIR", str(tmp_path / "state"))

    c1, c2, remote = _make_remote()
    t1 = root / remote[0].path
    t1.parent.mkdir(parents=True)
    t1.write_bytes(c1)
    # 状态库登记的是旧的 sha256（与远端不同）
    ms._upsert_state_rows(
        root,
        [
            (
                remote[0].path,
                "0" * 64,
                "0" * 64,
                remote[0].size,
                str(t1),
                remote[0].layout,
                "daily_forward",
            )
        ],
    )

    _FakeClient.payloads = {remote[0].path: c1, remote[1].path: c2}
    _FakeClient.stream_calls = 0
    monkeypatch.setattr(ms.httpx, "Client", _FakeClient)
    monkeypatch.setattr(ms, "list_remote_files", lambda **kw: remote)

    summary = ms.init_from_modelscope(["daily_forward"])

    assert summary["skipped"] == 0
    assert summary["downloaded"] == 2


def test_init_rejects_unknown_dataset(tmp_path, monkeypatch):
    import pytest

    root = tmp_path / "quantdb"
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(root))
    monkeypatch.setenv("QUANTDB_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(ms, "list_remote_files", lambda **kw: [])

    with pytest.raises(ValueError):
        ms.init_from_modelscope(["not_a_dataset"])


def test_init_aborts_on_empty_remote_manifest_without_touching_local(
    tmp_path, monkeypatch
):
    """远端清单为空 → 直接中止；本地文件/状态库一个字节都不动。

    用户红线（2026-10-21）：数据集初始化**绝不能**「先清空再拉」，
    无论远端枚举失败、仓库下线还是 API 返回空清单，都不得删除本地数据。

    断言必须覆盖「任何本地写入之前」：两套同步状态库（quantmind / sdk）
    都不能被创建——否则守卫若被挪到状态库重建之后，只查数据文件会漏网。
    """
    import pytest

    from backend.scripts.quantdb_daily_sync import _state_path

    root = tmp_path / "quantdb"
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(root))
    monkeypatch.setenv("QUANTDB_STATE_DIR", str(tmp_path / "state"))
    sentinel = root / "1_kline_data/daily_forward/dt=20260101/data.parquet"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_bytes(b"local-only")
    monkeypatch.setattr(ms, "list_remote_files", lambda **kw: [])

    with pytest.raises(RuntimeError, match="远端清单为空"):
        ms.init_from_modelscope(None)

    assert sentinel.read_bytes() == b"local-only"
    assert not _state_path(root).exists()
    assert not (root / "quantdb_sync.sqlite").exists()


def test_init_cancel_during_enumeration_returns_cancelled_without_writes(
    tmp_path, monkeypatch
):
    """枚举阶段取消 → cancelled=True，不误报「远端为空」，不落任何本地痕迹。

    list_remote_files 在取消时返回空清单；守卫必须先判取消再判空，
    否则后台任务会把用户主动取消记成 failed（2026-10-21 代码审查发现）。
    """
    from backend.scripts.quantdb_daily_sync import _state_path

    root = tmp_path / "quantdb"
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(root))
    monkeypatch.setenv("QUANTDB_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(ms, "list_remote_files", lambda **kw: [])

    summary = ms.init_from_modelscope(None, should_cancel=lambda: True)

    assert summary["cancelled"] is True
    assert summary["downloaded"] == 0
    assert summary["errors"] == 0
    assert not root.exists()
    assert not _state_path(root).exists()
    assert not (root / "quantdb_sync.sqlite").exists()


def test_init_never_deletes_local_only_files_or_state(tmp_path, monkeypatch):
    """只覆盖远端清单内的文件：远端没有的本地文件与未涉及的数据集原样保留。

    远端「有明细」才允许动——而且是逐文件原子覆盖；远端没有的路径（本地独有
    分区、其它数据集）与数据文件一条都不许删。状态库口径：仅本次远端命中的
    数据集按远端 sha256 重建（其本地独有文件的 objects 登记会随之移除），
    未选中数据集的状态登记不动。
    """
    root = tmp_path / "quantdb"
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(root))
    monkeypatch.setenv("QUANTDB_STATE_DIR", str(tmp_path / "state"))

    c1, c2, remote = _make_remote()
    # 本地独有文件（远端清单里没有）——必须存活
    local_only = root / "1_kline_data/daily_forward/dt=20250101/data.parquet"
    local_only.parent.mkdir(parents=True)
    local_only.write_bytes(b"local-only")
    # 另一个数据集的本地文件 + 状态登记——不能被本次初始化波及
    other = root / "3_financial_data/income/000001.SZ.parquet"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"income-local")
    ms._upsert_state_rows(
        root,
        [
            (
                "3_financial_data/income/000001.SZ.parquet",
                "a" * 64,
                "a" * 64,
                12,
                str(other),
                "v1_symbol",
                "income",
            )
        ],
    )

    _FakeClient.payloads = {remote[0].path: c1, remote[1].path: c2}
    _FakeClient.stream_calls = 0
    monkeypatch.setattr(ms.httpx, "Client", _FakeClient)
    monkeypatch.setattr(ms, "list_remote_files", lambda **kw: remote)

    summary = ms.init_from_modelscope(["daily_forward"])

    assert summary["downloaded"] == 2
    assert local_only.read_bytes() == b"local-only"
    assert other.read_bytes() == b"income-local"
    from backend.scripts.quantdb_daily_sync import _state_path

    conn = sqlite3.connect(str(_state_path(root)))
    income_rows = conn.execute(
        "SELECT key FROM objects WHERE dataset='income'"
    ).fetchall()
    conn.close()
    assert income_rows == [("3_financial_data/income/000001.SZ.parquet",)]


def test_classify_per_share():
    assert ms._classify("3_financial_data/per_share/000001.SZ.parquet") == (
        "per_share",
        "v1_symbol",
    )


def test_preflight_orders_by_spec_and_has_repo_url(tmp_path, monkeypatch):
    root = tmp_path / "quantdb"
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(root))
    monkeypatch.setenv("QUANTDB_STATE_DIR", str(tmp_path / "state"))

    # 故意乱序、跨类别，验证输出按 DATASETS 规格顺序（即 6 大类分组）
    remote = [
        ms.RemoteFile(
            path="5_technical_derived/valuation/dt=20260101/data.parquet",
            size=1,
            sha256="a",
            dataset="valuation",
            layout="v2_daily_partition",
        ),
        ms.RemoteFile(
            path="1_kline_data/daily_forward/dt=20260101/data.parquet",
            size=1,
            sha256="b",
            dataset="daily_forward",
            layout="v2_daily_partition",
        ),
        ms.RemoteFile(
            path="3_financial_data/per_share/000001.SZ.parquet",
            size=1,
            sha256="c",
            dataset="per_share",
            layout="v1_symbol",
        ),
        ms.RemoteFile(
            path="6_ml_datasets/l1_factors/dt=20260101/data.parquet",
            size=1,
            sha256="d",
            dataset="l1_factors",
            layout="v2_daily_partition",
        ),
    ]
    monkeypatch.setattr(ms, "list_remote_files", lambda **kw: remote)

    # 本地已存在 daily_forward 的 1 字节文件，且状态库登记了与远端一致的 sha256
    local = root / "1_kline_data" / "daily_forward" / "dt=20260101" / "data.parquet"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"x")
    ms._upsert_state_rows(
        root,
        [
            (
                "1_kline_data/daily_forward/dt=20260101/data.parquet",
                "b",
                "b",
                1,
                str(local),
                "v2_daily_partition",
                "daily_forward",
            )
        ],
    )

    pf = ms.preflight_modelscope()
    assert [d["dataset"] for d in pf["datasets"]] == [
        "daily_forward",
        "per_share",
        "valuation",
        "l1_factors",
    ]
    assert (
        pf["repo_url"]
        == "https://www.modelscope.cn/datasets/qusong0627/LightGBM_Alpha300"
    )
    assert pf["total_bytes"] == 4
    assert pf["skip_files"] == 1  # size 一致 + 状态库 sha256 一致
    assert pf["skip_bytes"] == 1
    assert pf["missing_bytes"] == 3
    assert pf["changed_bytes"] == 0
