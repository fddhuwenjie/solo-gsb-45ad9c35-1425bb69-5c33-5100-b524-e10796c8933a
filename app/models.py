"""请求/响应 Pydantic 模型。"""

from __future__ import annotations

from typing import Any, Literal, Optional, get_args

from pydantic import BaseModel, Field

Method = Literal[
    "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT"
]

#: 支持的 HTTP 方法集合（与 ``Method`` 字面量保持一致）。
ALLOWED_METHODS: frozenset[str] = frozenset(get_args(Method))

ConflictKind = Literal["duplicate", "full_shadow", "partial_overlap"]
ConflictStatus = Literal["proven", "unprovable"]


class RouteIn(BaseModel):
    method: str = Field(..., description="HTTP 方法，如 GET / POST（大小写不敏感）")
    path: str = Field(..., alias="template", description="路径模板，如 /users/{id:int}")
    priority: int = Field(
        default=0, description="优先级，数值越大越先匹配；相同优先级按注册顺序"
    )
    id: Optional[str] = Field(default=None, description="可选的调用方提供的路由标识")

    model_config = {"populate_by_name": True, "extra": "ignore"}


class AnalyzeRequest(BaseModel):
    routes: list[RouteIn] = Field(..., description="按注册顺序排列的路由")


class ParseError(BaseModel):
    index: int
    id: Optional[str] = None
    method: Optional[str] = None
    template: Optional[str] = None
    error: str


class RouteOut(BaseModel):
    index: int
    id: str
    method: str
    template: str
    normalized_template: str
    priority: int
    fixed_segments: int
    wildcard: bool
    warnings: list[str] = Field(default_factory=list)


class Conflict(BaseModel):
    method: str
    winner_index: int
    winner_id: str
    loser_index: int
    loser_id: str
    winner_template: str
    loser_template: str
    winner_priority: int
    loser_priority: int
    kind: ConflictKind = Field(
        description="duplicate=可达集合相同；full_shadow=负方被完全遮蔽；"
        "partial_overlap=仅部分相交"
    )
    status: ConflictStatus = Field(
        description="proven=已构造示例路径；unprovable=约束相交但无法构造见证"
    )
    witness_path: Optional[str] = Field(
        default=None, description="同时命中两条路由的最短稳定示例路径"
    )
    reason: Optional[str] = Field(
        default=None, description="unprovable 时的原因说明"
    )


class SuggestedRoute(BaseModel):
    index: int
    id: str
    template: str
    priority: int
    original_position: int = Field(description="在该方法原注册顺序中的位置（从 1 起）")
    suggested_position: int = Field(description="建议顺序中的位置（从 1 起）")
    move: Optional[int] = Field(
        default=None,
        description="相对原位置的移动量（原位置-新位置）；正数表示前移，负数表示后移",
    )


class SuggestedOrder(BaseModel):
    method: str
    order: list[SuggestedRoute]


class Summary(BaseModel):
    total: int
    valid: int
    parse_errors: int
    conflicts: int
    full_shadows: int
    duplicates: int
    partial_overlaps: int
    unprovable: int
    shadowed_routes: int


class AnalyzeResponse(BaseModel):
    routes: list[RouteOut]
    parse_errors: list[ParseError]
    conflicts: list[Conflict]
    suggested_order: list[SuggestedOrder]
    summary: Summary


class ExportOpenApiRequest(BaseModel):
    routes: list[RouteIn] = Field(..., description="按注册顺序排列的路由")


class PublishRoutesRequest(BaseModel):
    routes: list[RouteIn] = Field(..., description="按注册顺序排列的候选路由")
    expected_version: int = Field(
        ...,
        ge=0,
        description="乐观锁：发布所基于的当前活动版本；不一致时拒绝发布",
    )


