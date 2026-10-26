#!/usr/bin/env bash
# Pull the latest code of the installed branch, then re-run the installer with
# the saved settings (tokens and push channels are kept).
set -euo pipefail

APP=/opt/health/app
# shellcheck source=/dev/null
source /opt/health/install.conf
git -C "$APP" fetch --quiet --depth 1 origin "$REF"
git -C "$APP" reset --quiet --hard FETCH_HEAD
HB_SKIP_FETCH=1 exec bash "$APP/deploy/install.sh" "$@"
