#!/usr/bin/env bash
# One-command installer for a Debian/Ubuntu VPS (run as root).
#
#   curl -fsSL https://raw.githubusercontent.com/<you>/apple-health-mcp/main/deploy/install.sh \
#     | DOMAIN=health.example.com USER_TZ=Asia/Shanghai TELEGRAM_BOT_TOKEN=123:abc bash
#
# Settings (environment variables; remembered for re-runs):
#   DOMAIN                 public host name, its A record pointing at this server
#   USER_TZ                your time zone, e.g. Asia/Shanghai (kept in /opt/health/.env)
#   REPO_URL, REF          where the code comes from (default: this fork, main)
#   WEB=auto|caddy|nginx|none
#                          reverse proxy to configure; auto uses an existing Caddy or
#                          Nginx, else installs Caddy when the HTTPS port is free
#   HTTPS_PORT=443         public HTTPS port
# Push channels (any subset, stored in /opt/health/.env):
#   TELEGRAM_BOT_TOKEN     TELEGRAM_CHAT_ID is read from the bot's latest message when unset
#   TELEGRAM_CHAT_ID
#   SERVERCHAN_SENDKEY     WeChat via Server酱
#   PUSHPLUS_TOKEN         WeChat via PushPlus
#
# Safe to re-run: tokens are generated once and kept. deploy/update.sh pulls the
# latest code and runs this again with the saved settings.
set -euo pipefail

BASE=/opt/health
APP=$BASE/app
CONF=$BASE/install.conf
ENV_FILE=$BASE/.env
SERVICE_USER=health
UV_VERSION=0.13.1

# `git reset` below may rewrite this very file; bash reads scripts lazily, so
# run from a private copy when started out of the checkout.
if [[ -z "${HB_REEXEC:-}" && "${BASH_SOURCE[0]:-}" == "$APP/"* ]]; then
  copy=$(mktemp)
  cp "${BASH_SOURCE[0]}" "$copy"
  HB_REEXEC=1 exec bash "$copy" "$@"
fi

