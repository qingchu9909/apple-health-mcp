#!/usr/bin/env bash
# Print the values to enter in Health Auto Export, the Claude connector and the
# report routines. They are secrets: anyone with them can read or write
# your health data.
set -euo pipefail

ENV_FILE=/opt/health/.env
CONF=/opt/health/install.conf
[[ -r $ENV_FILE && -r $CONF ]] || { echo "请用 root 运行，并先跑过 install.sh" >&2; exit 1; }
# shellcheck source=/dev/null
source "$CONF"
get_env() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1; }
base="https://$DOMAIN"
[[ ${HTTPS_PORT:-443} == 443 ]] || base+=":$HTTPS_PORT"

cat <<TEXT
① iPhone：Health Auto Export（两个自动化都填这一套）
- URL：$base/ingest
- Header 名：Authorization
- Header 值：Bearer $(get_env HEALTH_TOKEN)

② Claude：自定义连接器 URL（认证选 None）
$base/mcp/$(get_env MCP_PATH_TOKEN)

③ Claude 云端定时任务（早报、晚报、周报、月报）：环境变量
- HEALTH_API_BASE=$base
- HEALTH_API_TOKEN=$(get_env REPORT_API_TOKEN)
TEXT
