"""Tests for bedtime / wake time from the JSON sleep automation (SLEEP_DIR)."""
import json
from datetime import datetime, timedelta
from pathlib import Path

from .conftest import _import_server


def _write_night(server, date: str, **fields) -> None:
    path = Path(server.SLEEP_DIR) / f"{date}.json"
    path.write_text(json.dumps({"date": date, **fields}), encoding="utf-8")


def test_sleep_times_join_the_csv_record_of_the_same_morning(tmp_data_dir: Path, monkeypatch):
    monkeypatch.setenv("USER_TZ", "Asia/Shanghai")
    server = _import_server(tmp_data_dir)
    today = server._today()
    yesterday = today - timedelta(days=1)
    (tmp_data_dir / "health_1.csv").write_text(
        "Date/Time,Sleep Analysis [Total] (hr),Sleep Analysis [Deep] (hr)\n"
        f"{yesterday} 00:00:00,7.2,1.1\n"
    )
    _write_night(
        server, str(yesterday),
        sleepStart=f"{yesterday - timedelta(days=1)} 23:12:00 +0800", sleepEnd=f"{yesterday} 06:50:00 +0800",
        inBedStart=f"{yesterday - timedelta(days=1)} 22:58:00 +0800", totalSleep=7.25, rem=1.5,
    )
    # A night the CSV does not have yet.
    _write_night(server, str(today), sleepStart=f"{yesterday} 23:40:00 +0800", sleepEnd=f"{today} 07:05:00 +0800", totalSleep=6.9)
    # Too old for the window.
    old = today - timedelta(days=40)
    _write_night(server, str(old), sleepEnd=f"{old} 07:00:00 +0800")
    (Path(server.SLEEP_DIR) / "notes.json").write_text("{}")

    result = json.loads(server.get_daily_sleep(days=14))
    assert [rec["date"] for rec in result["records"]] == [str(yesterday), str(today)]
    first, second = result["records"]
    assert first["total_h"] == 7.2  # the CSV value wins
    assert first["deep_h"] == 1.1
    assert first["rem_h"] == 1.5  # filled from the JSON where the CSV has nothing
    assert first["bedtime"] == f"{yesterday - timedelta(days=1)} 23:12"
    assert first["wake_time"] == f"{yesterday} 06:50"
    assert first["in_bed_start"] == f"{yesterday - timedelta(days=1)} 22:58"
    assert "in_bed_end" not in first
    assert second == {"date": str(today), "bedtime": f"{yesterday} 23:40", "wake_time": f"{today} 07:05", "total_h": 6.9}
    assert result["days_returned"] == 2
    assert server._freshness()["latest_sleep_night"] == str(today)


def test_sleep_times_keep_to_the_days_of_the_csv_records(tmp_data_dir: Path, monkeypatch):
    server = _import_server(tmp_data_dir)
    monkeypatch.setattr(server, "_now", lambda: datetime(2026, 10, 27, 11, 10))
    (tmp_data_dir / "health_1.csv").write_text(
        "Date/Time,Sleep Analysis [Total] (hr)\n2026-10-20 00:00:00,6.97\n2026-10-21 00:00:00,6.86\n"
    )
    _write_night(server, "2026-10-20", sleepEnd="2026-10-20 07:37:00 +0800")
    _write_night(server, "2026-10-21", sleepEnd="2026-10-21 07:37:00 +0800")

    result = json.loads(server.get_daily_sleep(days=7))
    assert [rec["date"] for rec in result["records"]] == ["2026-10-21"]


def test_without_sleep_uploads_nothing_changes(tmp_data_dir: Path):
    server = _import_server(tmp_data_dir)
    assert json.loads(server.get_daily_sleep(days=14)) == {"records": [], "days_requested": 14, "days_returned": 0}
    assert server._freshness()["latest_sleep_night"] is None


def test_older_tools_also_give_bedtime_and_wake_time(tmp_data_dir: Path, monkeypatch):
    # A chat whose tool list predates the bedtime fields of get_daily_sleep
    # still finds them through list_metrics, get_metric, get_sleep and get_summary.
    monkeypatch.setenv("USER_TZ", "Asia/Shanghai")
    server = _import_server(tmp_data_dir)
    monkeypatch.setattr(server, "_now", lambda: datetime(2026, 10, 27, 11, 10))
    (tmp_data_dir / "health_1.csv").write_text("Date/Time,Sleep Analysis [Total] (hr)\n2026-10-26 00:00:00,7.65\n")
    _write_night(server, "2026-10-26", sleepStart="2026-10-25 23:30:00 +0800", sleepEnd="2026-10-26 07:09:00 +0800")

    listing = server.list_metrics()
    assert "sleep_times (nights with bedtime and wake time, also in get_daily_sleep): 1, latest 2026-10-26" in listing
    for name in ("sleep_times", "Bedtime", "起床时间"):
        record = json.loads(server.get_metric(name, days=7))["records"][0]
        assert (record["bedtime"], record["wake_time"]) == ("2026-10-25 23:30", "2026-10-26 07:09")
    line = "2026-10-26: bedtime 2026-10-25 23:30, wake time 2026-10-26 07:09"
    assert line in server.get_sleep(days=7)
    assert f"Latest night {line}" in server.get_summary(days=7)
