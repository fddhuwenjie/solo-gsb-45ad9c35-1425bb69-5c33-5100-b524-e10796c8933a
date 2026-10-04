# HTTP 路由覆盖冲突审计 API

用 **Python + FastAPI + Pydantic** 实现的路由覆盖冲突审计服务。

输入一组**按注册顺序**排列的 HTTP 路由（方法、路径模板、优先级），服务在统一的
URL 解码与规范化模型上模拟路由器匹配，判定：

- 同方法的两条路由**可达集合是否相交**；
- 谁会**遮蔽（shadow）**谁（优先级高者胜，同级时注册更早者胜）；
- 为每个已证实冲突构造一条**最短且稳定的示例路径（witness）**；
- 参数约束理论相交、但交集内无法构造见证时，标记为 **`unprovable`（不可证实）**；
- 列出**解析错误**（不中断整批分析）与**建议注册顺序**（更具体的模板排前面）。

另外提供两个配套端点：

- `POST /export-openapi`：把同一套路由配置转换为**可嵌入 OpenAPI 3.1 的
  `paths` 片段**（参数约束映射为 JSON Schema，末尾 `**` 映射为 `wildcard` 参数，
  自动生成稳定唯一的 `operationId`），不返回冲突统计；
- `POST /render-paths`：**反向生成**——提交带 `id` 的路由模板与参数值，校验
  `int` / `uuid` / `slug` 约束并做 RFC 3986 单段百分号编码，返回每条路由可直接
  调用的 canonical path（末尾 `**` 接收可为空的段数组）。

此外提供**活动配置发布**能力：

- `POST /publish`：提交按注册顺序排列的路由与**期望版本号**，服务对同一候选集
  同时运行冲突审计与 OpenAPI 导出；解析错误、导出路径重复或存在 `full_shadow`
  一律拒绝（携带原始下标），通过后**原子替换**活动路由并把版本号加一。期望版本
  过期返回 **409**，相同期望版本的两个并发发布只有一个成功（乐观并发控制）。
- `GET /active`：读取当前活动版本，路由、审计结果与 OpenAPI `paths` **来自同一份
  快照**；初始为版本 `0`、路由为空。快照持久化在数据目录，**容器重启后仍可读到
  已发布版本**。

结果按「胜方优先级 → 方法 → 胜方规范化路径 → 原始下标」稳定排序。

---

## 一条命令启动

```bash
docker compose up --build
```

启动后：

| 用途 | 地址 |
| --- | --- |
| **API 基础地址 / 端口** | **http://localhost:8000** |
| 健康检查 | http://localhost:8000/healthz |
| 交互式文档（Swagger UI） | http://localhost:8000/docs |
| OpenAPI 模式 | http://localhost:8000/openapi.json |

> 容器内监听 `0.0.0.0:8000`，通过 Compose 映射到宿主机的 **8000** 端口。
> 如端口被占用，修改 `docker-compose.yml` 中 `"8000:8000"` 的左侧值即可。

停止：另开终端执行 `docker compose down`（或在启动终端按 `Ctrl+C`）。

### 不用 Docker 的本地运行方式

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## 快速试用

```bash
curl -s -X POST http://localhost:8000/analyze \
  -H 'Content-Type: application/json' \
  -d '{
    "routes": [
      {"method": "GET", "path": "/api/**",                "priority": 0,  "id": "catchall"},
      {"method": "GET", "path": "/api/users/{id:int}",    "priority": 5,  "id": "user"},
      {"method": "GET", "path": "/api/users/me",          "priority": 10, "id": "me"}
    ]
  }'
```

响应（节选）：

```json
{
  "conflicts": [
    {
      "winner_id": "me", "loser_id": "catchall",
      "kind": "partial_overlap", "status": "proven",
      "witness_path": "/api/users/me"
    },
    {
      "winner_id": "user", "loser_id": "catchall",
      "kind": "partial_overlap", "status": "proven",
      "witness_path": "/api/users/1"
    }
  ]
}
```

### 反向生成可调用 URL