FAILURES=()
step() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
ok() { printf '  \033[32m✔\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
fail() {
  printf '  \033[31m✘\033[0m %s\n' "$*"
  FAILURES+=("$*")
}
die() {
  printf '\n\033[31m安装中止：%s\033[0m\n' "$*" >&2
  exit 1
}

[[ $EUID -eq 0 ]] || die "请用 root 运行（先 sudo -i 再粘贴命令）"
command -v apt-get >/dev/null || die "只支持 Debian / Ubuntu"

# Command-line values win over the ones saved by the previous run.
SETTINGS=(DOMAIN REPO_URL REF WEB HTTPS_PORT)
for var in "${SETTINGS[@]}"; do declare "ARG_$var=${!var:-}"; done
if [[ -f $CONF ]]; then
  # shellcheck source=/dev/null
  source "$CONF"
fi
for var in "${SETTINGS[@]}"; do
  arg="ARG_$var"
  if [[ -n ${!arg} ]]; then declare "$var=${!arg}"; fi
done
DOMAIN=${DOMAIN:-}
REPO_URL=${REPO_URL:-https://github.com/qingchu9909/apple-health-mcp.git}
REF=${REF:-main}
WEB=${WEB:-auto}
HTTPS_PORT=${HTTPS_PORT:-443}

get_env() { [[ -f $ENV_FILE ]] && sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1; }
set_env() {
  # Replace or append KEY=VALUE without ever leaving the file world-readable.
  local tmp=$ENV_FILE.tmp
  install -m 640 -o root -g "$SERVICE_USER" /dev/null "$tmp"
  K=$1 V=$2 awk '
    index($0, ENVIRON["K"] "=") == 1 { if (!done) print ENVIRON["K"] "=" ENVIRON["V"]; done = 1; next }
    { print }
    END { if (!done) print ENVIRON["K"] "=" ENVIRON["V"] }
  ' "$ENV_FILE" >"$tmp"
  mv "$tmp" "$ENV_FILE"
}
new_token() { od -An -N32 -tx1 /dev/urandom | tr -d ' \n'; }

USER_TZ=${USER_TZ:-$(get_env USER_TZ || true)}
[[ -n $USER_TZ ]] || die "请在命令里加上你的时区，例如 USER_TZ=Asia/Shanghai"
[[ -f /usr/share/zoneinfo/$USER_TZ ]] || die "不认识的时区：$USER_TZ（例子：Asia/Shanghai、Asia/Tokyo、Europe/London）"
if [[ $WEB != none && ! $DOMAIN =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$ ]]; then
  die "请在命令里加上域名，例如 DOMAIN=health.example.com"
fi
[[ $HTTPS_PORT =~ ^[0-9]+$ ]] || die "HTTPS_PORT 必须是数字"
PUBLIC_BASE="https://${DOMAIN:-<你的域名>}"
[[ $HTTPS_PORT == 443 ]] || PUBLIC_BASE+=":$HTTPS_PORT"

step "系统依赖"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git curl ca-certificates iproute2 >/dev/null
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$BASE" --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi
install -d -m 755 "$BASE"
install -d -m 750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$BASE/data" "$BASE/reports" "$BASE/workouts"
ok "系统用户 $SERVICE_USER，数据目录 $BASE"

step "代码"
if [[ -z ${HB_SKIP_FETCH:-} ]]; then
  if [[ -d $APP/.git ]]; then
    git -C "$APP" remote set-url origin "$REPO_URL"
    git -C "$APP" fetch --quiet --depth 1 origin "$REF"
    git -C "$APP" reset --quiet --hard FETCH_HEAD
  else
    if [[ -e $APP ]]; then mv "$APP" "$APP.old.$(date +%s)"; fi
    git clone --quiet --depth 1 --branch "$REF" "$REPO_URL" "$APP"
  fi
fi
ok "$REF @ $(git -C "$APP" rev-parse --short HEAD)"

step "Python 环境"
export UV_PYTHON_INSTALL_DIR=$BASE/python UV_CACHE_DIR=$BASE/.cache/uv UV_NO_PROGRESS=1
quietly() {
  # Show a command's output only when it fails.
  local log
  log=$(mktemp)
  if ! "$@" >"$log" 2>&1; then
    cat "$log" >&2
    rm -f "$log"
    die "命令失败：$*"
  fi
  rm -f "$log"
}
install_uv() {
  curl -fsSL "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-installer.sh" |
    env UV_UNMANAGED_INSTALL="$BASE/bin" sh
}
if [[ ! -x $BASE/bin/uv || "$("$BASE/bin/uv" --version 2>/dev/null | awk '{print $2}')" != "$UV_VERSION" ]]; then
  quietly install_uv
fi
if [[ ! -x $BASE/venv/bin/python ]]; then
  quietly "$BASE/bin/uv" venv --python 3.12 "$BASE/venv"
fi
quietly "$BASE/bin/uv" pip install --python "$BASE/venv/bin/python" -r "$APP/requirements.txt"
ok "Python $("$BASE/venv/bin/python" -c 'import platform; print(platform.python_version())')，依赖已装好"

step "配置与令牌"
if [[ ! -f $ENV_FILE ]]; then install -m 640 -o root -g "$SERVICE_USER" /dev/null "$ENV_FILE"; fi
for key in HEALTH_TOKEN MCP_PATH_TOKEN REPORT_API_TOKEN; do
  if [[ -z $(get_env "$key") ]]; then set_env "$key" "$(new_token)"; fi
done
set_env DATA_DIR "$BASE/data"
set_env REPORTS_DIR "$BASE/reports"
set_env WORKOUTS_DIR "$BASE/workouts"
set_env USER_TZ "$USER_TZ"
for key in TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID SERVERCHAN_SENDKEY PUSHPLUS_TOKEN; do
  if [[ -n ${!key:-} ]]; then set_env "$key" "${!key}"; fi
done
if [[ -n $(get_env TELEGRAM_BOT_TOKEN) && -z $(get_env TELEGRAM_CHAT_ID) ]]; then
  chat_id=$(curl -fsS --max-time 15 "https://api.telegram.org/bot$(get_env TELEGRAM_BOT_TOKEN)/getUpdates" |
    "$BASE/venv/bin/python" -c '
import json, sys
updates = json.load(sys.stdin).get("result", [])
chats = [u[k]["chat"]["id"] for u in updates for k in ("message", "my_chat_member") if k in u]
print(chats[-1] if chats else "")' 2>/dev/null || true)
  if [[ -n $chat_id ]]; then
    set_env TELEGRAM_CHAT_ID "$chat_id"
  else
    fail "Telegram：没找到对话。先在 Telegram 里给你的机器人发一句话，再重新运行这条命令"
  fi
fi
printf 'DOMAIN=%q\nREPO_URL=%q\nREF=%q\nWEB=%q\nHTTPS_PORT=%q\n' \
  "$DOMAIN" "$REPO_URL" "$REF" "$WEB" "$HTTPS_PORT" >"$CONF"
ok "令牌和设置保存在 $ENV_FILE（时区 $USER_TZ）"

step "后台服务"
for unit in health-ingest health-mcp; do
  sed -e "s|@APP@|$APP|g" -e "s|@BASE@|$BASE|g" -e "s|@USER@|$SERVICE_USER|g" \
    "$APP/deploy/systemd/$unit.service" >"/etc/systemd/system/$unit.service"
done
systemctl daemon-reload
systemctl enable --quiet health-ingest health-mcp
systemctl restart health-ingest health-mcp
ok "health-ingest（127.0.0.1:8001）和 health-mcp（127.0.0.1:8002）已启动并设为开机自启"

HEALTH_TOKEN=$(get_env HEALTH_TOKEN)
MCP_PATH_TOKEN=$(get_env MCP_PATH_TOKEN)
REPORT_API_TOKEN=$(get_env REPORT_API_TOKEN)

check_ingest() { curl -fsS --max-time 15 "${@:2}" -X POST -H "Authorization: Bearer $HEALTH_TOKEN" --data-binary '' "$1/ingest" | grep -q '"empty"'; }
check_mcp() {
  curl -fsS --max-time 20 "${@:2}" -X POST "$1/mcp/$MCP_PATH_TOKEN" \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"install-check","version":"1"}}}' |
    grep -q '"serverInfo"'
}
check_api() { curl -fsS --max-time 60 "${@:2}" -H "Authorization: Bearer $REPORT_API_TOKEN" "$1/api/bundle?days=1" | grep -q '"freshness"'; }
wait_for() {
  local _
  for _ in $(seq 1 "$2"); do
    if curl -fsS --max-time 5 "${@:3}" "$1" >/dev/null 2>&1; then return 0; fi
    sleep 2
  done
  return 1
}

