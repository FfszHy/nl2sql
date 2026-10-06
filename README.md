# NL2SQL · 智能问数

用自然语言查询 PostgreSQL 数据库的本地工具：读取数据库结构，结合业务语义和向量检索生成 SQL，再展示查询结果与中文答复。

A local natural-language query tool for PostgreSQL, with a Python/FastAPI backend and a rebuildable React web UI.

**发布范围：后端、示例数据脚本和 React/Vite 网页前端均提供源码，前端包含依赖锁文件和构建配置。已在 Node.js 24 环境完成依赖安装与前端构建验证。** 本项目面向本地开发与演示，通过浏览器使用。

## 功能

- 注册 PostgreSQL 数据源，缓存表结构、字段注释及真实主键、唯一键和外键；区分配置关联与数据库确认的关联。
- 配置表及字段的业务描述、别名，建立语义向量索引并检索相关表。
- SQL 生成前执行业务语义改写：核对业务名称对应的实际数据库取值，附上配置的指标公式，保留原问题与默认口径说明。
- 将已解析的指标、实体、过滤和分析阶段编译为统一查询契约，检查指标来源、可证明的关联粒度和已配置的并列排名条件。
- 根据问题与 Schema 生成单条只读 SELECT，支持只读 WITH/CTE 和窗口函数，并应用结果行数上限。
- 使用数据库只读事务执行生成的查询，固定已核对模式的 `search_path`，设置 10 秒连接超时、30 秒语句超时和 5 秒锁等待超时。
- 返回 SQL、表格、中文答复和解释；根据分析意图、实际结果字段及指标单位在本地选择柱状图、折线图、饼图或散点图，输出受限 JSON 配置和选择理由。
- 左侧历史支持新建、切换和通过三点菜单删除对话；历史保存在当前浏览器，删除后刷新仍生效。
- 在执行前核对真实表、字段和别名，并用 PostgreSQL `EXPLAIN` 预检类型与分组语法；可修复的候选错误默认最多生成五次，附带八张关联电影业务表的合成数据生成脚本。

查询流程：`问题 → 业务语义改写和查询契约 → 相关表检索 → Schema 与业务约束上下文 → 模型生成 SQL → 只读、字段、指标来源和关联粒度校验 → PostgreSQL 预检和查询 → 根据结果生成答复及本地图表选型`。失败按阶段分类，在有限次数内重新生成并重新校验。

## 业务语义改写

业务人员可以直接问“App、网站、自助机和柜台，哪个渠道收入最多？比较购票人数、平均每单实收和退款金额占销售额的比例”。后端保留这段原文，将业务名称和指标解释成可核对的数据库约束，再交给 SQL 模型；最终中文答复仍针对原问题生成。这一步使用配置规则，不额外调用一个模型猜测改写。

规则位于 [config/business_semantics.json](config/business_semantics.json)。每个业务 profile 通过 `required_columns` 匹配数据库结构，也可用 `data_source_ids` 限定已注册数据源。内置 profile 面向仓库的八表影院示例，不会仅因其他库存在 `sales_channel` 字段就套用影院规则。其他业务数据库需要配置自己的名称和口径。

影院示例中，网站、App、自助机和柜台分别对应 `web`、`app`、`kiosk`、`counter`。命中名称映射时，后端使用只读事务核对相应字段的实际 DISTINCT 值，每个命中字段最多读取 51 个值；无法确认映射或字段缺失时返回业务语义错误。表及字段已有的业务说明和别名也会进入 SQL 上下文。

| 指标 | 内置影院口径 |
| --- | --- |
| 购票人数 | 按订单顾客去重，`COUNT(DISTINCT customer_id)` |
| 订单票数 | `SUM(ticket_count)`，与人数及场次售票数区分 |
| 净收款 | `SUM(total_amount - refund_amount)`，包含部分退款 |
| 平均每单实收 | `AVG(total_amount - refund_amount)`，按订单粒度计算 |
| 退款金额占销售额比例 | `100.0 * SUM(refund_amount) / NULLIF(SUM(total_amount), 0)` |
| 销售额 | `SUM(total_amount)`，未扣退款 |
| 观众平均评分 | `AVG(movie_reviews.rating)`，与影片的 `imdb_score` 区分 |
| 观众评价数量 | `COUNT(movie_reviews.review_id)`，按评价记录计数 |

