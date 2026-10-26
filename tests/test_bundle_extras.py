"""Baselines ignore today's unfinished totals; uncovered metrics reach the bundle."""
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .conftest import _import_server

TZ = "Asia/Shanghai"


def _write_days(data_dir: Path) -> None:
    today = datetime.now(ZoneInfo(TZ)).date()
    rows = ["Date/Time,Step Count (count),Resting Heart Rate (count/min),Time in Daylight (min),"
            "Underwater Depth (m),Sleep Analysis [Source] (text)"]
    for back in range(5, 0, -1):
        day = today - timedelta(days=back)
        rows.append(f"{day} 00:00:00,{8000 + back},{55 + back},{30 + back},,Watch")
    rows.append(f"{today} 00:00:00,1200,50,5,12.5,Watch")
    (data_dir / "health_20260101_000000_000000_aaaaaa.csv").write_text("\n".join(rows) + "\n")


def test_baselines_skip_todays_partial_totals(tmp_data_dir: Path, monkeypatch):
    monkeypatch.setenv("USER_TZ", TZ)
    _write_days(tmp_data_dir)
    server = _import_server(tmp_data_dir)
    baselines = {b["metric"]: b for b in json.loads(server.get_baselines(days=30))["baselines"]}

    yesterday = (datetime.now(ZoneInfo(TZ)).date() - timedelta(days=1)).isoformat()
    assert baselines["steps"]["yesterday_date"] == yesterday
    assert baselines["steps"]["yesterday"] == 8001
    assert baselines["steps"]["n_days"] == 5
    # Averages such as resting HR are complete for today and stay in.
    assert baselines["resting_hr"]["yesterday"] == 50
    assert baselines["resting_hr"]["n_days"] == 6


def test_other_metrics_lists_only_uncovered_numeric_columns(tmp_data_dir: Path, monkeypatch):
    monkeypatch.setenv("USER_TZ", TZ)
    _write_days(tmp_data_dir)
    server = _import_server(tmp_data_dir)
    other = server.other_metrics(days=7)

    assert set(other) == {"Time in Daylight (min)", "Underwater Depth (m)"}
    today = datetime.now(ZoneInfo(TZ)).date().isoformat()
    assert other["Underwater Depth (m)"] == {today: 12.5}
    assert len(other["Time in Daylight (min)"]) == 6