```bash
curl -s -X POST http://localhost:8000/render-paths \
  -H 'Content-Type: application/json' \
  -d '{
    "routes": [
      {"id": "user-posts",
       "template": "/users/{id:int}/posts/{slug:slug}",
       "parameters": {"id": 42, "slug": "hello-world"}},
      {"id": "assets",
       "template": "/assets/**",
       "wildcard": ["img/lo go", "café.png"]},
      {"id": "bad",
       "template": "/users/{id:int}",
       "parameters": {"id": "abc"}}
    ]
  }'
```

响应（结果与输入等长、同序；单项失败不中止批处理）：

```json
{
  "results": [
    {"index": 0, "id": "user-posts",
     "template": "/users/{id:int}/posts/{slug:slug}",
     "normalized_template": "/users/{id:int}/posts/{slug:slug}",
     "canonical_path": "/users/42/posts/hello-world", "errors": []},
    {"index": 1, "id": "assets",
     "template": "/assets/**", "normalized_template": "/assets/**",
     "canonical_path": "/assets/img%2Flo%20go/caf%C3%A9.png", "errors": []},
    {"index": 2, "id": "bad",
     "template": "/users/{id:int}", "normalized_template": "/users/{id:int}",
     "canonical_path": null,
     "errors": [
       {"code": "invalid_parameter", "field": "id",
        "message": "value for 'id' does not satisfy int constraint"}
     ]}
  ]
}
```

---

## 发布与读取活动版本

`/analyze`、`/export-openapi`、`/render-paths` 都是**无状态**的显式输入接口，
行为保持不变；活动配置由独立的 `/publish` 与 `/active` 管理。

### 读取活动版本

```bash
curl -s http://localhost:8000/active
```

初始（从未发布过）时返回版本 `0`、路由为空，`audit` 与 `paths` 是该空集的
同一快照产物：

```json
{
  "version": 0,
  "routes": [],
  "audit": {"routes": [], "parse_errors": [], "conflicts": [],
            "suggested_order": [],
            "summary": {"total": 0, "valid": 0, "parse_errors": 0,
                        "conflicts": 0, "full_shadows": 0, "duplicates": 0,
                        "partial_overlaps": 0, "unprovable": 0,
                        "shadowed_routes": 0}},
  "paths": {}
}
```

`routes`、`audit`、`paths` 三者**来自同一份快照**，不会出现旧路由搭配新导出
片段的拼接结果。

### 发布新版本

发布请求提交**按注册顺序**排列的路由与所基于的当前版本（乐观锁）：

```bash
curl -s -X POST http://localhost:8000/publish \
  -H 'Content-Type: application/json' \
  -d '{
    "expected_version": 0,
    "routes": [
      {"method": "GET", "path": "/users/{id:int}", "id": "get-user", "priority": 0},
      {"method": "GET", "path": "/users/me",       "id": "get-me",  "priority": 0},
      {"method": "POST", "path": "/users",         "id": "create-user"}
    ]
  }'
```

校验通过且期望版本等于当前版本时，**原子替换活动路由、版本加一**，响应回传新版本
的路由视图、审计结果与 OpenAPI `paths`：

```json
{
  "published": true,
  "version": 1,
  "detail": null,
  "routes": [ {"id": "get-user", "...": "..."}, {"id": "get-me", "...": "..."},
              {"id": "create-user", "...": "..."} ],
  "audit": { "...": "与 /analyze 同构的完整审计结果..." },
  "paths": {
    "/users":      {"post": {"operationId": "create-user", "...": "..."}},
    "/users/{id}": {"get":  {"operationId": "get-user", "...": "..."}},
    "/users/me":   {"get":  {"operationId": "get-me", "...": "..."}}
  },
  "parse_errors": null,
  "duplicate_routes": null,
  "full_shadows": null
}
```

之后再读 `GET /active` 即得到版本 `1` 的同一份快照。

### 校验失败：返回原始下标，活动版本不变

服务对候选集**同时**运行现有冲突审计与 OpenAPI 导出，任一情况出现都拒绝发布
（HTTP 仍为 200，`published=false`），并按类别返回请求数组中的**原始下标**：

| 拒绝字段 | 含义 |
| --- | --- |
| `parse_errors` | 审计或导出侧报告的解析错误（非法模板、不支持的方法、保留参数名等），按下标去重排序 |
| `duplicate_routes` | 导出侧报告的**导出路径重复**（如 `/x/{id:int}` 与 `/x/{id:uuid}` 同为 `/x/{id}`） |
| `full_shadows` | 审计结果中 `status=proven` 的 `full_shadow` 冲突（含胜/负方原始下标与 witness） |

