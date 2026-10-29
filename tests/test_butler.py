"""Tests for the butler's rules (mcp/butler.py): night vitals, readiness, alerts."""
import sys
from datetime import date, datetime, timedelta

from .conftest import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "mcp"))
import butler  # noqa: E402

NOW = datetime(2026, 11, 10, 9, 0)
TODAY = "2026-11-10"


def _day(offset: int) -> str:
    return (date.fromisoformat(TODAY) + timedelta(days=offset)).isoformat()


def _night(day: str, hours: float = 7.5, **fields) -> dict:
    before = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    return {"date": day, "total_h": hours, "bedtime": f"{before} 23:30", "wake_time": f"{day} 07:00", **fields}


def _readings(day: str, value: float, count: int = 12, start_hour: int = 0) -> list[tuple[datetime, float]]:
    first = datetime.fromisoformat(day) + timedelta(hours=start_hour, minutes=5)
    return [(first + timedelta(minutes=30 * i), value) for i in range(count)]


def _snapshot(hrv_by_night: dict[str, float], hours_by_night: dict[str, float] | None = None, **extra):
    hours_by_night = hours_by_night or {}
    nights = {day: _night(day, hours_by_night.get(day, 7.2)) for day in hrv_by_night}
    readings = [r for day, value in hrv_by_night.items() for r in _readings(day, value)]
    return butler.Snapshot(now=NOW, nights=nights, readings={"hrv_night_ms": readings}, **extra)


USUAL_HRV = {_day(-i): value for i, value in zip(range(8, 0, -1), [58, 62, 60, 57, 61, 59, 63, 60], strict=True)}


def test_night_vitals_average_the_readings_between_bedtime_and_wake_time():
    nights = {TODAY: _night(TODAY)}
    readings = {
        # Asleep: 23:30 the night before until 07:00.
        "hrv_night_ms": _readings(TODAY, 60, count=12) + _readings(_day(-1), 90, count=2, start_hour=23)[1:]
        # Awake later in the day, and lower, as daytime readings are.
        + _readings(TODAY, 30, count=10, start_hour=12),
        "respiratory_rate_night": _readings(TODAY, 13.5, count=5),  # too few to make a night
    }
    nightly = butler.night_vitals(nights, readings)
    assert nightly == {TODAY: {"hrv_night_ms": round((60 * 12 + 90) / 13, 1)}}


def test_a_low_night_hrv_is_an_alert_and_a_rest_day():
    snap = _snapshot({**USUAL_HRV, TODAY: 45})
    found = butler.findings(snap)
    assert [f.key for f in found] == [f"{TODAY}:hrv_night"]
    assert "45 ms" in found[0].text and "平均 60 ms" in found[0].text and "低 25%" in found[0].text
    ready = butler.readiness(snap)
    assert ready["level"] == "rest"
    assert ready["alerts"][0]["title"] == "夜间 HRV 偏低"


def test_a_night_without_the_watch_raises_nothing():
    # The daily mean of HRV reads low on such a day; the night mean does not exist.
    snap = _snapshot(USUAL_HRV)
    snap.readings["hrv_night_ms"] += _readings(TODAY, 30, count=10, start_hour=9)
    assert butler.findings(snap) == []
    ready = butler.readiness(snap)
    assert ready["level"] == "unknown"
    assert ready["notes"] and "没有睡眠记录" in ready["notes"][0]


def test_the_night_hrv_baseline_needs_four_nights():
    snap = _snapshot({_day(-2): 60, _day(-1): 61, TODAY: 40})
    assert butler.findings(snap) == []
    signal = next(s for s in butler.readiness(snap)["signals"] if s["name"] == "hrv_night")
    assert signal["status"] == "unknown"
    assert "再戴表睡 2 晚" in signal["text"]


def test_readiness_levels():
    days = {_day(-i): {"date": _day(-i), "resting_hr": 58.0} for i in range(1, 25)}
    usual_sleep = dict.fromkeys(USUAL_HRV, 7.0)

    good = _snapshot({**USUAL_HRV, TODAY: 66}, {**usual_sleep, TODAY: 7.6},
                     days={**days, TODAY: {"date": TODAY, "resting_hr": 58.0}})
    assert butler.readiness(good)["level"] == "good"
    assert butler.readiness(good)["summary"] == "🟢 状态不错，可以练强一点"

    short_night = _snapshot({**USUAL_HRV, TODAY: 60}, {**usual_sleep, TODAY: 5.5},
                            days={**days, TODAY: {"date": TODAY, "resting_hr": 59.0}})
    assert butler.readiness(short_night)["level"] == "easy"

    two_lows = _snapshot({**USUAL_HRV, TODAY: 56}, {**usual_sleep, TODAY: 5.5},
                         days={**days, TODAY: {"date": TODAY, "resting_hr": 63.0}})
    assert butler.readiness(two_lows)["level"] == "rest"
    assert butler.findings(two_lows) == []  # low, but not alert-worthy

    no_sleep_to_speak_of = _snapshot({**USUAL_HRV, TODAY: 66}, {**usual_sleep, TODAY: 4.5})
    assert butler.readiness(no_sleep_to_speak_of)["level"] == "rest"

    plain = _snapshot({**USUAL_HRV, TODAY: 60}, {**usual_sleep, TODAY: 6.8})
    assert butler.readiness(plain)["level"] == "normal"