内置影院规则将未明确限定的“收入”默认解释为净收款，并在响应的 `data.business_rewrite.assumptions` 中说明；“销售额”采用退款前金额。这是可修改的业务配置，不是模型推断出的财务标准。示例数据没有成本信息，利润类请求会明确返回无法按现有口径计算。

配置也包含城市、影院、电影类型和会员等级维度，以及示例库的关联键。影院城市沿订单→场次→影院→城市关联，电影类型使用 `movies.genre`；不能用顾客居住城市或电影片名替代。全额退款对应 `refunded`，只有用户明确要求时才排除；部分退款仍保留并扣减退款金额。按购票人数问“喜欢哪类电影”解释为购买行为排名。

`data.business_rewrite` 返回原问题、改写后的约束、业务名称映射、指标定义、维度、关联路径、默认口径和证据来源，并包含版本化的 `query_contract`。契约记录每个指标的数据来源、公式和粒度，以及真实键证据、过滤和结构化分析要求；配置声明的键不会自动成为数据库唯一性证明。

SQL 校验沿 CTE 和输出列追踪指标，阻止订单金额或评价记录被一对多关联放大；支持已独立聚合后的零值补齐，包括多层相同零默认值。多表 `COUNT(*)` 通过真实完整 PK/UK 和从事实来源出发的关联证明确认计数粒度，不因使用多表就误拒，也不把维度表彼此唯一当作订单不被重复的证据。AVG 的冗余补零仅在能证明输入非空、字段非 NULL 且没有外连接补空时放行，不能把缺失均分自行解释成0。已知字段可空时，不采用可能改变 NULL 语义的 SUM/AVG 等价变换。并列排名检查两个窗口所在的候选层、已声明门槛是否先于排名生效、原始指标排序、稳定同分顺序及后置排名比较。城市与会员排行目前仍以文字约束指导生成，没有完整的结构化排名证明。

生成结构由契约中的事实源、完整实体键和排名阶段推导；修复反馈区分关联放大、计数来源无法证明、指标公式、平均值空缺、排名来源/候选/后置条件，提供对应原因及改写步骤。执行前还按 PostgreSQL 标识符规则引用表、CTE 和派生表别名，避免模型使用保留字别名造成语法错误；已有引用的大小写保持不变。

校验覆盖已配置的明确约束和支持的表达式结构，不能证明任意业务语义、候选集合完整性或所有派生表的唯一性；部分合法的复杂 SQL 会被保守拒绝。实现与已知边界见 [统一查询契约](docs/query-contract.md)。

已配置的城市与影院分层排名、会员与电影类型组内排名、观众评分与收入排名比较，直接按实际结果行生成文字说明，保留顺序和返回的指标值。说明最多展示前 20 行并标明截断；无法明确对应的字段请查看结果表。其他问题仍由模型生成答复。

启用 `retry_on_error` 后，字段、业务口径或 PostgreSQL 语法、类型和表达式错误会带着上一条候选和错误反馈模型，修复时提供全部可见 Schema。连接中断、权限不足、语句超时和锁等待不会要求模型重写 SQL，断线时的回滚失败也不会掩盖原错误。注册数据源出现字段或表不存在的数据库错误时，最多刷新一次 Schema，并重新构建业务契约；原业务口径丢失或变化时明确拒绝，不能绕过业务校验继续查询。多条查询仅在每条均通过只读检查时允许重新生成成一条查询，原候选始终不执行；写操作、危险函数及其他安全拒绝不进入修复。`SQL_GENERATION_MAX_ATTEMPTS` 默认为 5（首次生成加最多四次修复），范围 1–10；关闭重试只生成一次。响应的 `data.sql_generation` 记录生成次数、修复原因和 Schema 刷新状态，不会无限重试。

