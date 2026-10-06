# API 与图表

启动后访问 <http://127.0.0.1:8000/docs> 查看完整请求模型。

## 接口

| 接口 | 用途 |
| --- | --- |
| `POST /healthz` | 本地服务健康检查，不检查模型或数据库连接 |
| `POST /datasources/test` | 测试数据库连接 |
| `POST /datasources` | 注册数据源并尝试语义初始化 |
| `GET /datasources/{id}` | 数据源元信息 |
| `POST /datasources/{id}/test` | 测试已注册的数据源连接 |
| `POST /datasources/{id}/schema/refresh` | 刷新 Schema 缓存 |
| `GET /datasources/{id}/semantic-config/schema` | 读取语义配置 |
| `POST /datasources/{id}/semantic-config` | 保存业务描述和别名 |
| `POST /datasources/{id}/semantic-index/refresh` | 更新向量索引 |
| `GET /datasources/{id}/semantic-index/status` | 查询语义索引状态 |
| `POST /query` | 问数 |
| `POST /query/explain` | 查询解释 |

```bash
curl -X POST http://127.0.0.1:8000/healthz
```

## 数据源配置

`POST /datasources` 的请求格式如下；数据库账号应只具备必要业务表的读取权限。

```json
{
  "name": "cinema-demo",
  "datasource": {
    "host": "YOUR_DATABASE_HOST",
    "port": 5432,
    "user": "YOUR_READ_ONLY_USER",
    "password": "YOUR_DATABASE_PASSWORD",
    "database": "YOUR_DATABASE",
    "sslmode": "require"
  }
}
```

连接默认使用 `sslmode=require`。网页当前没有 SSL 模式选择；无 SSL 的本地测试数据库可通过 API 显式配置 `sslmode=disable`。`allowed_tables` 可限制用于生成及字段校验的 Schema，实际访问权限仍由数据库角色控制。

注册会尝试建立语义索引；注册成功与索引初始化成功分别检查，具体状态以响应为准。

## 问数

注册数据源得到 `data_source_id` 后，可向 `POST /query` 提交：

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

## 图表配置

设置 `include_chart: true` 后，成功响应中的 `data.chart_config` 是声明式 JSON 对象，前端根据查询结果的 `columns`、`rows` 构建图表。例如：

```json
{
  "version": 1,
  "type": "bar",
  "title": "城市与影院净收入",
  "category": ["city_name", "cinema_name"],
  "series": [
    { "field": "net_revenue", "name": "净收入", "axis": "primary" }
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

## 查询诊断

- `data.business_rewrite`：业务名称、指标、默认口径和查询契约，见 [业务语义配置](business-semantics.md)。
- `data.sql_generation`：候选次数、修复原因及 Schema 刷新状态，见 [查询契约](query-contract.md)。
- `data.chart_selection`：图表意图、选择理由和字段画像。
- `trace_id`：关联请求、候选 SQL 和诊断日志；分享前应脱敏，见 [数据与安全说明](security.md)。

已配置的城市、会员和评分/收入排名分析按实际结果行生成中文说明，最多展示前20行并标明截断；其他问题由模型生成答复。`POST /query/explain` 校验生成的候选，不连接数据库执行 EXPLAIN；实际 `POST /query` 才做数据库预检与查询。