```bash
curl -s -X POST http://localhost:8000/publish \
  -H 'Content-Type: application/json' \
  -d '{
    "expected_version": 1,
    "routes": [
      {"method": "GET", "path": "/x/{id:int}"},
      {"method": "GET", "path": "/x/{id:uuid}"}
    ]
  }'
```

```json
{
  "published": false,
  "version": 1,
  "detail": null,
  "parse_errors": [],
  "duplicate_routes": [
    {"index": 1, "method": "GET", "template": "/x/{id:uuid}",
     "error": "duplicate route: GET /x/{id} already exported from index 0; ..."}
  ],
  "full_shadows": []
}
```

### 版本冲突：相同期望版本只有一个成功

期望版本与当前版本不一致时返回 **409 Conflict** 与当前版本号；**活动版本和已发布
内容不会被旧提交覆盖**。调用方应重新读取 `/active`、按最新版本重试：

```bash
curl -s -i -X POST http://localhost:8000/publish \
  -H 'Content-Type: application/json' \
  -d '{"expected_version": 0, "routes": [{"method": "GET", "path": "/other"}]}'
```

```http
HTTP/1.1 409 Conflict
```

```json
{
  "published": false,
  "version": 1,
  "detail": "expected_version 0 no longer matches current version 1; re-read /active and retry"
}
```

因此两个使用**同一期望版本**的并发发布，恰好一个 `200 published=true` 并把版本
加一，另一个得到 `409` 与新版本号；下一次发布必须以新版本号为期望版本。

### 持久化与数据保留

- 活动快照（版本、路由原始输入、审计结果、OpenAPI `paths`）在发布时整体写入数据
  目录中的 `active-routes.json`（临时文件 + fsync + 原子替换），容器重启后自动
  从该文件恢复；
- `docker compose up --build` 使用命名卷 `route-audit-data` 挂载到容器内
  `/data`，**重建容器后数据保留**；
- 非 Docker 运行时可用环境变量 `ROUTE_AUDIT_DATA_DIR` 指定数据目录（默认为
  `/data`），例如：

  ```bash
  ROUTE_AUDIT_DATA_DIR=./data uvicorn app.main:app --host 0.0.0.0 --port 8000
  ```

---


模板必须以 `/` 开头，由 `/` 分隔的段构成：

| 段形式 | 含义 | 示例 |
| --- | --- | --- |
| 静态段 | 字面文本，注册时做规范化 | `/users/profile` |
| `{name}` | 通用参数，匹配任意**非空、不含 `/`** 的单段 | `/files/{name}` |
| `{name:int}` | 非负整数：`0` 或 `[1-9][0-9]*`（无前导零） | `/users/{id:int}` |
| `{name:uuid}` | 8-4-4-4-12 十六进制（大小写均可） | `/users/{id:uuid}` |
| `{name:slug}` | 小写字母数字与中划线：`[a-z0-9]+(-[a-z0-9]+)*` | `/posts/{slug:slug}` |
| 末尾 `**` | 只能作为**最后一段**，匹配**零个或多个**后续段 | `/assets/**`、`/**` |

规则细节：

- 参数名必须是 `[A-Za-z_][A-Za-z0-9_]*`，同一模板内不可重复。
- 花括号必须包裹**整段**：`/a/{id:int}` 合法，`/a/pre{id}` 非法。
- `**` 只能出现在末尾；单独的 `*` 不支持。
- 内部空段（`//`）非法；尾随斜杠会被规范化移除（产生 warning）。
- `int` 与 `uuid` 不相交；二者都是 `slug` 的真子集；`slug` 是通用参数的真子集。

### 通配 `**` 的语义

`**` 匹配**零个或多个**段，因此：

- `/a/**` 同时覆盖 `/a`、`/a/b`、`/a/b/c`；
- `/a/**` 与根 `/` **不**相交（前缀 `/a` 无法越过）；
- `/a/**` 与 `/a/b/**` 在 `/a/b` 处相交；
- `/**` 覆盖一切路径（含根）。

