"""FastAPI 应用入口。"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .exporter import export_openapi_fragment
from .models import (
    ActiveSnapshotResponse,
    AnalyzeRequest,
    AnalyzeResponse,
    ExportOpenApiRequest,
    ExportOpenApiResponse,
    PublishRoutesRequest,
    PublishRoutesResponse,
    RenderPathsRequest,
    RenderPathsResponse,
)
from .publishing import validate_candidate
from .render import render_paths
from .service import analyze_routes
from .store import SnapshotStore

app = FastAPI(
    title="HTTP 路由覆盖冲突审计 API",
    description=(
        "按注册顺序接收路由模板，判定同方法路由的可达集合是否相交、谁遮蔽谁，"
        "并构造最短稳定的示例路径；也可把路由配置导出为可嵌入 OpenAPI 3.1 的 "
        "paths 片段，或按模板与参数值反向生成可调用的 canonical path；"
        "并提供按乐观版本号原子发布、可持久恢复的活动配置。"
    ),
    version="1.1.0",
)


@lru_cache(maxsize=1)
def get_store() -> SnapshotStore:
    """活动快照存储；数据目录可经 ``ROUTE_AUDIT_DATA_DIR`` 覆盖。"""
    return SnapshotStore(os.environ.get("ROUTE_AUDIT_DATA_DIR", "/data"))


@app.get("/healthz", tags=["meta"])
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/analyze", response_model=AnalyzeResponse, tags=["audit"])
def analyze(request: AnalyzeRequest) -> AnalyzeResponse:
    """分析一组路由的覆盖冲突。"""
    return analyze_routes(request.routes)


@app.post("/export-openapi", response_model=ExportOpenApiResponse, tags=["export"])
def export_openapi(request: ExportOpenApiRequest) -> ExportOpenApiResponse:
    """把路由配置转换为可嵌入 OpenAPI 3.1 的 paths 片段（不含冲突统计）。"""
    return export_openapi_fragment(request.routes)


@app.post("/render-paths", response_model=RenderPathsResponse, tags=["render"])
def render_path_batch(request: RenderPathsRequest) -> RenderPathsResponse:
    """按路由模板与参数值反向生成可调用 URL（逐条报告错误，不中止批处理）。"""
    return render_paths(request.routes)


@app.post(
    "/publish",
    response_model=PublishRoutesResponse,
    tags=["active-config"],
    responses={409: {"model": PublishRoutesResponse}},
)
def publish_routes(request: PublishRoutesRequest) -> Any:
    """把候选路由集校验后原子发布为新的活动版本。

    - 对同一候选集同时运行冲突审计与 OpenAPI 导出；
    - 任一侧有解析错误、导出路径重复或审计含 proven 的 full_shadow：
      返回 200 与携带原始下标的拒绝明细，活动版本不变；
    - 校验通过但 ``expected_version`` 不等于当前版本：返回 409 与当前
      版本（相同期望版本的并发发布只有一个成功）；
    - 校验通过且期望版本匹配：原子替换活动路由并把版本加一后整体落盘。
    """
    check = validate_candidate(request.routes)
    if not check.accepted:
        return PublishRoutesResponse(
            published=False,
            version=get_store().current_version(),
            parse_errors=check.parse_errors,
            duplicate_routes=check.duplicate_routes,
            full_shadows=check.full_shadows,
        )

    # 校验在锁外完成，CAS 提交在锁内；较重的计算不阻塞其它读取。
    routes_payload = [
        route.model_dump(by_alias=True, exclude_none=True)
        for route in request.routes
    ]
    published, version, snapshot = get_store().publish(
        expected_version=request.expected_version,
        routes_payload=routes_payload,
        audit=check.audit.model_dump(),
        paths=check.export.paths,
    )
    if not published:
        return JSONResponse(
            status_code=409,
            content={
                "published": False,
                "version": version,
                "detail": (
                    f"expected_version {request.expected_version} no longer matches "
                    f"current version {version}; re-read /active and retry"
                ),
            },
        )

    assert snapshot is not None
    return PublishRoutesResponse(
        published=True,
        version=version,
        routes=check.audit.routes,
        audit=check.audit,
        paths=check.export.paths,
    )


@app.get("/active", response_model=ActiveSnapshotResponse, tags=["active-config"])
def read_active() -> ActiveSnapshotResponse:
    """读取活动版本：路由、审计结果与 OpenAPI paths 来自同一快照。"""
    snapshot = get_store().current()
    return ActiveSnapshotResponse(
        version=int(snapshot["version"]),
        routes=snapshot["audit"]["routes"],
        audit=snapshot["audit"],
        paths=snapshot["paths"],
    )