查询审计默认写入 `data/query-audit.jsonl`，保留 trace_id、UTC 时间、候选 SQL、失败阶段、错误说明和 SQLSTATE；默认单文件 1 MiB，最多三份备份。`QUERY_AUDIT_PATH` 设为空可关闭文件记录；日志配置修改后重启生效。日志排除结果数组及连接凭据对象，但问题、SQL 和数据库诊断仍可能包含业务信息。

## Demo 录屏问题

下面四问对应影院示例库的真实字段和已配置口径，分别展示分类比较、时间趋势、整体构成和数值关系。可以直接用业务人员的口吻输入，按这个顺序录屏。

1. **条形图 · 会员购票偏好**

   普通、银卡、金卡和白金用户，各自最喜欢哪三类电影？按实际购票人数来看，同时看看订单量和扣掉退款后的收入，全额退款的订单不算。用条形图比较各会员等级的购票偏好。

2. **折线图 · 月度收入趋势**

   按下单月份统计扣掉退款后的收入，月份从早到晚排列，用折线图展示月度收入趋势。

3. **饼图 · 渠道收入构成**

   统计全部购票渠道扣掉退款后的收入金额，并用饼图展示渠道收入构成。

4. **散点图 · 高评分影片的收入表现**

   只看至少有20条观众评价的电影，哪些电影的平均评分排进前20，但扣掉退款后的收入没有进前20？把评分、评价数量、收入和两项排名列出来。用散点图观察这些电影评分与收入的关系。

第一问以去重购票人数作为偏好依据，主图比较人数，订单量和净收款保留在结果表；同一顾客可以出现在不同电影类型中。第二问按订单时间汇总，不按电影上映日期。第三问用全部渠道的可加收入展示构成，不筛 Top N，也不把去重人数或均值当成份额。第四问先筛选评价达标影片，再在共同候选集合中计算评分和收入排名；两项排名按原始指标降序，以电影 ID 稳定同分次序。散点横轴是评分、纵轴是净收款，仅描述筛选后的影片。

四问已重新解析业务语义，并用先前经真实 HTTP、数据库和独立基线核对的 SQL 与结果回放字段、业务口径和图表选型；当前样本分别返回12、13、4、19行，选出 bar、line、pie、scatter。本次问法调整没有重新调用外部模型，不能据此保证每次生成都得到同一 SQL 结构；结果结构不适合所请求图型时，系统保留表格和选型理由。验证过程及边界见 [2026-10-07 回归记录](docs/query-regression-2026-10-07.md)，此前的复杂城市、渠道比较及评价门槛变体验证见 [2026-10-06 回归记录](docs/query-regression-2026-10-06.md)。

## 快速开始

需要 Python 3.11、Node.js、一个 PostgreSQL 数据库，以及可用的模型和 embedding API。macOS 可使用 conda 安装脚本；以下使用标准 Python 环境。当前后端依赖版本在 Python 3.11 环境验证，前端在 Node.js 24 环境验证；其他 Node.js 版本未在本次验证。

```bash
git clone https://github.com/FfszHy/nl2sql.git
cd nl2sql
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

编辑 `.env`，填写 `LLM_API_KEY`，需要独立 embedding 凭据时填写 `EMBEDDING_API_KEY`。模板对应 DashScope 原生 API 的请求和响应格式。自定义 API 时还需匹配 `LLM_PAYLOAD_*` 和 `LLM_RESPONSE_CONTENT_PATHS`，仅替换 URL 并不能保证接口兼容。

生成并填写本地 `DATASOURCE_SECRET_KEY`，注册数据源后保持该值稳定：

```bash
python -c 'import secrets; print(secrets.token_hex(32))'
```

启动后端：

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

在第二个终端安装前端依赖并启动开发服务：

```bash
cd nl2sql/frontend
npm ci
npm run dev -- --host 127.0.0.1
```

打开 <http://127.0.0.1:5173>，在「设置」中填写后端地址 `http://127.0.0.1:8000`，注册数据库连接。为查询工具使用仅能读取必要业务表的数据库账号。`PG_SCHEMA` 默认为 `public`；云端 PostgreSQL 连接通常需保留 `sslmode=require`。

也可直接使用现有构建产物，在项目根目录运行：