---

## URL 解码与规范化规则（审计模型）

审计器在一个**明确定义、幂等**的规范化路径上判定匹配，模板与请求使用同一套规则：

1. **安全百分号解码**
   - 非保留字符（RFC 3986 unreserved：`A-Z a-z 0-9 - . _ ~`）被解码：
     `/%41` → `/A`，`/%7e` → `/~`；
   - **保留字符不解码**：`%2F` 不会变成段分隔符（`/a%2Fb` 与 `/a/b` 不相交），
     `%3F`、`%23` 等同理；十六进制位统一为大写；
   - 非法转义（`%zz`、`%2`、孤立的 `%`）与控制字符（含 `%00`、`%7F`）→ 解析错误。
2. **点段移除**：按 RFC 3986 §5.2.4 处理 `.` / `..`；`..` 不可越过根
   （`/../a` → `/a`）。因此 `/x/./y`、`/z/%2e/w` 会与去点后的模板撞车。
3. **无尾随斜杠语义**：尾随 `/` 被移除（`/a/b/` ≡ `/a/b`）；根路径记为零段。
4. **内部空段保留**：`/a//b` 的空段保留在规范化结果中，静态段和参数段
   （参数至少匹配一个非空字符）都无法匹配它，只有 `**` 能吸收。

构造出的 witness 会经过一次**完整的再规范化往返校验**：重新规范化后必须仍同时
命中两条路由，否则降级为 `unprovable`。

---

## API 参考

### `POST /analyze`

请求体：

```json
{
  "routes": [
    {
      "method": "GET",
      "path": "/users/{id:int}",
      "priority": 0,
      "id": "optional-caller-id"
    }
  ]
}
```

- `routes` **按注册顺序**排列（数组顺序即注册顺序）。
- 字段也接受别名 `template`（等价于 `path`）。
- `method`：大小写不敏感，允许
  `GET POST PUT PATCH DELETE HEAD OPTIONS TRACE CONNECT`；其它方法记入 `parse_errors`。
- `priority`：整数，**数值越大越先匹配**；缺省 `0`；相同优先级按注册顺序决胜。
- `id`：可选；缺省时生成 `route-<数组下标>`。未知字段会被忽略。
- 信封级别校验失败（如缺少 `path`）返回 **422**；单条路由的问题不影响其它路由，
  统一以 **200 + `parse_errors`** 返回。

响应字段：

| 字段 | 说明 |
| --- | --- |
| `routes[]` | 所有有效路由：规范化后的模板、固定段数、是否含通配、规范化 warnings |
| `parse_errors[]` | 解析失败的路由：输入下标、方法/模板、错误原因 |
| `conflicts[]` | 成对冲突报告（见下） |
| `suggested_order[]` | 按方法分组的建议注册顺序及每条路由的移动量 |
| `summary` | 总数、有效数、冲突数、完全遮蔽/重复/部分相交计数、不可证实数、被遮蔽路由数 |

`conflicts[]` 每项：

| 字段 | 说明 |
| --- | --- |
| `winner_*` / `loser_*` | 胜方与负方的下标、id、模板、优先级 |
| `kind` | `duplicate`（可达集合完全相同）、`full_shadow`（负方被胜方完全遮蔽）、`partial_overlap`（仅部分相交） |
| `status` | `proven`（已构造 witness）或 `unprovable`（约束相交但无法构造见证） |
| `witness_path` | 同时命中双方的**最短稳定**示例路径；`unprovable` 时为 `null` |
| `reason` | `unprovable` 时的原因 |

### 遮蔽决胜规则

1. `priority` 高者胜；
2. 优先级相同时，注册顺序更早者胜。

注意：胜方只代表“谁会先抢走这个请求”。`full_shadow`/`duplicate` 表示负方**永远
没有机会**被命中；`partial_overlap` 表示双方各有独占路径，仅在 witness 处撞车。

### 建议顺序

`suggested_order` 不改变优先级语义，只给出“若希望更具体模板先生效”的稳定排列：

1. 不含 `**` 的优先；
2. 固定段更长的优先；
3. 静态段更多、且静态/参数区分出现在更靠前位置的优先；
4. 以上都相同时保持原注册顺序。