step "本机自检"
if wait_for http://127.0.0.1:8001/health 15 && check_ingest http://127.0.0.1:8001; then
  ok "数据接收服务正常"
else
  fail "数据接收服务异常：journalctl -u health-ingest -n 50"
fi
if wait_for http://127.0.0.1:8002/healthz 15 && check_mcp http://127.0.0.1:8002 && check_api http://127.0.0.1:8002; then
  ok "MCP 服务和日报接口正常"
else
  fail "MCP 服务异常：journalctl -u health-mcp -n 50"
fi

port_owner() { ss -ltnpH "( sport = :$1 )" 2>/dev/null | sed -nE 's/.*users:\(\("([^"]+)".*/\1/p' | head -n 1; }

setup_caddy() {
  local site=/etc/caddy/health-butler.caddy addr=$DOMAIN fresh=0
  [[ $HTTPS_PORT == 443 ]] || addr="$DOMAIN:$HTTPS_PORT"
  if ! command -v caddy >/dev/null; then
    # The package starts a welcome page on :80, which fails when 80 is taken;
    # that is fine, the config is replaced and Caddy restarted below.
    apt-get install -y -qq caddy >/dev/null 2>&1 || true
    if ! command -v caddy >/dev/null; then
      fail "Caddy 安装失败：apt-get install caddy"
      return 1
    fi
    fresh=1
  fi
  sed "s|^health\.example\.com {|$addr {|" "$APP/deploy/caddy/Caddyfile.example" >"$site"
  if ((fresh)); then
    # Replace the welcome page. No HTTP->HTTPS redirects, so Caddy never
    # holds port 80 (x-ui / acme.sh renewals often need it); certificates
    # come via TLS-ALPN on 443, or a short-lived listener on 80.
    cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.dist
    printf '{\n\tauto_https disable_redirects\n}\n\nimport %s\n' "$site" >/etc/caddy/Caddyfile
  elif ! grep -qxF "import $site" /etc/caddy/Caddyfile; then
    cp /etc/caddy/Caddyfile "/etc/caddy/Caddyfile.bak.$(date +%s)"
    printf '\nimport %s\n' "$site" >>/etc/caddy/Caddyfile
  fi
  if ! caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1; then
    fail "Caddy 配置校验失败：caddy validate --config /etc/caddy/Caddyfile"
    return 1
  fi
  systemctl enable --quiet caddy
  systemctl reload-or-restart caddy
  ok "Caddy 已接管 $addr，会自动申请和续期证书"
}

