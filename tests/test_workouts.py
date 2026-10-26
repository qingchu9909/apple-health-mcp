"""Tests for the workout tools (Health Auto Export workouts, export version 2)."""
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .conftest import _import_server

TZ = ZoneInfo("Asia/Shanghai")


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S %z")


def _write_workout(workouts_dir: Path, workout: dict) -> None:
    (workouts_dir / f"{workout['id']}.json").write_text(json.dumps(workout), encoding="utf-8")


def _run(start: datetime, workout_id: str = "RUN-1") -> dict:
    return {
        "id": workout_id,
        "name": "Outdoor Run",
        "start": _stamp(start),
        "end": _stamp(start + timedelta(minutes=30)),
        "duration": 1800,
        "location": "Outdoor",
        "activeEnergyBurned": {"qty": 350.44, "units": "kcal"},
        "distance": {"qty": 5.021, "units": "km"},
        "elevationUp": {"qty": 42, "units": "m"},
        "heartRate": {
            "min": {"qty": 95, "units": "bpm"},
            "avg": {"qty": 148, "units": "bpm"},
            "max": {"qty": 171, "units": "bpm"},
        },
        "heartRateData": [
            {"date": _stamp(start), "Min": 120, "Avg": 130, "Max": 140, "units": "bpm"},
            {"date": _stamp(start + timedelta(minutes=1)), "Min": 141, "Avg": 150, "Max": 160, "units": "bpm"},
        ],
        "stepCount": [{"date": _stamp(start), "qty": 2000, "units": "count"}, {"qty": 3000}],
        "metadata": {"HKIndoorWorkout": False},
    }


def _server(tmp_data_dir: Path, monkeypatch):
    monkeypatch.setenv("USER_TZ", "Asia/Shanghai")
    server = _import_server(tmp_data_dir)
    return server, Path(server.WORKOUTS_DIR)


def test_get_workouts_summarises_recent_workouts(tmp_data_dir: Path, monkeypatch):
    server, workouts_dir = _server(tmp_data_dir, monkeypatch)
    start = (datetime.now(TZ) - timedelta(days=2)).replace(second=0, microsecond=0)
    _write_workout(workouts_dir, _run(start))
    _write_workout(workouts_dir, _run(start - timedelta(days=40), workout_id="OLD"))

    result = json.loads(server.get_workouts(days=14))
    assert result["days_returned"] == 1
    run = result["records"][0]
    assert run["id"] == "RUN-1"
    assert run["name"] == "Outdoor Run"
    assert run["start"] == start.strftime("%Y-%m-%d %H:%M")
    assert run["duration_min"] == 30.0
    assert run["distance"] == {"value": 5.02, "unit": "km"}
    assert run["active_energy"] == {"value": 350.44, "unit": "kcal"}
    assert run["hr_avg"] == {"value": 148.0, "unit": "bpm"}
    assert run["hr_max"]["value"] == 171.0
    assert "temperature" not in run  # absent fields are omitted, not null


def test_workout_times_are_shown_in_user_tz(tmp_data_dir: Path, monkeypatch):
    server, workouts_dir = _server(tmp_data_dir, monkeypatch)
    utc_start = (datetime.now(ZoneInfo("UTC")) - timedelta(days=1)).replace(
        hour=0, minute=30, second=0, microsecond=0
    )
    _write_workout(workouts_dir, _run(utc_start))
    run = json.loads(server.get_workouts(days=14))["records"][0]
    assert run["start"] == utc_start.astimezone(TZ).strftime("%Y-%m-%d %H:%M")  # 08:30 local


def test_get_workout_detail_includes_heart_rate_series(tmp_data_dir: Path, monkeypatch):
    server, workouts_dir = _server(tmp_data_dir, monkeypatch)
    start = (datetime.now(TZ) - timedelta(days=1)).replace(hour=7, minute=0, second=0, microsecond=0)
    _write_workout(workouts_dir, _run(start))

    detail = json.loads(server.get_workout_detail("RUN-1"))
    assert detail["heart_rate"][0] == {"time": "07:00", "min": 120.0, "avg": 130.0, "max": 140.0}
    assert detail["heart_rate"][1]["avg"] == 150.0
    assert detail["steps"] == 5000
    assert detail["metadata"] == {"HKIndoorWorkout": False}


def test_get_workout_detail_unknown_id(tmp_data_dir: Path, monkeypatch):
    server, _ = _server(tmp_data_dir, monkeypatch)
    assert "error" in json.loads(server.get_workout_detail("nope"))


def test_corrupt_workout_file_is_skipped(tmp_data_dir: Path, monkeypatch):
    server, workouts_dir = _server(tmp_data_dir, monkeypatch)
    (workouts_dir / "broken.json").write_text("{not json")
    assert json.loads(server.get_workouts(days=14))["days_returned"] == 0


def test_new_workout_files_are_picked_up_after_caching(tmp_data_dir: Path, monkeypatch):
    server, workouts_dir = _server(tmp_data_dir, monkeypatch)
    start = (datetime.now(TZ) - timedelta(days=1)).replace(second=0, microsecond=0)
    _write_workout(workouts_dir, _run(start))
    assert json.loads(server.get_workouts(days=14))["days_returned"] == 1
    _write_workout(workouts_dir, _run(start + timedelta(hours=3), workout_id="RUN-2"))
    assert json.loads(server.get_workouts(days=14))["days_returned"] == 2