`move = 原位置 − 新位置`：正数表示前移，负数表示后移，`0` 表示不变。

### `GET /healthz`

返回 `{"status": "ok"}`，供容器健康检查使用。

### `POST /publish`

把候选路由集校验后**原子发布**为新的活动版本。请求体在 `/analyze` 的基础上增加
`expected_version`（非负整数，乐观锁）：

```json
{
  "expected_version": 0,
  "routes": [
    {"method": "GET", "path": "/users/{id:int}", "priority": 0, "id": "get-user"}
  ]
}
```

处理规则：

1. 对同一 `routes` 同时运行冲突审计（同 `/analyze`）与 OpenAPI 导出
   （同 `/export-openapi`）；
2. 任一侧存在解析错误、导出侧存在导出路径重复、或审计存在 proven 的
   `full_shadow` 时，返回 **200** 且 `published=false`，并在 `parse_errors`、
   `duplicate_routes`、`full_shadows` 中分别给出**原始下标**；活动版本不变；
3. 校验通过但 `expected_version` 不等于当前版本：返回 **409**，响应体
   `published=false`、`version` 为当前版本、`detail` 说明原因；
4. 校验通过且版本匹配：返回 **200**，`published=true`、`version` 为递增后的
   新版本，并回传该版本的 `routes`（审计视图）、`audit` 与 `paths`。

注意：`duplicate`（两条路由可达集合完全相同）不属于发布拒绝条件；只有
`full_shadow`（存在永远无法命中的负方）才拒绝。信封级校验失败（缺少字段、
`expected_version` 为负数等）返回 **422**。

### `GET /active`

读取当前活动版本。响应：

| 字段 | 说明 |
| --- | --- |
| `version` | 当前活动版本号；初始为 `0`，每次成功发布加一 |
| `routes` | 活动路由的审计视图（与 `/analyze` 响应中的 `routes[]` 同构） |
| `audit` | 与 `routes` 同一快照的完整审计结果（同 `/analyze` 响应） |
| `paths` | 与 `routes` 同一快照的 OpenAPI 3.1 `paths` 片段 |

三个字段保证来自同一份不可变快照。服务首次启动（数据目录中无快照文件）时为
版本 `0`、路由为空；之后从持久化文件恢复，容器重启不丢失。

### `POST /export-openapi`

把同一套路由配置转换为**可嵌入 OpenAPI 3.1 文档的 `paths` 片段**。只做结构转换，
不返回任何冲突统计（冲突审计请用 `POST /analyze`）。

请求体与 `/analyze` 相同：

```json
{
  "routes": [
    {"method": "GET", "path": "/users/{id:int}", "id": "getUser"},
    {"method": "GET", "path": "/assets/**"}
  ]
}
```

响应（`paths` 可整体拷入 OpenAPI 3.1 文档的 `paths:` 下；每个操作附带最小
`responses` 占位，保证片段本身合法）：

```json
{
  "paths": {
    "/assets/{wildcard}": {
      "get": {
        "operationId": "get_assets_wildcard",
        "parameters": [
          {"name": "wildcard", "in": "path", "required": true,
           "schema": {"type": "string"}, "x-route-wildcard": true}
        ],
        "responses": {"default": {"description": "Default response"}}
      }
    },
    "/users/{id}": {
      "get": {
        "operationId": "getUser",
        "parameters": [
          {"name": "id", "in": "path", "required": true,
           "schema": {"type": "integer", "minimum": 0}}
        ],
        "responses": {"default": {"description": "Default response"}}
      }
    }
  },
  "parse_errors": []
}
```

#### 转换规则

| 模板段 | OpenAPI 表示 |
| --- | --- |
| 静态段 | 按规范化结果原样保留 |
| `{name}` | path 参数 `name`，schema `{"type": "string", "minLength": 1}` |
| `{name:int}` | path 参数 `name`，schema `{"type": "integer", "minimum": 0}` |
| `{name:uuid}` | path 参数 `name`，schema `{"type": "string", "format": "uuid"}` |
| `{name:slug}` | path 参数 `name`，schema `{"type": "string", "pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$"}` |
| 末尾 `**` | path 参数 `wildcard`（`{"type": "string"}`），附 `"x-route-wildcard": true` |

