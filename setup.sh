#!/usr/bin/env bash
# 智能问数 - macOS conda 环境初始化脚本
#
# 作用：只做“创建 conda 环境 + 安装依赖”，不启动任何服务。
#       前后端由你手动启动（脚本结束后会打印命令）。
#
# 用法：
#   bash setup.sh
#
# 可选环境变量：
#   CONDA_ENV_NAME  环境名，默认 nlp2sql
#   PY_VERSION      Python 版本，默认 3.11
#   PIP_INDEX_URL   pip 源，默认 PyPI，可指定其他镜像
#   CONDA_BIN      指定 conda 可执行文件路径（自动查找失败时用）

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

ENV_NAME="${CONDA_ENV_NAME:-nlp2sql}"
PY_VERSION="${PY_VERSION:-3.11}"
PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.org/simple}"

# 1) 定位 conda（优先 PATH，其次常见安装路径）
CONDA_BIN="${CONDA_BIN:-}"
if [ -z "$CONDA_BIN" ]; then
  if command -v conda >/dev/null 2>&1; then
    CONDA_BIN="$(command -v conda)"
  else
    for cand in \
      "$HOME/miniconda3/bin/conda" \
      "$HOME/anaconda3/bin/conda" \
      "/opt/homebrew/Caskroom/miniconda/base/bin/conda" \
      "/opt/miniconda3/bin/conda" \
      "/usr/local/miniconda3/bin/conda"; do
      if [ -x "$cand" ]; then CONDA_BIN="$cand"; break; fi
    done
  fi
fi
if [ -z "$CONDA_BIN" ]; then
  echo "[错误] 未找到 conda。请确认已安装 miniconda，或手动指定："
  echo "       CONDA_BIN=/path/to/conda bash setup.sh"
  exit 1
fi
echo "[信息] 使用 conda：$CONDA_BIN"

# 2) 创建 conda 环境
if "$CONDA_BIN" env list | awk 'NF && $1 !~ /^#/ {print $1}' | grep -qx "$ENV_NAME"; then
  echo "[信息] conda 环境已存在：$ENV_NAME（跳过创建）"
else
  echo "[信息] 创建 conda 环境：$ENV_NAME (python $PY_VERSION)"
  "$CONDA_BIN" create -n "$ENV_NAME" "python=$PY_VERSION" -y
fi

# 3) 安装依赖
echo "[信息] 安装依赖（pip 源：$PIP_INDEX_URL）..."
"$CONDA_BIN" run -n "$ENV_NAME" python -m pip install --upgrade pip -i "$PIP_INDEX_URL" >/dev/null 2>&1 || true
"$CONDA_BIN" run -n "$ENV_NAME" python -m pip install -r requirements.txt -i "$PIP_INDEX_URL"

echo ""
echo "========== 环境准备完成 =========="
echo "环境名：$ENV_NAME"
echo ""
echo "首次使用请复制 .env.example 为 .env，填写模型凭据和本地 DATASOURCE_SECRET_KEY。"
echo "已有 .env 时请保留原配置。运行说明见 README.md。"
echo ""
echo "请手动启动前后端（两个终端分别执行）："
echo ""
echo "  # 终端 1：后端（http://127.0.0.1:8000）"
echo "  conda activate $ENV_NAME"
echo "  cd $DIR"
echo "  python -m uvicorn app.main:app --host 127.0.0.1 --port 8000"
echo ""
echo "  # 终端 2：前端网页（http://127.0.0.1:5173）"
echo "  conda activate $ENV_NAME"
echo "  cd $DIR"
echo "  python -m http.server 5173 --bind 127.0.0.1 --directory frontend/dist"
echo ""
echo "启动后打开 http://127.0.0.1:5173 ，并在网页“设置”里把后端服务地址填为 http://127.0.0.1:8000"
echo "=================================="
