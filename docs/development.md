# 配置与开发

## 模型与运行配置

配置模板见 [.env.example](../.env.example)。默认使用 DashScope 原生 API；填写 `LLM_API_KEY`，确认该 key 可以调用模板中的语言模型与 embedding 服务。`EMBEDDING_API_KEY` 留空时依次回退到 `DASHSCOPE_API_KEY` 和语言模型 key；需要独立凭据时单独填写。

更换模型接口时，应核对 `LLM_API_URL`、`LLM_MODEL`、鉴权头和方案、`LLM_PAYLOAD_MODEL_PATH`、`LLM_PAYLOAD_MESSAGES_PATH`、`LLM_PAYLOAD_EXTRA_JSON`、`LLM_RESPONSE_CONTENT_PATHS`。embedding 服务还需匹配 URL、模型及向量维度，仅修改模型 URL 不足以保证兼容。

| 配置 | 默认值与用途 |
| --- | --- |
| `DATASOURCE_SECRET_KEY` | 必须替换模板值；生成随机值后填入 `.env`，注册数据源后保持稳定 |
| `PG_SCHEMA` | `public`；指定查询使用的 PostgreSQL 模式 |
| `SQL_GENERATION_MAX_ATTEMPTS` | `5`；包括首次生成，范围1–10 |
| `SEMANTIC_RETRIEVAL_ENABLED` | `true`；使用业务描述和别名检索相关表 |
| `QUERY_AUDIT_PATH` | `data/query-audit.jsonl`；空字符串关闭文件审计 |
| `CORS_ALLOW_ORIGINS` | 本机5173端口；前端端口变化时更新允许的地址 |

审计、缓存和连接凭据按敏感数据处理，见 [数据与安全说明](security.md)。

## 使用现有网页构建

不修改前端时，可从项目根目录运行已有构建产物：

```bash
python3 -m http.server 5173 --bind 127.0.0.1 --directory frontend/dist
```

静态服务没有开发代理，需在网页设置中填写后端地址。它只提供网页，Python 后端仍需单独启动。

## 构建与检查

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

后端离线测试覆盖 SQL 安全、字段与业务口径、关联粒度、排名阶段、修复和审计；前端测试覆盖图表字段绑定、空值和恶意配置。离线测试不连接真实数据库或模型，不能作为自然语言问数准确率评估。实际模型候选保存在 `tests/fixtures/business_sql/`。数据库及 HTTP 回放的范围与基线见 [2026-10-06](query-regression-2026-10-06.md) 和 [2026-10-07](query-regression-2026-10-07.md) 回归记录。GitHub Actions 在 Python 3.11 下运行后端检查。

## 项目结构

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

## 贡献

提交问题时附上复现步骤、Python/Node.js 版本和脱敏错误信息。不要上传 API key、连接密码、`.env`、状态库或真实查询数据。后端修改后运行离线测试；前端修改后运行测试和构建，并检查实际页面。
