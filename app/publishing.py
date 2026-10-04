"""活动版本发布校验：对同一候选集同时运行冲突审计与 OpenAPI 导出。

任一侧报告解析错误、导出侧报告导出路径重复、或审计结果含 proven 的
``full_shadow`` 时，候选集都不可发布；所有拒绝原因都携带在请求数组中的
原始下标，活动版本保持不变。
"""

from __future__ import annotations

from dataclasses import dataclass

from .exporter import DUPLICATE_ROUTE_ERROR_PREFIX, export_openapi_fragment
from .models import (
    AnalyzeResponse,
    Conflict,
    ExportOpenApiResponse,
    ParseError,
    RouteIn,
)
from .service import analyze_routes


@dataclass(frozen=True)
class CandidateCheck:
    """候选集的一次性校验结果。"""

    audit: AnalyzeResponse
    export: ExportOpenApiResponse
    parse_errors: list[ParseError]
    duplicate_routes: list[ParseError]
    full_shadows: list[Conflict]

    @property
    def accepted(self) -> bool:
        return not (self.parse_errors or self.duplicate_routes or self.full_shadows)


def validate_candidate(routes: list[RouteIn]) -> CandidateCheck:
    """对同一候选集运行现有冲突审计与 OpenAPI 导出并汇总拒绝原因。

    两侧对同一下标可能各自报告解析错误（例如不受支持的 HTTP 方法），
    合并后按下标去重并稳定排序，下标一致时保留先出现的那条。
    """
    audit = analyze_routes(routes)
    export = export_openapi_fragment(routes)

    parse_errors: list[ParseError] = []
    duplicate_routes: list[ParseError] = []
    for err in [*audit.parse_errors, *export.parse_errors]:
        if err.error.startswith(DUPLICATE_ROUTE_ERROR_PREFIX):
            duplicate_routes.append(err)
        else:
            parse_errors.append(err)

    deduped: dict[int, ParseError] = {}
    for err in parse_errors:
        deduped.setdefault(err.index, err)
    parse_errors = [deduped[index] for index in sorted(deduped)]
    duplicate_routes.sort(key=lambda e: e.index)

    full_shadows = [
        conflict
        for conflict in audit.conflicts
        if conflict.status == "proven" and conflict.kind == "full_shadow"
    ]

    return CandidateCheck(
        audit=audit,
        export=export,
        parse_errors=parse_errors,
        duplicate_routes=duplicate_routes,
        full_shadows=full_shadows,
    )
