from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _tmp_dir(prefix: str) -> Path:
    base = REPO_ROOT / ".tmp"
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{prefix}-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def workdir():
    """工作区内的临时目录。

    不用 pytest 的 tmp_path，也不用 tempfile.mkdtemp：某些 Windows 沙箱环境下
    mkdtemp 创建出来的目录会带上畸形权限，连自己都读不了。用普通 mkdir 就正常。
    """
    path = _tmp_dir("case")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(autouse=True)
def isolated_defaults(monkeypatch):
    """让测试不碰仓库里的 data/，也不因为默认策略互相干扰。

    - store 指到临时文件：否则每个测试都会往 data/store.db 里写口径；
    - 默认关掉人工审批：绝大多数测试关心的是别的逻辑，需要审批的测试自己显式打开。
    """
    path = _tmp_dir("store") / "store.db"
    monkeypatch.setenv("INSIGHT_STORE_PATH", str(path))
    monkeypatch.setenv("INSIGHT_APPROVAL_ENABLED", "false")
    yield path
    shutil.rmtree(path.parent, ignore_errors=True)