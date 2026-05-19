#!/bin/zsh

set -u

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_BIN="$APP_DIR/.venv/bin/python"
APP_FILE="$APP_DIR/app.py"
PORT="8501"
URL="http://localhost:${PORT}"

cd "$APP_DIR" || exit 1

echo "AI 圖片批次 Prompt 管理器"
echo "資料夾：$APP_DIR"
echo ""

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "找不到 Python 虛擬環境：$PYTHON_BIN"
  echo "請先在這個資料夾建立 .venv 並安裝 requirements.txt。"
  echo ""
  echo "按任意鍵關閉。"
  read -k 1
  exit 1
fi

if [[ ! -f "$APP_FILE" ]]; then
  echo "找不到 app.py：$APP_FILE"
  echo ""
  echo "按任意鍵關閉。"
  read -k 1
  exit 1
fi

LISTENER="$(lsof -nP -iTCP:${PORT} -sTCP:LISTEN 2>/dev/null || true)"
if [[ -n "$LISTENER" ]]; then
  echo "localhost:${PORT} 已經有服務在執行，直接開啟頁面：$URL"
  open "$URL"
  echo ""
  echo "如果開到的不是圖片生成工具，請先關掉占用 ${PORT} 的程式後再重新點這個檔案。"
  echo "按任意鍵關閉。"
  read -k 1
  exit 0
fi

echo "正在啟動 Streamlit，固定網址：$URL"
echo "瀏覽器會自動開啟。這個視窗保持開著代表服務正在執行。"
echo "要停止服務時，回到這個視窗按 Control-C。"
echo ""

(sleep 3 && open "$URL") &

exec "$PYTHON_BIN" -m streamlit run "$APP_FILE" \
  --server.headless true \
  --server.port "$PORT" \
  --browser.gatherUsageStats false