```bash
python3 -m http.server 5173 --bind 127.0.0.1 --directory frontend/dist
```

静态服务没有开发代理，需在网页设置中填写后端地址。

macOS conda 安装和示例数据库步骤见 [Mac 使用说明](Mac使用说明.md)。

## API

启动后访问 <http://127.0.0.1:8000/docs> 查看完整请求模型。

| 接口 | 用途 |
| --- | --- |
| `POST /healthz` | 本地服务健康检查，不检查模型或数据库连接 |
| `POST /datasources/test` | 测试数据库连接 |
| `POST /datasources` | 注册数据源并尝试语义初始化 |
| `GET /datasources/{id}` | 数据源元信息 |
| `POST /datasources/{id}/schema/refresh` | 刷新 Schema 缓存 |
| `GET /datasources/{id}/semantic-config/schema` | 读取语义配置 |
| `POST /datasources/{id}/semantic-config` | 保存业务描述和别名 |
| `POST /datasources/{id}/semantic-index/refresh` | 更新向量索引 |
| `POST /query` | 问数 |
| `POST /query/explain` | 查询解释 |

```bash
curl -X POST http://127.0.0.1:8000/healthz
```

注册数据源得到 `data_source_id` 后，可提交：

```json
{
  "question": "按类型统计电影数量",
  "data_source_id": "YOUR_REGISTERED_DATASOURCE_ID",
  "options": {
    "max_rows": 200,
    "retry_on_error": true,
    "include_explanation": true,
    "include_chart": false
  }
}
```

设置 `include_chart: true` 后，成功响应中的 `data.chart_config` 是声明式 JSON 对象，前端根据查询结果的 `columns`、`rows` 构建图表。例如：

```json
{
  "version": 1,
  "type": "bar",
  "title": "城市与影院净收入",
  "category": ["city_name", "cinema_name"],
  "series": [
    { "field": "net_revenue", "name": "净收入", "axis": "primary" },
    { "field": "revenue_share", "name": "城市收入占比", "axis": "secondary" }
  ],
  "orientation": "horizontal"
}
```

`category` 包含 1–3 个互不重复的结果列名，多个字段组合为标签；`series` 包含 1–8 个数值列。字段名必须在结果列中唯一出现。饼图、散点图仅支持一个分类字段和一个主轴系列，散点图分类字段表示数值 x。`orientation` 仅用于柱状图。标题可省略，最多 200 个字符；系列名可省略，最多 100 个字符。

选型在本地完成，不再额外调用模型选择图型，也不向选图模型发送结果数据。`data.chart_selection` 返回 `intent`、`reason` 和有界字段 `profile`，审计记录图型与选择原因，不记录画像的实际数值。指标配置可声明 `display_name`、`unit`、`additive`；金额、客单金额、人数、订单、评分和比例分别处理，不擅自换算元/万元或比例刻度。

| 问题与数据条件 | 默认选择 |
| --- | --- |
| 分类比较、组内排名 | 条形图，只绘制主要指标，其他单位指标保留表格 |
| 单一时间序列，时间值唯一且已按时间排列 | 折线图；多组时间序列或无时间顺序时保留表格 |
| 同一结果行中明确选中的两项变化数值 | 散点图；编号、名次及含义不明确的多指标不代替关系变量 |
| 明确问整体构成，简单单事实源、完整互斥分类、少量非负可加数值 | 饼图；Top N、截断、去重人数、均值、比例或无法证明互斥归属的关联不画饼图 |
| 单值、空结果或无法确认适合的结构 | 保留表格及选择理由 |

图表配置或规划异常时，查询仍返回成功结果，`chart_config` 为 `null`，`chart_error` 提供提示；没有适用图型时 `chart_error` 仍为 `null`，原因位于 `chart_selection`。未请求图表时这三项均为 `null`。图型多样性来自分析目的，不按问题轮换图型。

图表接口已从 `echarts_code` 改为 `chart_config`。前后端需同时更新并重启后端；历史对话中的旧 JavaScript 图表不再加载，表格和文字保留，重新查询即可生成新配置。

## 数据与安全边界

