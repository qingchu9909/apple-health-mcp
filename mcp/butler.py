"""Rules of the health butler.

Two things come out of here: the alerts the server pushes on its own when
last night or today clearly departs from the user's own recent data, and
today's readiness (how hard to train), which the report routines and the
Claude connector read.

Everything is a pure function of a Snapshot that server.py fills from the
uploads, so the tests can feed made-up nights. Baselines are personal: an
alert needs a clear move away from the user's own recent nights, never a
population norm. The thresholds match routines/*.zh.prompt.md.
"""
from __future__ import annotations

import bisect
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

# Per-reading metrics averaged between bedtime and wake time, with the fewest
# readings that still make a night (fewer means the watch was off, or only a
# daily average was uploaded).
NIGHT_FIELDS = {
    "hrv_night_ms": 6,
    "respiratory_rate_night": 6,
    "blood_oxygen_night_pct": 4,
}

# Personal baselines: how far back they look and how many values they need.
HRV_NIGHTS, HRV_MIN_NIGHTS = 14, 4
SLEEP_NIGHTS, SLEEP_MIN_NIGHTS = 14, 4
RESTING_HR_DAYS, RESTING_HR_MIN_DAYS = 30, 10
SHORT_BASELINE_NIGHTS, SHORT_BASELINE_MIN = 7, 4

# The watch writes today's resting heart rate early and revises it as the day
# goes on (2026-10-28 read 73 at 09:00 and 63 that night), so today's value
# counts from this hour; before it, yesterday's settled one stands in.
RESTING_HR_SETTLED_HOUR = 18

# Alert thresholds.
HRV_ALERT_SD = -1.5  # and at most HRV_ALERT_RATIO of the mean
HRV_ALERT_RATIO = 0.85
RESTING_HR_ALERT_BPM = 7
WRIST_TEMP_ALERT_C = 0.5
RESPIRATORY_ALERT = 3.0
BLOOD_OXYGEN_ALERT_PCT = 92  # two nights in a row below it

# Event alerts (watch notifications, ECGs) older than this are history, not news.
EVENT_WINDOW = timedelta(hours=24)

LEVELS = {
    "good": ("🟢", "状态不错，可以练强一点",
             "适合高强度或时间长一点的训练：间歇跑、力量训练，或 45–60 分钟的有氧。"),
    "normal": ("🟢", "状态正常，照常练",
               "中等强度就好：慢跑、骑车或快走 30–45 分钟，常规力量训练也可以，别冲极限。"),
    "easy": ("🟡", "恢复得一般，练轻一点",
             "低强度为主：散步、轻松骑车、拉伸或瑜伽 20–30 分钟，今天不上强度。"),
    "rest": ("🔴", "今天以恢复为主",
             "散步、拉伸就够了，别做高强度，晚上早点睡。"),
    "unknown": ("⚪", "昨晚没有数据",
                "按自己的感觉来，中等强度比较稳妥。"),
}


@dataclass
class Snapshot:
    """What the rules look at.

    Dates are local ``YYYY-MM-DD``; a night is dated by the morning it ended
    on, as in get_daily_sleep.
    """

    now: datetime
    nights: dict[str, dict] = field(default_factory=dict)  # get_daily_sleep records by date
    days: dict[str, dict] = field(default_factory=dict)  # get_daily_vitals records by date
    readings: dict[str, list[tuple[datetime, float]]] = field(default_factory=dict)  # NIGHT_FIELDS key -> readings
    ecg: list[dict] = field(default_factory=list)  # get_ecg records
    hr_notifications: list[dict] = field(default_factory=list)  # get_heart_rate_notifications records

    @property
    def today(self) -> str:
        return self.now.date().isoformat()


@dataclass(frozen=True)
class Finding:
    key: str  # the same thing on the same day keeps the same key, so it is pushed once
    title: str
    text: str


@dataclass
class _Check:
    name: str
    value: float
    unit: str
    text: str
    status: str  # good | ok | low | unknown
    baseline: float | None = None
    alert: Finding | None = None


