#!/usr/bin/env bash
# What the phone has sent lately: the last uploads (with the reason when one
# was refused) and how much data is on disk.
set -uo pipefail
shopt -s nullglob

BASE=/opt/health

echo "== 最近的上传 =="
uploads=$(journalctl -u health-ingest -n 5000 --no-pager -o short-iso 2>/dev/null |
  grep -E '"POST /ingest |\[ingest\] 401: ' | tail -n 12 |
  sed -E \
    -e 's/^([0-9-]+)T([0-9:]+).*\[ingest\] 401: (.*)$/\1 \2    原因：\3/' \
    -e 's/^([0-9-]+)T([0-9:]+).*" 200 OK$/\1 \2  ✔ 收到/' \
    -e 's/^([0-9-]+)T([0-9:]+).*" 401 Unauthorized$/\1 \2  ✘ 密码不对/' \
    -e 's/^([0-9-]+)T([0-9:]+).*" ([0-9]{3}) (.*)$/\1 \2  ✘ 出错 \3 \4/')
echo "${uploads:-还没有收到过上传}"
# Older refusals stay in the list above, so say plainly how the newest went.
last=$(grep -E ' (✔|✘) ' <<<"$uploads" | tail -n 1)
when=$(awk '{print $1, $2}' <<<"$last")
if [[ $last == *✔* ]]; then
  echo "→ 现在正常：最新一次上传（$when）成功，更早的 ✘ 不用管"
elif [[ -n $last ]]; then
  echo "→ 最新一次上传（$when）没成功，原因见上"
fi

echo
echo "== 已收到的数据 =="
csvs=("$BASE"/data/health_*.csv)
days=""
if ((${#csvs[@]})); then
  days=$(tail -q -n +2 "${csvs[@]}" | tr -d '"' | cut -c1-10 | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' | sort -u)
fi
if [[ -n $days ]]; then
  echo "健康指标：$(wc -l <<<"$days") 天（$(head -n 1 <<<"$days") 到 $(tail -n 1 <<<"$days")）"
else
  echo "健康指标：还没有"
fi
workouts=("$BASE"/workouts/*.json)
if ((${#workouts[@]})); then
  starts=$(grep -ho '"start": "[0-9]\{4\}-[0-9]\{2\}-[0-9]\{2\}' "${workouts[@]}" | cut -d'"' -f4 | sort)
  echo "运动记录：${#workouts[@]} 条（$(head -n 1 <<<"$starts") 到 $(tail -n 1 <<<"$starts")）"
else
  echo "运动记录：还没有"
fi