- API 当前没有用户鉴权，保持绑定 `127.0.0.1`。CORS 不是身份认证，也不提供数据库权限隔离。
- `.env` 和 `data/` 不提交到仓库。状态库包含连接信息、密码混淆值、Schema、语义配置和向量缓存；请按敏感文件保护。
- 当前数据库密码存储采用可逆 XOR 混淆，**不属于安全加密**。设置独立 secret 不能替代受保护的存储方案；更换 secret 会影响已有数据源密码的读取。
- `allowed_tables` 筛选提供给模型及静态字段校验的 Schema；访问范围仍必须由数据库角色、表权限等控制。
- SQL 校验使用 SQLGlot 的 PostgreSQL 语法树检查主查询和 CTE 的只读结构，并保留保守的关键字、函数、系统库和注释拒绝规则；正常字面量仍可能被误拒绝。语法树检查不保证 SQL 可执行或构成完整安全沙箱，只读事务仍应配合低权限账号与受信任的数据库函数。
- 图表输出只接受大小不超过 16,000 个字符的 JSON，前后端按字段白名单校验；不接受模型 JavaScript、任意 ECharts option、函数或外部资源配置。图表提示使用画布富文本，不将模型或数据库字符串作为 HTML 渲染。前端受控代码绑定数据，保留柱状图/折线图中的空值，饼图拒绝负值。
- 问题、Schema、业务描述和改写中核对的分类取值会发送到配置的模型/embedding 服务；模型答复请求包含 SQL、列名和最多 20 行查询结果。查询日志记录问题及 SQL；被安全、字段或业务检查拒绝的候选 SQL 分别记录为 `sql_validation_failed`、`schema_validation_failed`、`business_validation_failed`，附带请求 trace_id。请使用获准发送的数据。
- 模型可能生成错误 SQL 或误解结果；重要结论应核对 SQL 与数据。当前离线测试不评估自然语言问数准确率。

## 开发与验证

前端构建（在 `frontend/` 中运行）：

```bash
npm ci
npm test
npm run build
```

构建输出到 `frontend/dist/`。前端测试使用 Node.js 内置的 TypeScript 类型擦除，在 Node.js 24 下验证。Vite 仅启动网页，Python 后端需在另一个终端单独运行；网页设置中的后端地址应与实际端口一致。`npm run lint` 当前缺少 ESLint 配置文件。

后端离线检查：

```bash
python -m unittest discover -s tests -v
bash -n setup.sh
```

后端离线测试当前为266项，覆盖 SQL 拒绝规则、别名引用和行数限制、真实字段与 CTE/窗口血缘、真实复合键元数据、查询契约、计数来源及完整关联链、关联放大和可空字段反例、排名阶段、Schema 失效修复、权限错误分类、滚动审计、结果说明、本地图表选型和适用性边界；这些测试不连接真实数据库或模型。实际模型候选保存在 `tests/fixtures/business_sql/`。本轮另有九项真实数据库回归和四项带图表的 HTTP/数据库回放，逐项核对独立合成数据基线，验证范围见 [本轮记录](docs/query-regression-2026-10-07.md)。前端13项测试覆盖数据绑定、空值、恶意配置和旧代码执行入口移除，四种实际查询配置也经前端绑定核对。GitHub Actions 在 Python 3.11 下运行后端检查。

```text
app/              FastAPI 后端源码
config/           业务名称与指标口径配置
scripts/          PostgreSQL 合成示例数据生成脚本
frontend/src/     React 前端源码
frontend/dist/    网页构建产物
tests/            离线回归测试
.env.example      无密钥的配置模板
requirements.txt  Python 依赖
setup.sh          macOS conda 安装脚本
```

## 贡献与许可

提交问题时附上复现步骤、Python/Node.js 版本和脱敏错误信息。不要上传 API key、连接密码、`.env`、状态库或真实查询数据。后端修改后运行离线测试；前端修改后运行 `npm run build` 并检查实际页面。

本项目原创代码采用 [MIT 许可证](LICENSE)。现有前端产物包含第三方代码，其授权和已识别组件见 [第三方说明](THIRD_PARTY_NOTICES.md)。
