#!/usr/bin/env bash
set -euo pipefail

DATA_DIR="${GROK_DATA_DIR:-/app/data}"
LOG_DIR="${GROK_LOG_DIR:-/app/logs}"
CONFIG_FILE="${GROK_CONFIG_FILE:-${DATA_DIR}/config.json}"

mkdir -p "$DATA_DIR" "$LOG_DIR" "$DATA_DIR/accounts" "$DATA_DIR/cpa_auth" \
  "$DATA_DIR/grok2api_auth" "$DATA_DIR/cloakbrowser-cache"

if [[ ! -e "$CONFIG_FILE" ]]; then
  python - "$CONFIG_FILE" <<'PY'
import json
import os
import sys
from pathlib import Path

source = Path("/app/config.example.json")
target = Path(sys.argv[1])
config = json.loads(source.read_text(encoding="utf-8"))


def env_int(name, default, minimum, maximum):
    try:
        value = int(os.environ.get(name, str(default)) or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


browser_engine = os.environ.get("GROK_BROWSER_ENGINE", "camoufox").strip().lower()
config["browser_engine"] = browser_engine if browser_engine in {"camoufox", "cloakbrowser"} else "camoufox"
config["browser_headless"] = False
config["browser_traffic_savings_level"] = "more"
config["cpa_auth_dir"] = "data/cpa_auth"
config["grok2api_auth_dir"] = "data/grok2api_auth"
config["outlookemail_api_base"] = os.environ.get(
    "GROK_OUTLOOKEMAIL_API_BASE", "http://outlook-email:5000"
).strip()
grokiq_url = os.environ.get("GROKIQ_WEBHOOK_URL", "").strip()
grokiq_token = os.environ.get("GROKIQ_WEBHOOK_TOKEN", "").strip()
if grokiq_url:
    config["grokiq_webhook_url"] = grokiq_url
if grokiq_token:
    config["grokiq_webhook_token"] = grokiq_token
config["grokiq_webhook_enabled"] = os.environ.get(
    "GROKIQ_WEBHOOK_ENABLED", "0"
).strip().lower() in {"1", "true", "yes", "on"}
config["grokiq_webhook_timeout_seconds"] = env_int(
    "GROKIQ_WEBHOOK_TIMEOUT_SECONDS", 10, 1, 60
)
target.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
  echo "[docker] 已创建容器默认配置: $CONFIG_FILE"
elif [[ -d "$CONFIG_FILE" ]]; then
  echo "[docker] 配置路径是目录而不是文件: $CONFIG_FILE" >&2
  exit 1
fi

# Bind mount 可能由宿主机 root 创建，启动时修正容器内权限。
chown -R app:app "$DATA_DIR" "$LOG_DIR"

LOG_FILE="$LOG_DIR/container-$(date -u +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$LOG_FILE") 2>&1

echo "[docker] DISPLAY=${DISPLAY:-:99}"
echo "[docker] 浏览器后端: Camoufox / CloakBrowser（由配置选择，默认 Camoufox）"
echo "[docker] 浏览器模式: 有头（Xvfb 虚拟显示器）"
echo "[docker] 配置: $CONFIG_FILE"
echo "[docker] 数据: $DATA_DIR"
echo "[docker] 日志: $LOG_FILE"

if [[ ! -e "${XDG_CACHE_HOME:-/opt/camoufox-cache}/camoufox/.0.5_FLAG" ]]; then
  echo "[docker] 警告: 镜像内未找到 Camoufox 浏览器内核（official/stable）。" >&2
  echo "[docker] 注册会在启动浏览器时失败。请重新构建镜像，或在容器内执行: python -m camoufox fetch" >&2
fi

export HOME=/home/app
export XDG_CACHE_HOME=${XDG_CACHE_HOME:-/opt/camoufox-cache}
export CLOAKBROWSER_CACHE_DIR=${CLOAKBROWSER_CACHE_DIR:-${DATA_DIR}/cloakbrowser-cache}
export DISPLAY=${DISPLAY:-:99}
export GROK_FORCE_HEADED=${GROK_FORCE_HEADED:-1}

exec gosu app xvfb-run \
  --auto-servernum \
  --server-args="-screen 0 ${XVFB_SCREEN:-1920x1080x24} -nolisten tcp" \
  "$@"
