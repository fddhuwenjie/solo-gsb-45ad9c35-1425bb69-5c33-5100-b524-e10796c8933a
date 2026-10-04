"""测试环境配置：所有测试默认使用临时数据目录，避免触碰真实持久化路径。"""

from __future__ import annotations

import pytest

from app.main import get_store


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ROUTE_AUDIT_DATA_DIR", str(tmp_path / "data"))
    get_store.cache_clear()
    yield
    get_store.cache_clear()