- 路径模板与参数使用与审计相同的规范化结果（点段、尾随斜杠、百分号转义先被规范化）。
- 约束不进入路径模板，只体现在参数 schema 上；因此 `/x/{id:int}` 与 `/x/{id:uuid}`
  会导出为同一路径 `/x/{id}`。
- 模板内若已有名为 `wildcard` 的参数，末尾 `**` 无法再映射为同名参数，该路由记入
  `parse_errors`。

#### operationId 规则

1. **优先使用显式 `id`**；
2. 缺少 `id` 时由**方法 + 规范化路径**生成，如 `GET /users/{id}` → `get_users_id`、
   `GET /` → `get_root`、`GET /assets/**` → `get_assets_wildcard`；
3. 重名时追加**重复序号**（`-2`、`-3` …）；序号在「规范化路径 → 方法」排序后的
   稳定顺序上分配，与输入顺序无关，保证结果稳定且唯一。

#### 重复项处理

同方法且导出路径相同的重复项**不会静默覆盖**：保留先注册者，后续重复项记入
`parse_errors`，错误信息带双方原始下标。不同方法共用同一路径是合法的，会合并到
同一个 path 项下。

片段按键排序：先按规范化路径，同路径内按方法名排序。

### `POST /render-paths`

按路由模板**反向生成**可调用 URL。提交一批带参路由，服务逐条解析规范化模板、
校验参数约束并做百分号编码，返回每条路由的 canonical path。**单项失败不中止
批处理**：每条结果独立携带 `errors`；响应始终 200，`results` 与输入等长、同序。

请求体：

```json
{
  "routes": [
    {
      "id": "optional-caller-id",
      "template": "/users/{id:int}/posts/{slug:slug}",
      "parameters": {"id": 42, "slug": "hello-world"},
      "wildcard": null
    }
  ]
}
```

| 字段 | 说明 |
| --- | --- |
| `id` | 可选标识；在响应中原样回填，便于对齐结果（缺失为 `null`） |
| `template` | 路径模板（也接受别名 `path`）；语法与 `/analyze` 完全一致 |
| `parameters` | 参数名 → 值的对象；值为字符串，或可精确表示为整数的 JSON 数字 |
| `wildcard` | 末尾 `**` 的段数组，**可为空数组或缺省**（匹配零段）；模板不含 `**` 时只允许空/缺省 |

- 参数值按**模板声明**提供：缺少按模板参数顺序逐项报 `missing_parameter`，
  额外的按键名排序逐项报 `extra_parameter`。
- 取值约束与模板类型一致：`int`（`0` 或无前导零的正整数）、`uuid`（8-4-4-4-12
  十六进制）、`slug`（小写字母数字与中划线）、`{name}`（任意非空单段）；
  不合规逐项报 `invalid_parameter`。布尔、浮点、`null`、数组、对象都不是合法段值。
- 空段、ASCII 控制字符、字面 `.` / `..` 段（会被点段规范化吞掉）一律拒绝。
- 通配数组的逐段规则与参数相同，非法段报 `invalid_wildcard`（`field` 为
  `wildcard[<下标>]`）；非通配模板提供非空数组报 `wildcard_not_allowed`。
- 模板本身解析失败（非法语法、非法百分号转义等）报 `template_invalid`，该条
  `canonical_path` 与 `normalized_template` 为 `null`。
- 任一条目存在错误时，该条 `canonical_path` 为 `null`，但不影响其它条目。
- 信封级校验失败（如缺少 `template`）返回 **422**，与 `/analyze` 一致。

#### 百分号编码规则

每个参数值按 **RFC 3986 单段**编码（等价于对段做 `quote(value, safe="")`）：

- 只有 unreserved 字符（`A-Z a-z 0-9 - . _ ~`）保持原样，其余全部转义；
- 因此值中的 `/` 变为 `%2F`、空格变为 `%20`、`%` 变为 `%25`、`?` 变为 `%3F`，
  一个参数**永远只占一个路径段**；
- 非 ASCII 字符按 UTF-8 字节转义（`café` → `caf%C3%A9`）；
- 末尾 `**` 的每个数组元素独立做同样的单段编码后逐段拼上；
- 静态段使用与审计相同的规范化字面文本（不解码保留字符），生成结果会经过一次
  完整的规范化往返校验，确保返回的是稳定 canonical path。

