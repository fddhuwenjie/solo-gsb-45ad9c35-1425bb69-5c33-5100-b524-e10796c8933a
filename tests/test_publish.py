"""活动版本发布/读取端点的测试。

每个测试使用独立的临时数据目录，互不干扰；另含一个真实 uvicorn
服务器上的并发发布测试，验证相同期望版本只有一个发布成功。
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pytest
import uvicorn
from fastapi.testclient import TestClient

from app.main import app
from app.main import get_store


@pytest.fixture()
def client():
    return TestClient(app)


def publish(client, routes, expected_version):
    return client.post(
        "/publish",
        json={"routes": routes, "expected_version": expected_version},
    )


GOOD_ROUTES = [
    {"method": "GET", "path": "/users/{id:int}", "id": "get-user"},
    {"method": "GET", "path": "/users/me", "id": "get-me"},
    {"method": "POST", "path": "/users", "id": "create-user"},
]


def test_initial_active_is_version_0_empty(client):
    body = client.get("/active").json()
    assert body["version"] == 0
    assert body["routes"] == []
    assert body["paths"] == {}
    assert body["audit"]["routes"] == []
    assert body["audit"]["summary"]["total"] == 0


def test_publish_success_replaces_active_and_bumps_version(client):
    response = publish(client, GOOD_ROUTES, expected_version=0)
    assert response.status_code == 200
    body = response.json()
    assert body["published"] is True
    assert body["version"] == 1
    assert [r["id"] for r in body["routes"]] == ["get-user", "get-me", "create-user"]
    assert set(body["paths"]) == {"/users/{id}", "/users", "/users/me"}

    active = client.get("/active").json()
    assert active["version"] == 1
    assert active["paths"] == body["paths"]
    assert active["audit"]["conflicts"] == body["audit"]["conflicts"]


def test_parse_errors_are_rejected_with_original_indices_and_keep_active(client):
    routes = [
        {"method": "GET", "path": "/ok"},
        {"method": "GET", "path": "/bad/{n:float}"},
        {"method": "FROG", "path": "/x"},
    ]
    response = publish(client, routes, expected_version=0)
    assert response.status_code == 200
    body = response.json()
    assert body["published"] is False
    assert body["version"] == 0
    assert [e["index"] for e in body["parse_errors"]] == [1, 2]
    # 活动版本保持为空的初始版本
    assert client.get("/active").json()["version"] == 0


def test_duplicate_export_paths_are_rejected_with_original_index(client):
    routes = [
        {"method": "GET", "path": "/x/{id:int}"},
        {"method": "GET", "path": "/x/{id:uuid}"},  # 导出路径同为 /x/{id}
    ]
    body = publish(client, routes, expected_version=0).json()
    assert body["published"] is False
    assert body["parse_errors"] == []
    assert body["duplicate_routes"][0]["index"] == 1
    assert "duplicate route" in body["duplicate_routes"][0]["error"]
    assert client.get("/active").json()["version"] == 0


def test_full_shadow_is_rejected_with_loser_index(client):
    # 高优先级 catch-all 先注册，会完全遮蔽后注册的低优先级静态路由
    routes = [
        {"method": "GET", "path": "/files/{name}", "priority": 5, "id": "all"},
        {"method": "GET", "path": "/files/report.pdf", "priority": 0, "id": "file"},
    ]
    body = publish(client, routes, expected_version=0).json()
    assert body["published"] is False
    shadows = body["full_shadows"]
    assert len(shadows) == 1
    assert shadows[0]["kind"] == "full_shadow"
    assert shadows[0]["loser_index"] == 1
    assert shadows[0]["winner_index"] == 0
    assert client.get("/active").json()["version"] == 0


def test_stale_expected_version_returns_409_and_current_version(client):
    assert publish(client, GOOD_ROUTES, expected_version=0).json()["published"] is True
    response = publish(client, GOOD_ROUTES, expected_version=0)
    assert response.status_code == 409
    body = response.json()
    assert body["published"] is False
    assert body["version"] == 1
    # 旧提交没有覆盖已发布内容
    active = client.get("/active").json()
    assert active["version"] == 1
    assert [r["id"] for r in active["routes"]] == [
        "get-user",
        "get-me",
        "create-user",
    ]


def test_sequential_publishes_increment_version(client):
    first = publish(client, GOOD_ROUTES, expected_version=0)
    assert first.json()["version"] == 1

    second_routes = [{"method": "GET", "path": "/v2/things", "id": "thing"}]
    second = publish(client, second_routes, expected_version=1)
    assert second.status_code == 200
    body = second.json()
    assert body["published"] is True
    assert body["version"] == 2

    active = client.get("/active").json()
    assert active["version"] == 2
    assert [r["id"] for r in active["routes"]] == ["thing"]
    # 旧版本的片段没有残留在新快照中
    assert "/users/{id}" not in active["paths"]


def test_active_returns_consistent_snapshot(client):
    publish(client, GOOD_ROUTES, expected_version=0)
    active = client.get("/active").json()
    # 审计视图、审计结果中的路由与 paths 互相一致
    assert active["audit"]["routes"] == active["routes"]
    assert active["audit"]["summary"]["valid"] == len(active["routes"])
    assert "/users/{id}" in active["paths"]


def test_snapshot_persists_across_store_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("ROUTE_AUDIT_DATA_DIR", str(tmp_path))
    get_store.cache_clear()
    with TestClient(app) as client:
        body = publish(client, GOOD_ROUTES, expected_version=0).json()
        assert body["published"] is True

    # 磁盘文件存在且包含完整快照
    snapshot_file = tmp_path / "active-routes.json"
    assert snapshot_file.exists()
    on_disk = json.loads(snapshot_file.read_text(encoding="utf-8"))
    assert on_disk["version"] == 1
    assert on_disk["paths"]

    # 用同一目录构造全新存储实例（模拟容器重启）
    get_store.cache_clear()
    with TestClient(app) as client:
        active = client.get("/active").json()
    assert active["version"] == 1
    assert [r["id"] for r in active["routes"]] == [
        "get-user",
        "get-me",
        "create-user",
    ]
    assert active["paths"] == on_disk["paths"]


def test_legacy_explicit_endpoints_unchanged(client):
    response = client.post(
        "/analyze",
        json={"routes": [{"method": "GET", "path": "/bad/{n:float}"}]},
    )
    assert response.status_code == 200
    assert response.json()["parse_errors"][0]["index"] == 0

    response = client.post(
        "/export-openapi",
        json={"routes": [{"method": "GET", "path": "/ok"}]},
    )
    assert response.status_code == 200
    assert "/ok" in response.json()["paths"]

    response = client.post(
        "/render-paths",
        json={
            "routes": [
                {
                    "id": "r",
                    "template": "/users/{id:int}",
                    "parameters": {"id": 7},
                }
            ]
        },
    )
    assert response.status_code == 200
    assert response.json()["results"][0]["canonical_path"] == "/users/7"


def test_concurrent_publish_with_same_expected_version_single_winner(tmp_path, monkeypatch):
    monkeypatch.setenv("ROUTE_AUDIT_DATA_DIR", str(tmp_path))
    get_store.cache_clear()

    # 先绑定一个空闲端口再交给 uvicorn，避免依赖其内部套接字属性
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        # 轮询健康检查等待服务就绪
        for _ in range(100):
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/healthz", timeout=1
                ) as resp:
                    if resp.status == 200:
                        break
            except OSError:
                pass
            time.sleep(0.05)

        def do_publish(route_id):
            payload = json.dumps(
                {
                    "routes": [
                        {"method": "GET", "path": f"/items/{route_id}", "id": route_id}
                    ],
                    "expected_version": 0,
                }
            ).encode("utf-8")
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/publish",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read())

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(do_publish, ["alpha", "beta"]))

        statuses = sorted(code for code, _ in results)
        assert statuses == [200, 409]
        ok = next(body for code, body in results if code == 200)
        conflict = next(body for code, body in results if code == 409)
        assert ok["published"] is True
        assert ok["version"] == 1
        assert conflict["version"] == 1

        req = urllib.request.Request(f"http://127.0.0.1:{port}/active")
        with urllib.request.urlopen(req, timeout=10) as resp:
            active = json.loads(resp.read())
        assert active["version"] == 1
        winner_id = active["routes"][0]["id"]
        assert winner_id in {"alpha", "beta"}
        assert set(active["paths"]) == {f"/items/{winner_id}"}
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        get_store.cache_clear()
