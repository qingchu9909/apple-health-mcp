"""Tests for the ECG, heart rate notification and State of Mind tools."""
import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .conftest import FIXTURES, _import_server

TZ = ZoneInfo("Asia/Shanghai")


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S %z")


def _write(directory: Path, name: str, event: dict) -> None:
    (directory / f"{name}.json").write_text(json.dumps(event), encoding="utf-8")


def _server(tmp_data_dir: Path, monkeypatch):
    monkeypatch.setenv("USER_TZ", "Asia/Shanghai")
    return _import_server(tmp_data_dir)


def test_get_ecg_lists_recent_recordings(tmp_data_dir: Path, monkeypatch):
    server = _server(tmp_data_dir, monkeypatch)
    start = (datetime.now(TZ) - timedelta(days=3)).replace(second=0, microsecond=0)
    ecg_dir = Path(server.ECG_DIR)
    _write(ecg_dir, "a", {"start": _stamp(start), "classification": "Sinus Rhythm", "averageHeartRate": 62.4})
    _write(ecg_dir, "b", {"start": _stamp(start - timedelta(days=200)), "classification": "Inconclusive"})
    _write(ecg_dir, "d", {"start": _stamp(start - timedelta(minutes=1)), "classification": "Inconclusive Poor Recording",
                          "averageHeartRate": 0})
    _write(ecg_dir, "c", {"start": _stamp(start + timedelta(hours=1)), "classification": "Atrial Fibrillation",
                          "averageHeartRate": {"qty": 96, "units": "bpm"}, "severity": "Moderate"})

    result = json.loads(server.get_ecg(days=90))
    assert result["days_returned"] == 3
    poor, first, second = result["records"]
    assert poor == {"start": (start - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M"),
                    "classification": "Inconclusive Poor Recording"}
    assert first == {"start": start.strftime("%Y-%m-%d %H:%M"), "classification": "Sinus Rhythm", "average_hr": 62.4}
    assert second["classification"] == "Atrial Fibrillation"
    assert second["average_hr"] == 96.0
    assert second["severity"] == "Moderate"


def test_heart_rate_notifications_are_told_apart(tmp_data_dir: Path, monkeypatch):
    server = _server(tmp_data_dir, monkeypatch)
    start = (datetime.now(TZ) - timedelta(days=1)).replace(second=0, microsecond=0)
    notes = Path(server.HR_NOTIFICATIONS_DIR)
    rates = [{"hr": hr, "units": "bpm"} for hr in (128, 131, 140)]
    _write(notes, "high", {"start": _stamp(start), "end": _stamp(start + timedelta(minutes=10)),
                           "threshold": 120, "heartRate": rates, "heartRateVariation": [{"hrv": 20, "units": "ms"}]})
    _write(notes, "low", {"start": _stamp(start + timedelta(hours=2)), "end": _stamp(start + timedelta(hours=2)),
                          "threshold": 40, "heartRate": [{"hr": 37}, {"hr": 38}], "heartRateVariation": []})
    _write(notes, "afib", {"start": _stamp(start + timedelta(hours=3)), "end": _stamp(start + timedelta(hours=4)),
                           "heartRate": [], "heartRateVariation": []})

    records = json.loads(server.get_heart_rate_notifications(days=30))["records"]
    assert [r["kind"] for r in records] == ["high", "low", "irregular_rhythm"]
    high, low, afib = records
    assert high["threshold_bpm"] == 120.0
    assert (high["hr_min"], high["hr_avg"], high["hr_max"]) == (128.0, 133.0, 140.0)
    assert high["hrv_avg_ms"] == 20.0
    assert low["hr_max"] == 38.0
    assert "threshold_bpm" not in afib and "hr_avg" not in afib
    assert afib["end"] == (start + timedelta(hours=4)).strftime("%Y-%m-%d %H:%M")


def test_get_state_of_mind_lists_logged_moods_in_local_time(tmp_data_dir: Path, monkeypatch):
    server = _server(tmp_data_dir, monkeypatch)
    moods = Path(server.STATE_OF_MIND_DIR)
    when = (datetime.now(ZoneInfo("UTC")) - timedelta(days=1)).replace(second=0, microsecond=0)
    _write(moods, "m1", {"id": "m1", "start": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "kind": "daily_mood",
                         "valence": -0.43, "valenceClassification": "slightly_unpleasant",
                         "labels": ["stressed", "tired"], "associations": ["work"], "metadata": {}})
    _write(moods, "m2", {"id": "m2", "start": (when - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "kind": "momentary_emotion", "valence": 0.8, "valenceClassification": "pleasant",
                         "labels": [], "associations": []})

    result = json.loads(server.get_state_of_mind(days=14))
    assert result["records"] == [{
        "time": when.astimezone(TZ).strftime("%Y-%m-%d %H:%M"),
        "kind": "daily_mood",
        "valence": -0.43,
        "feeling": "slightly_unpleasant",
        "labels": ["stressed", "tired"],
        "associations": ["work"],
    }]
    assert json.loads(server.get_state_of_mind(days=60))["days_returned"] == 2


def test_the_bundle_carries_ecg_and_notifications(tmp_data_dir: Path, monkeypatch):
    server = _server(tmp_data_dir, monkeypatch)
    start = datetime.now(TZ) - timedelta(days=2)
    _write(Path(server.ECG_DIR), "a", {"start": _stamp(start), "classification": "Sinus Rhythm"})
    bundle = server.build_bundle(days=14)
    assert bundle["ecg"]["days_returned"] == 1
    assert bundle["heart_rate_notifications"]["records"] == []
    assert bundle["state_of_mind"]["records"] == []


def test_clients_with_the_old_tool_list_still_reach_ecg_and_moods(tmp_data_dir: Path, monkeypatch):
    # Claude's connector may keep the tool list from before get_ecg and
    # get_state_of_mind existed; list_metrics, get_metric and get_summary
    # lead to the same data.
    shutil.copy(FIXTURES / "sample_clean.csv", tmp_data_dir / "health_1.csv")
    server = _server(tmp_data_dir, monkeypatch)
    start = (datetime.now(TZ) - timedelta(days=1)).replace(second=0, microsecond=0)
    _write(Path(server.ECG_DIR), "a", {"start": _stamp(start), "classification": "Sinus Rhythm", "averageHeartRate": 61})
    _write(Path(server.STATE_OF_MIND_DIR), "m1", {
        "id": "m1", "start": start.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "kind": "momentary_emotion", "valence": 0.0, "valenceClassification": "neutral",
        "labels": ["tired"], "associations": ["hobbies"],
    })

    listing = server.list_metrics()
    assert "Step Count (count)" in listing
    assert f"ecg (ECG recordings): 1, latest {start:%Y-%m-%d %H:%M}" in listing
    assert f"state_of_mind (logged moods and emotions): 1, latest {start:%Y-%m-%d %H:%M}" in listing
    assert "heart_rate_notifications (high, low and irregular heart rate notifications): 0" in listing

    for name in ("ecg", "ECG", "心電圖"):
        assert json.loads(server.get_metric(name))["records"][0]["classification"] == "Sinus Rhythm"
    for name in ("state_of_mind", "State of Mind", "mood", "心情"):
        assert json.loads(server.get_metric(name, days=7))["records"][0]["labels"] == ["tired"]
    assert json.loads(server.get_metric("Heart Rate Notifications"))["records"] == []
    assert "not found" in server.get_metric("Blood Pressure")

    summary = server.get_summary(days=7)
    assert f'ECG recordings: 1, latest {start:%Y-%m-%d %H:%M} (details: get_metric("ecg"))' in summary
    assert 'logged moods and emotions: 1' in summary
    assert "heart rate notifications" not in summary
