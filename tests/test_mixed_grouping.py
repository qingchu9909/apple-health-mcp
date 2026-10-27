"""A backfill grouped by day next to the hourly sync grouped by minute."""
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .conftest import _import_server

TZ = "Asia/Shanghai"
COLUMNS = "Date/Time,Step Count (count),Resting Energy (kJ),Apple Stand Hour (hr),Heart Rate Variability (ms)"


def _days():
    today = datetime.now(ZoneInfo(TZ)).date()
    return today - timedelta(days=2), today - timedelta(days=1), today


def _write_minutes(data_dir: Path) -> None:
    _, yesterday, today = _days()
    (data_dir / "health_20260101_000000_000000_aaaaaa.csv").write_text("\n".join([
        COLUMNS,
        f"{yesterday} 08:00:00,4000,5,1,50",
        f"{yesterday} 18:00:00,6000,5,1,60",
        f"{today} 00:00:00,10,5,,",  # a minute that happens to start the day
        f"{today} 09:00:00,2000,5,1,",
    ]) + "\n")


def _write_backfill(data_dir: Path, name: str, steps: tuple[int, int, int]) -> None:
    # One row per day; today's is a snapshot taken in the morning.
    two_days_ago, yesterday, today = _days()
    (data_dir / name).write_text("\n".join([
        COLUMNS + ",Sleep Analysis [Total] (hr)",
        f"{two_days_ago} 00:00:00,{steps[0]},7000,12,55,7.5",
        f"{yesterday} 00:00:00,{steps[1]},7100,13,70,8.0",
        f"{today} 00:00:00,{steps[2]},2000,3,48,6.0",
    ]) + "\n")


def _by_date(records: str) -> dict:
    return {rec["date"]: rec for rec in json.loads(records)["records"]}


def test_minute_rows_win_over_a_days_total_without_counting_twice(tmp_data_dir: Path, monkeypatch):
    monkeypatch.setenv("USER_TZ", TZ)
    _write_minutes(tmp_data_dir)
    _write_backfill(tmp_data_dir, "health_20260102_000000_000000_bbbbbb.csv", (9000, 9800, 1500))
    server = _import_server(tmp_data_dir)
    two_days_ago, yesterday, today = (str(day) for day in _days())

    fitness = _by_date(server.get_daily_fitness(days=3))
    assert fitness[two_days_ago]["steps"] == 9000  # only the backfill has that day
    assert fitness[yesterday]["steps"] == 10000  # not 9800, not 19800
    assert fitness[today]["steps"] == 2010
    assert _by_date(server.get_daily_vitals(days=3))[yesterday]["hrv_ms"] == 55
    # Totals still fill in what the minute rows lack: the night's sleep, today's HRV.
    assert {d: r["total_h"] for d, r in _by_date(server.get_daily_sleep(days=3)).items()} == {
        two_days_ago: 7.5, yesterday: 8.0, today: 6.0,
    }
    assert _by_date(server.get_daily_vitals(days=3))[today]["hrv_ms"] == 48

    other = server.other_metrics(days=3)
    assert other["Apple Stand Hour (hr)"] == {two_days_ago: 12, yesterday: 2, today: 1}
    assert other["Resting Energy (kJ)"] == {two_days_ago: 7000, yesterday: 10, today: 10}

    summary = server.get_summary(days=3)
    assert ("Step Count (count): avg=9500.00, min=9000.00, max=10000.00, last=10000.00, "
            "today so far=2010.00") in summary


def test_a_later_backfill_after_compaction_still_counts_once(tmp_data_dir: Path, monkeypatch):
    monkeypatch.setenv("USER_TZ", TZ)
    _write_minutes(tmp_data_dir)
    _write_backfill(tmp_data_dir, "health_20260102_000000_000000_bbbbbb.csv", (9000, 9800, 1500))
    server = _import_server(tmp_data_dir)
    monkeypatch.setattr(server, "_COMPACT_AFTER", 1)
    server.load_all_csv()
    assert [f.name for f in tmp_data_dir.glob("health_*.csv")] == [
        "health_20260102_000000_000000_bbbbbb_merged.csv"
    ]

    _write_backfill(tmp_data_dir, "health_20260103_000000_000000_cccccc.csv", (9100, 9900, 1600))
    fitness = _by_date(server.get_daily_fitness(days=3))
    two_days_ago, yesterday, today = (str(day) for day in _days())
    assert fitness[two_days_ago]["steps"] == 9100  # the newer total for a day without minutes
    assert fitness[yesterday]["steps"] == 10000
    assert fitness[today]["steps"] == 2010


def test_latest_and_fitness_read_minute_rows_per_metric_and_per_day(tmp_data_dir: Path, monkeypatch):
    monkeypatch.setenv("USER_TZ", TZ)
    _write_minutes(tmp_data_dir)
    _write_backfill(tmp_data_dir, "health_20260102_000000_000000_bbbbbb.csv", (9000, 9800, 1500))
    server = _import_server(tmp_data_dir)
    two_days_ago, yesterday, today = (str(day) for day in _days())

    latest = json.loads(server.get_latest())
    assert latest["Step Count (count)"] == {"day_total": 2010, "date": today, "through": "09:00"}
    assert latest["Heart Rate Variability (ms)"] == {"value": 48, "time": f"{today} 00:00"}
    assert latest["Sleep Analysis [Total] (hr)"] == {"value": 6.0, "time": f"{today} 00:00"}

    lines = server.get_fitness(days=3).splitlines()
    assert lines[0].split()[:3] == ["Date", "Step", "Count"]
    assert [line.split()[:2] for line in lines[1:]] == [
        [two_days_ago, "9000.0"], [yesterday, "10000.0"], [today, "2010.0"],
    ]
