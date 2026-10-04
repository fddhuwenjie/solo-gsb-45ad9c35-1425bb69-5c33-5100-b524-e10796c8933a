"""FastAPI 应用入口。"""

from __future__ import annotations

from fastapi import FastAPI

from .exporter import export_openapi_fragment
from .models import (
    AnalyzeRequest,
    AnalyzeResponse,
    ExportOpenApiRequest,
    ExportOpenApiResponse,
    RenderPathsRequest,
    RenderPathsResponse,
)
from .render import render_paths
from .service import analyze_routes

app = FastAPI(
    title="HTTP 路由覆盖冲突审计 API",
    description=(
        "按注册顺序接收路由模板，判定同方法路由的可达集合是否相交、谁遮蔽谁，"
        "并构造最短稳定的示例路径；也可把路由配置导出为可嵌入 OpenAPI 3.1 的 "
        "paths 片段，或按模板与参数值反向生成可调用的 canonical path。"
    ),
    version="1.0.0",
)


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