class PublishRoutesResponse(BaseModel):
    published: bool = Field(description="true=已替换活动版本；false=校验失败或版本冲突")
    version: int = Field(description="发布成功时为新版本号；失败时为当前活动版本")
    detail: Optional[str] = Field(
        default=None, description="版本冲突时的原因；校验失败按下列字段逐项报告"
    )
    routes: Optional[list[RouteOut]] = Field(
        default=None, description="发布成功时，新活动版本的路由审计视图"
    )
    audit: Optional[AnalyzeResponse] = Field(
        default=None, description="发布成功时，新活动版本的冲突审计结果"
    )
    paths: Optional[dict[str, Any]] = Field(
        default=None, description="发布成功时，新活动版本的 OpenAPI 3.1 paths 片段"
    )
    parse_errors: Optional[list[ParseError]] = Field(
        default=None,
        description="校验失败：审计或导出侧报告的解析错误（含原始下标）",
    )
    duplicate_routes: Optional[list[ParseError]] = Field(
        default=None, description="校验失败：导出路径重复的路由（含原始下标）"
    )
    full_shadows: Optional[list[Conflict]] = Field(
        default=None, description="校验失败：审计结果中的 full_shadow 冲突"
    )


class ActiveSnapshotResponse(BaseModel):
    version: int = Field(description="当前活动版本号；初始为 0")
    routes: list[RouteOut] = Field(description="当前活动版本的路由（审计视图）")
    audit: AnalyzeResponse = Field(description="与 routes 同一快照计算的冲突审计结果")
    paths: dict[str, Any] = Field(
        description="与 routes 同一快照导出的 OpenAPI 3.1 paths 片段"
    )

    model_config = {"extra": "ignore"}


class ExportOpenApiResponse(BaseModel):
    paths: dict[str, Any] = Field(
        default_factory=dict,
        description="可直接嵌入 OpenAPI 3.1 文档的 paths 片段（按路径、方法排序）",
    )
    parse_errors: list[ParseError] = Field(
        default_factory=list,
        description="解析失败或被拒绝的重复路由（含原始下标）",
    )


class RenderPathRouteIn(BaseModel):
    id: Optional[str] = Field(
        default=None,
        description="调用方提供的路由标识；在响应中原样回填，便于对齐结果",
    )
    path: str = Field(
        ..., alias="template", description="路径模板，如 /users/{id:int}/items/**"
    )
    parameters: Optional[dict[str, Any]] = Field(
        default=None,
        description="参数名 -> 参数值；JSON 字符串，或可精确表示为整数的数字",
    )
    wildcard: Optional[list[Any]] = Field(
        default=None,
        description="末尾 ** 接收的段数组（可为空/缺省）；模板不含 ** 时禁止提供",
    )

    model_config = {"populate_by_name": True, "extra": "ignore"}


class RenderPathsRequest(BaseModel):
    routes: list[RenderPathRouteIn] = Field(
        ..., description="按调用顺序排列的带参路由，结果按相同顺序返回"
    )


class RenderPathError(BaseModel):
    code: str = Field(
        description=(
            "template_invalid=模板解析失败；missing_parameter=缺少模板参数；"
            "extra_parameter=模板不接受的参数；invalid_parameter=参数类型/取值"
            "不满足约束；invalid_wildcard=通配段取值非法；"
            "wildcard_not_allowed=模板不含末尾 **；not_canonical=生成结果未通过"
            "规范化往返校验"
        )
    )
    field: Optional[str] = Field(
        default=None,
        description="出错字段：参数名或 wildcard[<下标>]；模板级错误时为 null",
    )
    message: str


class RenderPathResult(BaseModel):
    index: int = Field(..., description="在输入数组中的原始下标")
    id: Optional[str] = None
    template: Optional[str] = Field(
        default=None, description="输入的原始模板；信封级字段缺失时为 null"
    )
    normalized_template: Optional[str] = Field(
        default=None, description="规范化后的模板；模板解析失败时为 null"
    )
    canonical_path: Optional[str] = Field(
        default=None,
        description="反向生成的可调用 canonical path；任一项校验失败时为 null",
    )
    errors: list[RenderPathError] = Field(default_factory=list)


class RenderPathsResponse(BaseModel):
    results: list[RenderPathResult] = Field(
        ..., description="与输入 routes 等长、顺序一致的逐条结果"
    )