def test_a_high_resting_heart_rate_says_when_it_is_the_second_day():
    days = {_day(-i): {"date": _day(-i), "resting_hr": 58.0} for i in range(2, 25)}
    days[_day(-1)] = {"date": _day(-1), "resting_hr": 64.0}
    days[TODAY] = {"date": TODAY, "resting_hr": 66.0}
    evening = NOW.replace(hour=butler.RESTING_HR_SETTLED_HOUR)
    found = butler.findings(butler.Snapshot(now=evening, days=days))
    assert [f.key for f in found] == [f"{TODAY}:resting_hr"]
    assert "今天静息心率 66" in found[0].text and "高 8" in found[0].text and "连续两天" in found[0].text

    # In the morning today's value is still moving; yesterday's (6 above) does not alert.
    assert butler.findings(butler.Snapshot(now=NOW, days=days)) == []

    # Before today's value is in, yesterday's stands in, under yesterday's key.
    del days[TODAY]
    days[_day(-1)]["resting_hr"] = 66.0
    found = butler.findings(butler.Snapshot(now=NOW, days=days))
    assert [f.key for f in found] == [f"{_day(-1)}:resting_hr"]
    assert "昨天静息心率 66" in found[0].text and "连续两天" not in found[0].text


def test_wrist_temperature_breathing_and_blood_oxygen_alerts():
    nights = {_day(-i): _night(_day(-i), wrist_temp_c=36.3) for i in range(1, 8)}
    nights[TODAY] = _night(TODAY, wrist_temp_c=36.9)
    readings = {
        "respiratory_rate_night": [r for i in range(1, 8) for r in _readings(_day(-i), 13.0)]
        + _readings(TODAY, 16.5),
        "blood_oxygen_night_pct": _readings(_day(-1), 91.0) + _readings(TODAY, 90.0),
    }
    found = butler.findings(butler.Snapshot(now=NOW, nights=nights, readings=readings))
    assert [f.key.split(":")[1] for f in found] == ["wrist_temp", "respiratory_rate", "blood_oxygen"]
    assert "高 0.60 °C" in found[0].text
    assert "16.5 次/分" in found[1].text and "高 3.5" in found[1].text
    assert "昨晚 90%，前一晚 91%" in found[2].text

    # One low night of blood oxygen is usually the strap, not the lungs.
    readings["blood_oxygen_night_pct"] = _readings(_day(-1), 96.0) + _readings(TODAY, 90.0)
    found = butler.findings(butler.Snapshot(now=NOW, nights=nights, readings=readings))
    assert "blood_oxygen" not in [f.key.split(":")[1] for f in found]


def test_watch_notifications_and_afib_ecgs_of_the_last_day():
    snap = butler.Snapshot(
        now=NOW,
        hr_notifications=[
            {"start": "2026-11-10 08:12", "kind": "high", "threshold_bpm": 120.0, "hr_min": 121, "hr_max": 134},
            {"start": "2026-11-09 23:40", "kind": "irregular_rhythm"},
            {"start": "2026-11-08 12:00", "kind": "low", "threshold_bpm": 40.0},  # two days old
        ],
        ecg=[
            {"start": "2026-11-10 08:30", "classification": "Sinus Rhythm", "average_hr": 61},
            {"start": "2026-11-10 08:35", "classification": "Atrial Fibrillation", "average_hr": 96},
        ],
    )
    found = butler.findings(snap)
    assert [f.title for f in found] == ["手表提醒：静止时心率过高", "手表提醒：心律不齐", "心电图结果：房颤"]
    assert "今天 08:12" in found[0].text and "120 次/分" in found[0].text and "121–134" in found[0].text
    assert "昨天 23:40" in found[1].text
    assert "平均心率 96" in found[2].text
    assert len({f.key for f in found}) == 3


def test_one_push_carries_everything_found_together():
    one = butler.Finding("k1", "夜间 HRV 偏低", "a")
    two = butler.Finding("k2", "静息心率偏高", "b")
    assert butler.alert_message([one]) == ("健康提醒：夜间 HRV 偏低", "a")
    title, body = butler.alert_message([one, two])
    assert title == "健康提醒：2 件事需要留意"
    assert body == "**夜间 HRV 偏低**\na\n\n**静息心率偏高**\nb"
    title, body = butler.welcome_message("23:00–08:00")
    assert title == "健康管家提醒已开启"
    assert "23:00–08:00 不打扰" in body
    assert "不打扰" not in butler.welcome_message(None)[1]
