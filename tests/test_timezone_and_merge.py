"""User time zone handling and newest-wins merging across exports."""
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .conftest import _import_server


def test_now_and_today_follow_user_tz(tmp_data_dir: Path, monkeypatch):
    # UTC+14: almost always a different calendar date than a UTC/US server.
    monkeypatch.setenv("USER_TZ", "Pacific/Kiritimati")
    server = _import_server(tmp_data_dir)
    expected = datetime.now(ZoneInfo("Pacific/Kiritimati")).replace(tzinfo=None)
    assert abs((server._now() - expected).total_seconds()) < 5
    assert server._today() == expected.date()


def test_report_date_defaults_to_users_today(tmp_data_dir: Path, monkeypatch):
    monkeypatch.setenv("USER_TZ", "Etc/GMT+12")  # UTC-12
    server = _import_server(tmp_data_dir)
    saved = json.loads(server.save_health_report(content="hello"))
    assert saved["date"] == datetime.now(ZoneInfo("Etc/GMT+12")).date().isoformat()


def test_without_user_tz_server_local_time_is_kept(tmp_data_dir: Path, monkeypatch):
    monkeypatch.delenv("USER_TZ", raising=False)
    server = _import_server(tmp_data_dir)
    assert server.USER_TZ is None
    assert abs((server._now() - datetime.now()).total_seconds()) < 5


def test_newer_export_wins_and_columns_merge_for_same_timestamp(tmp_data_dir: Path):
    (tmp_data_dir / "health_20260401_080000.csv").write_text(
        "Date/Time,Step Count (count),Heart Rate [Avg] (count/min)\n"
        "2026-04-01 09:00:00,100,70\n"
    )
    (tmp_data_dir / "health_20260401_100000.csv").write_text(
        "Date/Time,Step Count (count),Resting Heart Rate (count/min)\n"
        "2026-04-01 09:00:00,250,55\n"
        "2026-04-01 10:00:00,40,\n"
    )
    server = _import_server(tmp_data_dir)
    df = server.load_all_csv()

    assert len(df) == 2
    row = df.iloc[0]
    assert row["Step Count (count)"] == 250  # the later export's complete hour wins
    assert row["Heart Rate [Avg] (count/min)"] == 70  # only the earlier export had it
    assert row["Resting Heart Rate (count/min)"] == 55
    assert df["Date/Time"].is_monotonic_increasing


def test_offset_timestamps_are_converted_to_user_tz(tmp_data_dir: Path, monkeypatch):
    monkeypatch.setenv("USER_TZ", "Asia/Shanghai")
    (tmp_data_dir / "health_offsets.csv").write_text(
        "Date/Time,Step Count (count)\n"
        "2026-04-01 01:00:00 +0000,10\n"
        "2026-04-01 03:00:00 +0100,20\n"
    )
    server = _import_server(tmp_data_dir)
    df = server.load_all_csv()
    assert [str(t) for t in df["Date/Time"]] == ["2026-04-01 09:00:00", "2026-04-01 10:00:00"]
    assert df["Step Count (count)"].tolist() == [10, 20]


def test_get_metric_caps_rows(tmp_data_dir: Path):
    lines = ["Date/Time,Step Count (count)"]
    now = datetime.now()
    for minute in range(600):
        stamp = now.replace(second=0, microsecond=0).timestamp() - minute * 60
        lines.append(f"{datetime.fromtimestamp(stamp):%Y-%m-%d %H:%M:%S},{minute}")
    (tmp_data_dir / "health_many.csv").write_text("\n".join(lines) + "\n")
    server = _import_server(tmp_data_dir)
    out = server.get_metric("Step Count (count)", days=2)
    assert out.startswith("(showing the last 500 of 600 rows")


def test_imperial_and_kcal_columns_are_converted(tmp_data_dir: Path):
    (tmp_data_dir / "health_units.csv").write_text(
        "Date/Time,Active Energy (kcal),Walking + Running Distance (mi),Walking Speed (mi/hr),"
        "Apple Sleeping Wrist Temperature (degF),Blood Glucose (mg/dL)\n"
        "2026-04-01 00:00:00,100,2,3,98.6,90.08\n"
    )
    server = _import_server(tmp_data_dir)
    row = server.load_all_csv().iloc[0]

    assert round(row["Active Energy (kJ)"], 1) == 418.4
    assert round(row["Walking + Running Distance (km)"], 3) == 3.219
    assert round(row["Walking Speed (km/hr)"], 3) == 4.828
    assert round(row["Apple Sleeping Wrist Temperature (degC)"], 2) == 37.0
    assert round(row["Blood Glucose (mmol/L)"], 2) == 5.0
    assert "Active Energy (kcal)" not in row.index


def test_newer_unit_spellings_are_read_as_the_usual_columns(tmp_data_dir: Path):
    (tmp_data_dir / "health_units.csv").write_text(
        "Date/Time,Step Count (steps),Heart Rate [Avg] (bpm),Resting Heart Rate (bpm),"
        "Apple Sleeping Wrist Temperature (ºC),Body Temperature (°F)\n"
        "2026-04-01 00:00:00,5000,72,55,36.4,98.6\n"
    )
    server = _import_server(tmp_data_dir)
    row = server.load_all_csv().iloc[0]

    assert row["Step Count (count)"] == 5000
    assert row["Heart Rate [Avg] (count/min)"] == 72
    assert row["Resting Heart Rate (count/min)"] == 55
    assert row["Apple Sleeping Wrist Temperature (degC)"] == 36.4
    assert row["Body Temperature (degF)"] == 98.6
    assert "Step Count (steps)" not in row.index


def test_unit_switch_between_exports_keeps_one_series(tmp_data_dir: Path):
    (tmp_data_dir / "health_1.csv").write_text("Date/Time,Active Energy (kJ)\n2026-04-01 00:00:00,1000\n")
    (tmp_data_dir / "health_2.csv").write_text("Date/Time,Active Energy (kcal)\n2026-04-02 00:00:00,100\n")
    server = _import_server(tmp_data_dir)
    df = server.load_all_csv()
    assert list(df["Active Energy (kJ)"].round(1)) == [1000.0, 418.4]
