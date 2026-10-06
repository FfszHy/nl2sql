# NL2SQL · 智能问数

面向业务人员的 PostgreSQL 自然语言问数工具。输入业务问题，查看查询结果、中文说明和可视化图表。提供 FastAPI 后端、React/Vite 前端及影院示例数据脚本。

## Demo

使用影院合成数据的 44 秒实际录屏：会员购票偏好、月度收入趋势、渠道收入构成，以及电影评分与收入关系。下方动图自动播放，高清版本可下载观看。

[![智能问数 Demo：条形图、折线图、饼图与散点图](demo_video/demo-preview.gif)](https://github.com/FfszHy/nl2sql/raw/refs/heads/main/demo_video/demo.mp4)

[下载高清录屏（MP4）](https://github.com/FfszHy/nl2sql/raw/refs/heads/main/demo_video/demo.mp4)

## 核心功能

- **自然语言问数**：用业务问题进行多表统计、指标比较和分组排名，查看生成的 SQL。
- **业务口径配置**：将渠道、会员等名称对应到实际数据取值，明确人数、订单量和收入的计算方式。
- **自动可视化**：根据问题和查询结果选择条形图、折线图、饼图或散点图；不适合绘图时保留表格。
- **校验与自修复**：执行前检查只读结构、真实字段和已配置指标，可修复错误默认最多进行四轮修复。
- **数据源与历史管理**：注册 PostgreSQL 连接、配置业务描述和别名，在浏览器中保存和切换对话。

## 快速开始

准备 Python 3.11、Node.js 24（已验证版本）、可访问的 PostgreSQL 数据库，以及可用的模型和 embedding API。

### 1. 安装后端

```bash
git clone https://github.com/FfszHy/nl2sql.git
cd nl2sql
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

### 2. 配置模型

默认模板使用 DashScope。编辑 `.env`，填写 `LLM_API_KEY`，并将下面生成的随机值填入 `DATASOURCE_SECRET_KEY`：

```bash
python -c 'import secrets; print(secrets.token_hex(32))'
```

模型 key 需能调用模板中的语言模型和 embedding 服务；独立 embedding 凭据填入 `EMBEDDING_API_KEY`。注册数据源后保持 secret 不变。其他接口配置见 [配置与开发](docs/development.md)。

### 3. 启动服务

在项目根目录启动后端：

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

另开终端，从项目根目录启动前端：

```bash
cd frontend
npm ci
npm run dev -- --host 127.0.0.1
```

### 4. 连接数据库并提问

打开 <http://127.0.0.1:5173>，进入「设置」，填写后端地址 `http://127.0.0.1:8000` 和数据库连接，点击「保存并注册数据源」。使用仅能读取必要业务表的数据库账号，并检查语义索引初始化状态。

连接默认要求 SSL；无 SSL 本地数据库的连接配置见 [API 文档](docs/api.md#数据源配置)。影院示例库与 macOS 安装步骤见 [macOS 使用说明](Mac使用说明.md)。API 交互文档位于 <http://127.0.0.1:8000/docs>。

## 示例问题

以下问题对应内置影院示例库。收入默认指扣除退款后的净收款；接入其他业务数据库时需配置自己的名称和指标口径。

- **会员偏好 · 条形图**：普通、银卡、金卡和白金用户，各自最喜欢哪三类电影？按实际购票人数来看，同时看看订单量和扣掉退款后的收入，全额退款的订单不算。用条形图比较各会员等级的购票偏好。
- **月度趋势 · 折线图**：按下单月份统计扣掉退款后的收入，月份从早到晚排列，用折线图展示月度收入趋势。
- **渠道构成 · 饼图**：统计全部购票渠道扣掉退款后的收入金额，并用饼图展示渠道收入构成。
- **评分与收入 · 散点图**：只看至少有20条观众评价的电影，哪些电影的平均评分排进前20，但扣掉退款后的收入没有进前20？把评分、评价数量、收入和两项排名列出来。用散点图观察这些电影评分与收入的关系。

## 使用限制

- 当前没有用户鉴权，前后端仅绑定本机地址，不要直接暴露到公网。
- 数据库访问范围由数据库账号权限控制；请使用只读账号。
- 问题、数据库结构、业务描述及部分查询结果会发送给配置的模型/embedding 服务。
- `.env` 和 `data/` 包含敏感配置与本地状态。数据库密码采用可逆混淆，**不属于安全加密**，请保护这些文件。
- 模型可能生成错误 SQL 或误解结果，重要结论应核对 SQL 和数据。

详细说明见 [数据与安全说明](docs/security.md)。

## 文档

| 文档 | 内容 |
| --- | --- |
| [业务语义配置](docs/business-semantics.md) | 名称映射、指标公式和示例口径 |
| [API 与图表](docs/api.md) | 接口、请求格式和图表配置 |
| [配置与开发](docs/development.md) | 模型接口配置、构建、测试和贡献 |
| [查询契约](docs/query-contract.md) | SQL 生成、校验与自修复机制 |
| [数据与安全说明](docs/security.md) | 凭据存储、数据流与权限边界 |
| [macOS 使用说明](Mac使用说明.md) | conda 安装、影院示例数据与故障排查 |

## 贡献与许可

欢迎提交脱敏的复现步骤或 Pull Request，开发检查见 [配置与开发](docs/development.md#贡献)。

本项目原创代码采用 [MIT 许可证](LICENSE)，第三方组件授权见 [第三方说明](THIRD_PARTY_NOTICES.md)。