setup_nginx() {
  local conf=/etc/nginx/conf.d/health-butler.conf
  [[ -d /etc/nginx/sites-enabled ]] && conf=/etc/nginx/sites-enabled/health-butler.conf
  if [[ ! -s /etc/letsencrypt/live/$DOMAIN/fullchain.pem ]]; then
    command -v certbot >/dev/null || apt-get install -y -qq certbot python3-certbot-nginx >/dev/null
    if ! certbot certonly --nginx -d "$DOMAIN" --non-interactive --agree-tos \
      --register-unsafely-without-email --quiet; then
      fail "证书申请失败：certbot certonly --nginx -d $DOMAIN"
      return 1
    fi
  fi
  sed -e "s/health\.example\.com/$DOMAIN/g" -e "s/listen 443 ssl;/listen $HTTPS_PORT ssl;/" \
    "$APP/deploy/nginx/health.single-domain.example.conf" >"$conf"
  install -d /etc/letsencrypt/renewal-hooks/deploy
  printf '#!/bin/sh\nsystemctl reload nginx\n' >/etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
  chmod 755 /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
  if ! nginx -t >/dev/null 2>&1; then
    rm -f "$conf"
    fail "Nginx 配置校验失败，已撤回：nginx -t"
    return 1
  fi
  systemctl reload nginx
  ok "Nginx 已接管 $DOMAIN:$HTTPS_PORT，证书由 certbot 自动续期"
}

