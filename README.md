# NL2SQL · 智能问数

用自然语言查询 PostgreSQL 数据库的本地工具：读取数据库结构，结合业务语义和向量检索生成 SQL，再展示查询结果与中文答复。

A local natural-language query tool for PostgreSQL, with a Python/FastAPI backend and a supplied web UI build.

**发布范围：后端和示例数据脚本提供源码；`frontend/dist/` 提供现有网页构建产物。前端源码、依赖锁文件和构建配置暂缺，因此当前版本无法从源码重建前端。** 本项目面向本地开发与演示。

## 功能

- 注册 PostgreSQL 数据源，缓存表结构、字段注释和外键关系。
- 配置表及字段的业务描述、别名，建立语义向量索引并检索相关表。
- 根据问题与 Schema 生成单条 SELECT，应用行数限制和保守的 SQL 检查。
- 使用数据库只读事务执行生成的查询，设置 30 秒语句超时和 5 秒锁等待超时。
- 返回 SQL、表格、中文答复和解释；可请求 ECharts 图表配置。
- 查询失败时可重试一次；附带八张关联电影业务表的合成数据生成脚本。

查询流程：`问题 → 相关表检索 → Schema 上下文 → 模型生成 SQL → 校验 → PostgreSQL → 模型生成答复`。

## 快速开始

需要 Python 3.11、一个 PostgreSQL 数据库，以及可用的模型和 embedding API。macOS 可使用 conda 安装脚本；以下使用标准 Python 环境。当前依赖版本在 Python 3.11 环境验证。

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

在第二个终端启动网页：

```bash
cd nl2sql
python3 -m http.server 5173 --bind 127.0.0.1 --directory frontend/dist
```

打开 <http://127.0.0.1:5173>，在「设置」中填写后端地址 `http://127.0.0.1:8000`，注册数据库连接。为查询工具使用仅能读取必要业务表的数据库账号。`PG_SCHEMA` 默认为 `public`；云端 PostgreSQL 连接通常需保留 `sslmode=require`。

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

## 数据与安全边界

- API 当前没有用户鉴权，保持绑定 `127.0.0.1`。CORS 不是身份认证，也不提供数据库权限隔离。
- `.env` 和 `data/` 不提交到仓库。状态库包含连接信息、密码混淆值、Schema、语义配置和向量缓存；请按敏感文件保护。
- 当前数据库密码存储采用可逆 XOR 混淆，**不属于安全加密**。设置独立 secret 不能替代受保护的存储方案；更换 secret 会影响已有数据源密码的读取。
- `allowed_tables` 只筛选提供给模型的 Schema，**不限制最终 SQL 的数据库访问权限**。访问范围必须由数据库角色、表权限等控制。
- SQL 校验采用保守的正则规则，可能误拒绝正常字面量；它不构成完整 SQL 解析器或安全沙箱。只读事务仍应配合低权限账号与受信任的数据库函数。
- 问题、Schema 和业务描述会发送到配置的模型/embedding 服务；生成答复时还会发送 SQL、列名和最多 20 行查询结果。查询日志记录问题及 SQL。请使用获准发送的数据。
- 模型可能生成错误 SQL 或误解结果；重要结论应核对 SQL 与数据。当前离线测试不评估自然语言问数准确率。

## 开发与验证

```bash
python -m unittest discover -s tests -v
bash -n setup.sh
```

测试覆盖 SQL 拒绝规则、行数限制和数据库事务调用顺序，使用 mock，不连接真实数据库或模型。GitHub Actions 在 Python 3.11 下运行这些检查。

```text
app/              FastAPI 后端源码
scripts/          PostgreSQL 合成示例数据生成脚本
frontend/dist/    网页构建产物，暂缺前端源码
tests/            离线回归测试
.env.example      无密钥的配置模板
requirements.txt  Python 依赖
setup.sh          macOS conda 安装脚本
```

## 贡献与许可

提交问题时附上复现步骤、Python 版本和脱敏错误信息。不要上传 API key、连接密码、`.env`、状态库或真实查询数据。修改后运行离线测试；前端源码尚未恢复，暂时无法接受可重建的前端源码修改。

本项目原创代码采用 [MIT 许可证](LICENSE)。现有前端产物包含第三方代码，其授权和已识别组件见 [第三方说明](THIRD_PARTY_NOTICES.md)。