`results[]` 每项：

| 字段 | 说明 |
| --- | --- |
| `index` | 在输入数组中的原始下标 |
| `id` | 原样回填的调用方标识（可能为 `null`） |
| `template` / `normalized_template` | 原始模板 / 解析规范化后的模板（模板非法时后者为 `null`） |
| `canonical_path` | 反向生成的可调用路径；有任何错误时为 `null` |
| `errors[]` | 逐项错误：`code`、`field`（参数名或 `wildcard[i]`，模板级为 `null`）、`message` |

错误码一览：`template_invalid`、`missing_parameter`、`extra_parameter`、
`invalid_parameter`、`invalid_wildcard`、`wildcard_not_allowed`、`not_canonical`。

---

## “不可证实”（unprovable）是什么意思？

常规情况下，两个参数约束只要相交，审计器都能从交集里挑出一个固定代表值作为
witness（例如 `int × slug` → `1`，`uuid × slug` → 全零 UUID）。

但可达集合是按“完整规范化后的请求路径”定义的。如果两个约束**在理论上相交**，
交集里却取不到任何能通过完整规范化往返校验、或落在交集内的代表值，审计器不会
编造一个假路径，而是如实返回：

```json
{
  "kind": "partial_overlap",
  "status": "unprovable",
  "witness_path": null,
  "reason": "parameter constraints intersect at segment N but no constructible witness exists in their intersection"
}
```

内置的 `int / uuid / slug / 通用` 类型之间不存在这种情况（交集非空则必有 witness）；
该状态主要为通过 `app.constraints.register_constraint` 注册的扩展约束保留
（仓库测试 `tests/test_unprovable.py` 演示了这一路径）。

---

## 项目结构

```
app/
  normalization.py   # URL 百分号解码、RFC 3986 点段移除、尾随斜杠/空段规则
  constraints.py     # int/uuid/slug/通用约束、交集与包含层级、扩展点
  templates.py       # 模板词法/语法解析与规范化
  analyzer.py        # 可达集合相交、覆盖关系、最短 witness、往返校验
  service.py         # 编排：解析分组、成对判定、稳定排序、建议顺序、汇总
  exporter.py        # 路由配置 -> OpenAPI 3.1 paths 片段（参数 schema、operationId）
  publishing.py      # 发布校验：同一候选集的审计 + 导出汇总（解析错误/重复/full_shadow）
  store.py           # 活动版本快照存储：CAS 并发控制、原子落盘、重启恢复
  render.py          # 按模板与参数值反向生成 canonical path（约束校验、单段编码）
  models.py          # Pydantic 请求/响应模型
  main.py            # FastAPI 路由：/analyze、/export-openapi、/render-paths、/healthz、/publish、/active
tests/               # pytest 测试（规范化、模板、相交、API、导出、反向渲染、unprovable、发布/并发/持久化）
Dockerfile
docker-compose.yml
requirements.txt / requirements-dev.txt
```

## 运行测试

```bash
pip install -r requirements-dev.txt
pytest -q
```

## 典型冲突示例

| 路由 A | 路由 B | 结论 | witness |
| --- | --- | --- | --- |
| `/users/{id:int}` | `/users/1` | 部分相交（静态更具体） | `/users/1` |
| `/users/{id:int}` | `/users/me` | 不相交 | — |
| `/u/{id:int}` | `/u/{id:uuid}` | 不相交（约束互斥） | — |
| `/f/{s:slug}` | `/f/{i:int}` | int 被 slug 完全覆盖 | `/f/1` |
| `/static/**` | `/static/a/b` | 静态子路径被通配完全遮蔽 | `/static/a/b` |
| `/static/**` | `/` | 不相交（前缀不同） | — |
| `/a/{x}/c` | `/a/b/{y}` | 部分相交，互不为超集 | `/a/b/c` |
| `/x/./y` | `/x/y` | 规范化后重复 | `/x/y` |
| `/a%2Fb` | `/a/b` | 不相交（`%2F` 不是分隔符） | — |
| `/%41` | `/A` | 规范化后重复 | `/A` |