if [[ $WEB != none ]]; then
  step "HTTPS（$PUBLIC_BASE）"
  public_ip=$(curl -fsS4 --max-time 10 https://api.ipify.org 2>/dev/null || true)
  dns_ip=$(getent ahostsv4 "$DOMAIN" | awk 'NR == 1 {print $1}')
  web=$WEB
  if [[ -z $dns_ip ]]; then
    fail "域名 $DOMAIN 还解析不到。请添加 A 记录指向 ${public_ip:-本机 IP}，生效后重新运行"
    web=none
  elif [[ -n $public_ip && $dns_ip != "$public_ip" ]]; then
    fail "域名 $DOMAIN 指向 $dns_ip，不是本机 $public_ip（用 Cloudflare 的话改成灰色云朵）"
    web=none
  fi
  if [[ $web == auto ]]; then
    p80=$(port_owner 80)
    p443=$(port_owner "$HTTPS_PORT")
    if [[ $p80 == caddy || $p443 == caddy ]]; then
      web=caddy
    elif [[ $p80 == nginx || $p443 == nginx ]]; then
      web=nginx
    elif [[ -z $p443 ]]; then
      web=caddy
    else
      fail "端口 $HTTPS_PORT 正被 $p443 使用，我没有动它。可以加 HTTPS_PORT=8443 换个端口重跑（80 端口需空闲以申请证书），或把这段输出发给 Claude"
      web=none
    fi
  fi
  if command -v ufw >/dev/null && ufw status | grep -q '^Status: active'; then
    ufw allow 80/tcp >/dev/null
    ufw allow "$HTTPS_PORT/tcp" >/dev/null
  fi
  case $web in
    caddy) setup_caddy || web=none ;;
    nginx) setup_nginx || web=none ;;
  esac

  if [[ $web != none ]]; then
    if wait_for "$PUBLIC_BASE/health" 30 && check_ingest "$PUBLIC_BASE" && check_mcp "$PUBLIC_BASE" && check_api "$PUBLIC_BASE"; then
      ok "外网 HTTPS 访问正常：接收、连接器、日报接口都通"
    elif check_mcp "$PUBLIC_BASE" --resolve "$DOMAIN:$HTTPS_PORT:127.0.0.1"; then
      fail "本机 HTTPS 正常，但从外网连不进来：检查服务商防火墙/安全组是否放行 80 和 $HTTPS_PORT"
    else
      fail "HTTPS 还不通（证书可能还在申请）。等一分钟后重新运行；还不行就把输出发给 Claude"
    fi
  fi
fi

push_setup_message() {
  cd "$APP/mcp" && DOTENV_PATH=$ENV_FILE "$BASE/venv/bin/python" - "$@" <<'PY'
import os
import sys

from dotenv import load_dotenv

load_dotenv(os.environ["DOTENV_PATH"])
import notify  # noqa: E402

results = notify.send_all(sys.argv[1], sys.argv[2])
print(" ".join(f"{name}:{status}" for name, status in results.items()))
sys.exit(0 if results and all(s == "ok" for s in results.values()) else 1)
PY
}

CONFIG_TEXT="**① iPhone：Health Auto Export（两个自动化都填这一套）**
- URL：\`$PUBLIC_BASE/ingest\`
- Header 名：\`Authorization\`
- Header 值：\`Bearer $HEALTH_TOKEN\`

**② Claude：自定义连接器 URL（认证选 None）**
\`$PUBLIC_BASE/mcp/$MCP_PATH_TOKEN\`

**③ Claude 云端定时日报：环境变量**
- \`HEALTH_API_BASE=$PUBLIC_BASE\`
- \`HEALTH_API_TOKEN=$REPORT_API_TOKEN\`

以后想再看这些：bash $APP/deploy/show-config.sh"

step "推送"
pushed=0
if [[ -n "$(get_env TELEGRAM_CHAT_ID)$(get_env SERVERCHAN_SENDKEY)$(get_env PUSHPLUS_TOKEN)" ]]; then
  if status=$(push_setup_message "健康管家已上线" "$CONFIG_TEXT
配置完可以删掉这条消息。" 2>&1); then
    ok "测试消息已发出（$status），手机上点一下等宽文字就能复制"
    pushed=1
  else
    fail "推送失败：$status"
  fi
else
  warn "还没配置推送渠道（Telegram / Server酱 / PushPlus），日报暂时只存服务器"
fi

if ((!pushed)); then
  step "你的配置（私密，别截图外传）"
  printf '%s\n' "$CONFIG_TEXT" | sed -e 's/\*\*//g' -e 's/`//g'
fi

echo
if ((${#FAILURES[@]})); then
  printf '\033[31m有 %d 项需要处理，把上面的 ✔/✘ 结果截图发给 Claude（遮住令牌）。\033[0m\n' "${#FAILURES[@]}"
  exit 1
fi
printf '\033[32m全部完成 ✅ 把上面的 ✔ 结果截图发给 Claude 即可（不用截令牌）。\033[0m\n'
