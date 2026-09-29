"""pytest 公共夹具：每个测试使用独立临时库，通过 TestClient 调用 API。"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def client(monkeypatch):
    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "test.db")
    monkeypatch.setenv("COC_DB_PATH", db_path)
    monkeypatch.setenv("COC_SEED_DEMO", "0")

    # 重新加载配置与数据库模块，使 settings.db_path 指向临时库
    from app import config
    importlib.reload(config)
    from app import database
    importlib.reload(database)
    from app import verification
    importlib.reload(verification)
    from app import seed
    importlib.reload(seed)
    from app import main
    importlib.reload(main)

    database.init_db()
    with TestClient(main.app) as c:
        yield c


def register(client, code="S-1", seal="SEAL-1", collector="采集人甲",
             collected_at="2026-09-20T08:00:00Z"):
    r = client.post("/api/samples", json={
        "sample_code": code, "collected_at": collected_at,
        "collector": collector, "seal_number": seal})
    assert r.status_code in (200, 201), r.text
    return r


def event(client, **kw):
    r = client.post("/api/events", json=kw)
    return r
