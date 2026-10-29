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
  echo "→ 上传正常：最新一次（$when）成功，更早的 ✘ 不用管"
elif [[ -n $last ]]; then
  echo "→ 最新一次上传（$when）没成功，原因见上"
fi

echo
echo "== 已收到的数据 =="
# Claude only reads a file whose header names the columns in English, which
# Health Auto Export writes only while the app itself runs in English.
csvs=("$BASE"/data/health_*.csv)
readable=()
unreadable=()
for f in "${csvs[@]}"; do
  if [[ $(head -n 1 "$f") == *Date/Time* ]]; then readable+=("$f"); else unreadable+=("$f"); fi
done
days=""
if ((${#readable[@]})); then
  days=$(tail -q -n +2 "${readable[@]}" | tr -d '"' | cut -c1-10 | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' | sort -u)
fi
if [[ -n $days ]]; then
  echo "健康指标：$(wc -l <<<"$days") 天（$(head -n 1 <<<"$days") 到 $(tail -n 1 <<<"$days")）"
else
  echo "健康指标：还没有"
fi
if ((${#unreadable[@]})); then
  first=$(head -n 1 "${unreadable[-1]}" | cut -d, -f1 | tr -d '"\r')
  first=${first#$'\xef\xbb\xbf'}
  echo "⚠ 有 ${#unreadable[@]} 个健康指标文件 Claude 读不了：表头是「$first」，不是「Date/Time」"
  if [[ ${unreadable[-1]} == "${csvs[-1]}" ]]; then
    echo "→ 最新一次上传也读不了：把 Health Auto Export 的语言改成 English，再重新导出这些天"
  else
    echo "→ 之后收到的都读得了，这些旧文件不用管"
  fi
fi
workouts=("$BASE"/workouts/*.json)
if ((${#workouts[@]})); then
  starts=$(grep -ho '"start": "[0-9]\{4\}-[0-9]\{2\}-[0-9]\{2\}' "${workouts[@]}" | cut -d'"' -f4 | sort)
  echo "运动记录：${#workouts[@]} 条（$(head -n 1 <<<"$starts") 到 $(tail -n 1 <<<"$starts")）"
else
  echo "运动记录：还没有"
fi
# One file per night (named by the morning it ended on), per ECG and per
# notification (named by its start, YYYYMMDD_HHMMSS).
nights=("$BASE"/sleep/*.json)
if ((${#nights[@]})); then
  echo "入睡/起床时间：${#nights[@]} 晚（$(basename "${nights[0]}" .json) 到 $(basename "${nights[-1]}" .json)）"
else
  echo "入睡/起床时间：还没有（要「睡眠时间」自动化，见设定文档）"
fi
ecgs=("$BASE"/ecg/*.json)
if ((${#ecgs[@]})); then
  echo "心电图：${#ecgs[@]} 次（最近一次 $(basename "${ecgs[-1]}" .json | cut -c1-8)）"
else
  echo "心电图：还没有（没量过，或还没建「心电图」自动化）"
fi
notes=("$BASE"/hr-notifications/*.json)
echo "心率通知：${#notes[@]} 条"
moods=("$BASE"/state-of-mind/*.json)
echo "心情记录：${#moods[@]} 条"
