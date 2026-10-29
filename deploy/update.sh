#!/usr/bin/env bash
# Pull the latest code of the installed branch, then re-run the installer with
# the saved settings (tokens and push channels are kept).
set -euo pipefail

APP=/opt/health/app
# shellcheck source=/dev/null
source /opt/health/install.conf
# A branch that was merged and deleted falls back to main.
rc=0
git -C "$APP" ls-remote --exit-code --heads origin "$REF" >/dev/null || rc=$?
if ((rc == 2)); then
  echo "分支 $REF 已经不在了，改用 main"
  REF=main
elif ((rc)); then
  exit "$rc"
fi
git -C "$APP" fetch --quiet --depth 1 origin "$REF"
git -C "$APP" reset --quiet --hard FETCH_HEAD
REF=$REF HB_SKIP_FETCH=1 exec bash "$APP/deploy/install.sh" "$@"
