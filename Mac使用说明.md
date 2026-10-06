# 智能问数 · macOS 运行说明

本项目使用 FastAPI 后端和 React/Vite 网页界面，支持 PostgreSQL，通过浏览器使用，默认仅在本机运行。以下使用 Python 3.11 和已验证的 Node.js 24。功能与快速开始见 [README](README.md)，数据保护与权限边界见 [数据与安全说明](docs/security.md)。

## 1. 创建 conda 环境

已安装 Miniconda 时，在项目目录执行：

```bash
bash setup.sh
conda activate nlp2sql
```

脚本创建 Python 3.11 环境并安装固定版本依赖，不启动服务。`psycopg[binary]` 使用预编译原生组件，可用性取决于平台 wheel 支持。

可覆盖环境名、Python 版本和 pip 源：

```bash
CONDA_ENV_NAME=myenv PY_VERSION=3.11 PIP_INDEX_URL=https://pypi.org/simple bash setup.sh
```

若找不到 conda：

```bash
CONDA_BIN=~/miniconda3/bin/conda bash setup.sh
```

## 2. 配置模型

```bash
cp .env.example .env
python -c 'import secrets; print(secrets.token_hex(32))'
```

将生成的随机值填入 `.env` 的 `DATASOURCE_SECRET_KEY`，填写模型与 embedding API 的凭据。已有 `.env` 时保留原配置；已有注册数据源时不要随意更改 secret。

模板使用 DashScope 原生 API 格式。自定义模型接口需同时匹配 payload 和响应路径。`.env` 与运行时 `data/` 均已加入 `.gitignore`。

## 3. 准备 PostgreSQL

可使用独立测试 PostgreSQL 或兼容服务（例如 Supabase）。从数据库管理端获取 Host、Port、User、Password、Database 和 SSL 模式。

对 Supabase Pooler 地址，用户名通常形如 `postgres.<项目ref>`；本项目会校验该格式。具体连接参数以服务商提供的信息为准。`PG_SCHEMA` 默认是 `public`。

电影业务示例由脚本合成，包含 `cities / cinemas / movies / screenings / customers / ticket_orders / order_items / movie_reviews` 八张表。使用专用的空测试数据库初始化：

```bash
python scripts/create_nl2sql_postgres_mock_db.py --host YOUR_DATABASE_HOST --port 5432 --user YOUR_SETUP_USER --password 'YOUR_TEST_DATABASE_PASSWORD' --database YOUR_TEST_DATABASE --sslmode require
```

初始化账号需要建表和写入权限，问数账号应仅授予必要业务表的读取权限。不要把管理员账号用于问数。脚本默认保留已有表并继续插入数据，重复运行并非幂等初始化。`--recreate` 会级联删除同名表再重建；仅在可丢弃的测试库中使用。不要把真实密码粘贴到共享日志或 issue；命令参数也可能留在 shell 历史中。

```bash
python scripts/create_nl2sql_postgres_mock_db.py --help
```

可调整各表数据量和随机种子；脚本同时使用运行日期，因此同一 seed 跨日运行的全部数据不保证一致。

## 4. 启动

终端一：

```bash
conda activate nlp2sql
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

终端二，从同一个项目目录进入前端；需先安装 Node.js，可使用已验证的 Node.js 24：

```bash
cd frontend
npm ci
npm run dev -- --host 127.0.0.1
```

访问 <http://127.0.0.1:5173>，进入「设置」，将后端地址填为 `http://127.0.0.1:8000`，填写只读数据库连接并保存注册。语义初始化需要调用模型/embedding API，失败信息会在返回结果中提供。

重新构建网页，在 `frontend/` 中执行 `npm run build`，输出到 `frontend/dist/`。也可在项目根目录直接启动现有构建产物：

```bash
python3 -m http.server 5173 --bind 127.0.0.1 --directory frontend/dist
```

静态服务没有开发代理，需填写后端地址。Vite 和静态服务都只提供网页，Python 后端仍需在另一个终端单独启动。

问数示例：`按类型统计电影数量`、`票房最高的电影有哪些`。停止服务时在两个终端分别按 `Ctrl+C`。

## 5. 排查

- **端口冲突**：可以更换 8000/5173；同时更新网页的后端地址。前端端口变化时，修改 `.env` 中 `CORS_ALLOW_ORIGINS` 为对应本地地址。
- **数据库连接失败**：检查主机、端口、用户名、库名、SSL 和网络；Pooler 参数应以数据库管理端给出的值为准。
- **模型或向量化失败**：核对 `.env` 中接口地址、凭据、模型名称和请求格式；注册成功不表示语义索引一定成功。
- **SQL 被拒绝**：已支持只读 WITH/CTE；主查询和每个 CTE 都须为 SELECT 查询，仍拒绝写入、危险函数、系统库和 SQL 注释。网页会显示后端具体错误；终端中的 `sql_validation_failed` 包含候选 SQL 与 trace_id，可据此核对误拒原因，分享前先脱敏。

本项目无 API 鉴权，当前密码持久化为可逆混淆。保持仅本机使用，不要将服务直接暴露到公网。问题、Schema 与部分查询结果会发送给配置的模型服务，详见 [数据与安全说明](docs/security.md)。