def _time(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def hours_text(hours: float) -> str:
    """7.65 -> '7 小时 39 分'."""
    h, m = divmod(round(hours * 60), 60)
    if h and m:
        return f"{h} 小时 {m} 分"
    return f"{h} 小时" if h else f"{m} 分钟"


def when_text(moment: datetime, now: datetime) -> str:
    if moment.date() == now.date():
        return f"今天 {moment:%H:%M}"
    if moment.date() == now.date() - timedelta(days=1):
        return f"昨天 {moment:%H:%M}"
    return f"{moment.month}月{moment.day}日 {moment:%H:%M}"


def night_vitals(nights: dict[str, dict], readings: dict[str, list[tuple[datetime, float]]]) -> dict[str, dict]:
    """Mean of each NIGHT_FIELDS metric between bedtime and wake time, by night.

    The daily mean of HRV mixes daytime and sleeping readings, so it reads low
    on any day the watch was off at night. The night mean compares like with
    like.
    """
    ordered = {key: sorted(values) for key, values in readings.items()}
    times = {key: [moment for moment, _ in values] for key, values in ordered.items()}
    result: dict[str, dict] = {}
    for day, night in nights.items():
        start, end = _time(night.get("bedtime")), _time(night.get("wake_time"))
        if start is None or end is None or end <= start:
            continue
        for key, fewest in NIGHT_FIELDS.items():
            if key not in ordered:
                continue
            lo, hi = bisect.bisect_left(times[key], start), bisect.bisect_right(times[key], end)
            window = [value for _, value in ordered[key][lo:hi]]
            if len(window) >= fewest:
                result.setdefault(day, {})[key] = round(statistics.fmean(window), 1)
    return result


def _series(records: dict[str, dict], key: str) -> dict[str, float]:
    return {day: rec[key] for day, rec in records.items() if isinstance(rec.get(key), int | float)}


def _before(series: dict[str, float], day: str, span: int) -> list[float]:
    """Values of the ``span`` days before ``day``, oldest first."""
    first = (date.fromisoformat(day) - timedelta(days=span)).isoformat()
    return [value for d, value in sorted(series.items()) if first <= d < day]


def _sleep_check(snap: Snapshot) -> _Check | None:
    hours = _series(snap.nights, "total_h")
    if not hours.get(snap.today):
        return None
    value = hours[snap.today]
    prior = [h for h in _before(hours, snap.today, SLEEP_NIGHTS) if h > 0]
    median = statistics.median(prior) if len(prior) >= SLEEP_MIN_NIGHTS else None
    text = f"昨晚睡了 {hours_text(value)}"
    if median is not None:
        text += f"（近两周中位数 {hours_text(median)}）"
    if value < 6.0 or (median is not None and value < median - 1.0):
        status = "low"
    elif value >= 7.0 and (median is None or value >= median - 0.25):
        status = "good"
    else:
        status = "ok"
    return _Check("sleep", round(value, 2), "h", text, status, median)


def _hrv_check(snap: Snapshot, nightly: dict[str, dict]) -> _Check | None:
    series = _series(nightly, "hrv_night_ms")
    if snap.today not in series:
        return None
    value = series[snap.today]
    prior = _before(series, snap.today, HRV_NIGHTS)
    if len(prior) < HRV_MIN_NIGHTS:
        more = HRV_MIN_NIGHTS - len(prior)
        text = f"夜间 HRV {value:.0f} ms（还在攒你的基线，再戴表睡 {more} 晚就能比较）"
        return _Check("hrv_night", value, "ms", text, "unknown")
    mean = statistics.fmean(prior)
    # A floor of 5% keeps a run of very similar nights from turning a small dip into many SDs.
    sd = max(statistics.stdev(prior) if len(prior) > 1 else 0.0, 0.05 * mean)
    z = (value - mean) / sd
    text = f"夜间 HRV {value:.0f} ms（你最近 {len(prior)} 晚平均 {mean:.0f} ms）"
    status = "low" if z <= -1 else "good" if z >= 0.5 else "ok"
    check = _Check("hrv_night", value, "ms", text, status, round(mean, 1))
    if z <= HRV_ALERT_SD and value <= HRV_ALERT_RATIO * mean:
        drop = round((1 - value / mean) * 100)
        check.alert = Finding(
            f"{snap.today}:hrv_night",
            "夜间 HRV 偏低",
            f"昨晚夜间 HRV {value:.0f} ms，比你最近 {len(prior)} 晚的平均 {mean:.0f} ms 低 {drop}%。"
            "常见原因：睡得少、前一天练得狠、喝了酒、压力大，或者快感冒了。"
            "今天别上高强度，晚上早点睡，明早看会不会回升。",
        )
    return check


def _resting_hr_check(snap: Snapshot) -> _Check | None:
    series = _series(snap.days, "resting_hr")
    yesterday = (date.fromisoformat(snap.today) - timedelta(days=1)).isoformat()
    settled = snap.now.hour >= RESTING_HR_SETTLED_HOUR
    if snap.today in series and (settled or yesterday not in series):
        day = snap.today
    elif yesterday in series:
        day = yesterday
    else:
        return None
    value = series[day]
    label = "今天" if day == snap.today else "昨天"
    prior = _before(series, day, RESTING_HR_DAYS)
    if len(prior) < RESTING_HR_MIN_DAYS:
        return _Check("resting_hr", value, "bpm", f"{label}静息心率 {value:.0f}", "unknown")
    median = statistics.median(prior)
    delta = value - median
    text = f"{label}静息心率 {value:.0f}（你 30 天的中位数 {median:.0f}）"
    status = "low" if delta >= 5 else "good" if delta <= 1 else "ok"
    check = _Check("resting_hr", value, "bpm", text, status, median)
    if delta >= RESTING_HR_ALERT_BPM and (day != snap.today or settled):
        before = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
        streak = before in series and series[before] - median >= 5
        check.alert = Finding(
            f"{day}:resting_hr",
            "静息心率偏高",
            f"{label}静息心率 {value:.0f} 次/分，比你 30 天的中位数 {median:.0f} 高 {delta:.0f}。"
            + ("已经连续两天偏高。" if streak else "")
            + "常见原因：睡眠不足、训练疲劳、喝酒、脱水，或者身体在对抗感染。"
            "今天放轻松、多喝水；如果还发烧、喉咙痛或特别乏力，就好好休息。",
        )
    return check


def _wrist_temp_check(snap: Snapshot) -> _Check | None:
    series = _series(snap.nights, "wrist_temp_c")
    prior = _before(series, snap.today, SHORT_BASELINE_NIGHTS)
    if snap.today not in series or len(prior) < SHORT_BASELINE_MIN:
        return None
    mean = statistics.fmean(prior)
    delta = series[snap.today] - mean
    text = f"睡眠手腕温度比前 {len(prior)} 晚平均 {delta:+.2f} °C"
    check = _Check("wrist_temp", series[snap.today], "°C", text, "low" if delta >= 0.3 else "ok", round(mean, 2))
    if delta >= WRIST_TEMP_ALERT_C:
        check.alert = Finding(
            f"{snap.today}:wrist_temp",
            "睡眠手腕温度偏高",
            f"昨晚手腕温度比你前 {len(prior)} 晚的平均高 {delta:.2f} °C。"
            "可能是要感冒或发烧，也可能是喝了酒、晚饭吃得晚、屋里太热。"
            "如果同时觉得不舒服，量一下体温，今天别练太猛。",
        )
    return check


def _respiratory_check(snap: Snapshot, nightly: dict[str, dict]) -> _Check | None:
    series = _series(nightly, "respiratory_rate_night")
    prior = _before(series, snap.today, SHORT_BASELINE_NIGHTS)
    if snap.today not in series or len(prior) < SHORT_BASELINE_MIN:
        return None
    value, mean = series[snap.today], statistics.fmean(prior)
    delta = value - mean
    text = f"睡眠呼吸频率 {value:.1f} 次/分（前 {len(prior)} 晚平均 {mean:.1f}）"
    check = _Check("respiratory_rate", value, "/min", text, "low" if delta >= 2 else "ok", round(mean, 1))
    if delta >= RESPIRATORY_ALERT:
        check.alert = Finding(
            f"{snap.today}:respiratory_rate",
            "睡眠呼吸频率偏高",
            f"昨晚睡眠时呼吸 {value:.1f} 次/分，比你前 {len(prior)} 晚的平均 {mean:.1f} 高 {delta:.1f}。"
            "常见于发烧、感冒初期或呼吸道不舒服，也可能是练得太累。"
            "多留意身体；如果咳嗽、胸闷或发烧，及时看医生。",
        )
    return check


def _blood_oxygen_check(snap: Snapshot, nightly: dict[str, dict]) -> _Check | None:
    series = _series(nightly, "blood_oxygen_night_pct")
    if snap.today not in series:
        return None
    value = series[snap.today]
    yesterday = (date.fromisoformat(snap.today) - timedelta(days=1)).isoformat()
    previous = series.get(yesterday)
    low = previous is not None and max(value, previous) < BLOOD_OXYGEN_ALERT_PCT
    check = _Check("blood_oxygen", value, "%", f"夜间血氧平均 {value:.0f}%", "low" if low else "ok")
    if low:
        check.alert = Finding(
            f"{snap.today}:blood_oxygen",
            "夜间血氧连续偏低",
            f"连续两晚夜间血氧平均低于 {BLOOD_OXYGEN_ALERT_PCT}%（昨晚 {value:.0f}%，前一晚 {previous:.0f}%）。"
            "单晚偏低多半是表带松了或压到了手腕，连续两晚就值得留意：先把表带调紧一点；"
            "如果还打鼾、夜里憋醒或白天特别困，可以去睡眠门诊看看。",
        )
    return check


def _checks(snap: Snapshot) -> list[_Check]:
    nightly = night_vitals(snap.nights, snap.readings)
    checks = [
        _sleep_check(snap),
        _hrv_check(snap, nightly),
        _resting_hr_check(snap),
        _wrist_temp_check(snap),
        _respiratory_check(snap, nightly),
        _blood_oxygen_check(snap, nightly),
    ]
    return [check for check in checks if check is not None]


def _event_findings(snap: Snapshot) -> list[Finding]:
    cutoff = snap.now - EVENT_WINDOW
    found = []
    for note in snap.hr_notifications:
        start = _time(note.get("start"))
        if start is None or start < cutoff:
            continue
        kind, when = note.get("kind"), when_text(start, snap.now)
        span = ""
        if isinstance(note.get("hr_min"), int | float) and isinstance(note.get("hr_max"), int | float):
            span = f"（这段时间 {note['hr_min']:.0f}–{note['hr_max']:.0f} 次/分）"
        threshold = note.get("threshold_bpm")
        threshold = f" {threshold:.0f} 次/分" if isinstance(threshold, int | float) else "你设的提醒值"
        if kind == "irregular_rhythm":
            found.append(Finding(
                f"hr_notification:{note['start']}:{kind}",
                "手表提醒：心律不齐",
                f"{when}，手表多次检测到不规则的心律，可能是房颤的迹象。"
                "找个安静的时候用手表的心电图 App 量一次，并带着记录去看医生。",
            ))
        elif kind == "high":
            found.append(Finding(
                f"hr_notification:{note['start']}:{kind}",
                "手表提醒：静止时心率过高",
                f"{when}，你静止时心率超过{threshold}{span}。"
                "如果当时没在活动、没喝咖啡或酒、也不紧张，就多留意；"
                "反复出现，或者伴随心慌、胸闷、头晕，去看医生。",
            ))
        elif kind == "low":
            found.append(Finding(
                f"hr_notification:{note['start']}:{kind}",
                "手表提醒：心率过低",
                f"{when}，你的心率低于{threshold}{span}。"
                "平时练耐力、心率本来就低又没有不舒服的话问题不大；如果头晕、乏力或差点晕倒，去看医生。",
            ))
    for ecg in snap.ecg:
        start = _time(ecg.get("start"))
        if start is None or start < cutoff or "fibrillation" not in str(ecg.get("classification", "")).lower():
            continue
        rate = f"，平均心率 {ecg['average_hr']:.0f}" if isinstance(ecg.get("average_hr"), int | float) else ""
        found.append(Finding(
            f"ecg:{ecg['start']}",
            "心电图结果：房颤",
            f"{when_text(start, snap.now)} 量的心电图结果是房颤（Atrial Fibrillation）{rate}。"
            "手表的单导联心电图不能确诊，但房颤值得尽快让医生看：「健康」App 的心电图记录可以导出 PDF 带去。"
            "如果同时胸痛、气短或头晕，马上就医。",
        ))
    return found


def findings(snap: Snapshot) -> list[Finding]:
    """Everything worth pushing now: today's departures from the user's
    baselines, then watch notifications and ECGs of the last 24 hours."""
    return [check.alert for check in _checks(snap) if check.alert] + _event_findings(snap)


def readiness(snap: Snapshot) -> dict:
    """How hard to train today, from last night and the resting heart rate."""
    checks = _checks(snap)
    alerts = [check.alert for check in checks if check.alert]
    lows = [check for check in checks if check.status == "low"]
    goods = [check for check in checks if check.status == "good"]
    sleep = next((check for check in checks if check.name == "sleep"), None)
    if alerts or len(lows) >= 2 or (sleep is not None and sleep.value < 5):
        level = "rest"
    elif lows:
        level = "easy"
    elif len(goods) >= 2:
        level = "good"
    elif any(check.status != "unknown" for check in checks):
        level = "normal"
    else:
        level = "unknown"
    emoji, label, advice = LEVELS[level]
    notes = []
    if sleep is None:
        notes.append("昨晚没有睡眠记录：没戴表睡的话可以忽略；戴了的话解锁一下 iPhone，让它把数据传上来。")
    return {
        "date": snap.today,
        "level": level,
        "summary": f"{emoji} {label}",
        "advice": advice,
        "reasons": [check.text for check in checks],
        "signals": [
            {
                "name": check.name,
                "value": check.value,
                "unit": check.unit,
                "baseline": check.baseline,
                "status": check.status,
                "text": check.text,
            }
            for check in checks
        ],
        "alerts": [{"key": a.key, "title": a.title, "text": a.text} for a in alerts],
        "notes": notes,
    }


def alert_message(found: list[Finding]) -> tuple[str, str]:
    """Title and body of one push for everything found in the same pass."""
    if len(found) == 1:
        return f"健康提醒：{found[0].title}", found[0].text
    return f"健康提醒：{len(found)} 件事需要留意", "\n\n".join(f"**{f.title}**\n{f.text}" for f in found)


def welcome_message(quiet: str | None) -> tuple[str, str]:
    """The push that tells the user the alerts are on, sent once."""
    lines = [
        "以后数据明显偏离你自己的平常时，服务器会马上推到这里：",
        "- 夜间 HRV 比你最近的夜晚明显偏低",
        f"- 静息心率比你 30 天的中位数高 {RESTING_HR_ALERT_BPM} 次/分以上",
        f"- 睡眠手腕温度高 {WRIST_TEMP_ALERT_C} °C 以上，或睡眠呼吸频率高 {RESPIRATORY_ALERT:.0f} 次/分以上",
        f"- 连续两晚夜间血氧低于 {BLOOD_OXYGEN_ALERT_PCT}%",
        "- 手表报了心率过高、过低、心律不齐，或心电图结果是房颤",
        "",
        "同时发现的几件事合成一条，同一件事一天只提一次"
        + (f"，{quiet} 不打扰，夜里的留到早上再发。" if quiet else "。"),
        f"夜间 HRV 要戴表睡满 {HRV_MIN_NIGHTS} 晚之后才开始比较。这些提醒按你自己的数据判断，不是医疗诊断。",
    ]
    return "健康管家提醒已开启", "\n".join(lines)
