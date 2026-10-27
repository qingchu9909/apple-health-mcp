"""Tests for the ingest service (ingest/main.py)."""
import importlib.util
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from .conftest import FIXTURES, REPO_ROOT, TEST_TOKEN

AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}


def _load(tmp_path: Path, monkeypatch, token: str = TEST_TOKEN):
    monkeypatch.setenv("HEALTH_TOKEN", token)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WORKOUTS_DIR", str(tmp_path / "workouts"))
    monkeypatch.setenv("SLEEP_DIR", str(tmp_path / "sleep"))
    monkeypatch.setenv("ECG_DIR", str(tmp_path / "ecg"))
    monkeypatch.setenv("HR_NOTIFICATIONS_DIR", str(tmp_path / "hr-notifications"))
    monkeypatch.setenv("STATE_OF_MIND_DIR", str(tmp_path / "state-of-mind"))
    spec = importlib.util.spec_from_file_location("ingest_main", REPO_ROOT / "ingest" / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, TestClient(module.app)


def test_refuses_to_start_with_a_weak_token(tmp_path: Path, monkeypatch):
    with pytest.raises(SystemExit):
        _load(tmp_path, monkeypatch, token="short")


def test_rejects_missing_or_wrong_token(tmp_path: Path, monkeypatch):
    _, client = _load(tmp_path, monkeypatch)
    assert client.post("/ingest", content=b"x").status_code == 401
    assert client.post("/ingest", content=b"x", headers={"Authorization": "Bearer None"}).status_code == 401
    assert client.post("/ingest", content=b"x", headers={"Authorization": "Bearer"}).status_code == 401
    wrong = {"Authorization": f"Bearer {TEST_TOKEN[:-1]}"}
    assert client.post("/ingest", content=b"x", headers=wrong).status_code == 401


@pytest.mark.parametrize(
    "value",
    [
        f"bearer {TEST_TOKEN}",
        f"  Bearer {TEST_TOKEN} ",
        f"Bearer {TEST_TOKEN}",
        f"Bearer {TEST_TOKEN}​",
        f"`Bearer {TEST_TOKEN}`",
        f"Header 值：Bearer {TEST_TOKEN}",
        f"Header 值：{TEST_TOKEN}",
        f"Bearer {TEST_TOKEN[:38]} {TEST_TOKEN[38:]}",
        TEST_TOKEN,
    ],
)
def test_accepts_the_token_despite_copy_paste_slips(tmp_path: Path, monkeypatch, value):
    _, client = _load(tmp_path, monkeypatch)
    # Raw UTF-8 bytes, as a phone sends a pasted value.
    response = client.post("/ingest", content=b"", headers={"Authorization": value.encode()})
    assert response.status_code == 200


def test_logs_why_a_request_was_refused(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("REPORT_API_TOKEN", "r" * 64)
    _, client = _load(tmp_path, monkeypatch)

    client.post("/ingest", content=b"")
    assert "no Authorization header" in capsys.readouterr().out

    client.post("/ingest", content=b"", headers={"Authorization": "Bearer " + "r" * 64})
    assert "REPORT_API_TOKEN" in capsys.readouterr().out

    client.post("/ingest", content=b"", headers={"Authorization": f"Bearer {TEST_TOKEN[:-1]}"})
    out = capsys.readouterr().out
    assert "got 63 characters, expected 64" in out
    assert TEST_TOKEN[:-1] not in out

    client.post("/ingest", content=b"", headers={"Authorization": "Bearer abc def"})
    assert "got 6 characters in 2 pieces, expected 64" in capsys.readouterr().out


def test_multipart_csv_is_unwrapped_and_never_overwritten(tmp_path: Path, monkeypatch):
    module, client = _load(tmp_path, monkeypatch)
    body = (FIXTURES / "sample_wrapped.csv").read_bytes().replace(b"\n", b"\r\n")
    headers = {**AUTH, "Content-Type": "multipart/form-data; boundary=Boundary-TEST1234"}
    first = client.post("/ingest", content=body, headers=headers)
    second = client.post("/ingest", content=body, headers=headers)
    assert first.status_code == second.status_code == 200
    files = sorted((tmp_path / "data").glob("health_*.csv"))
    assert len(files) == 2
    assert files[0].read_bytes().startswith(b"Date/Time,")
    assert not files[0].read_bytes().rstrip().endswith(b"--")


def test_json_workouts_are_stored_per_id_without_routes(tmp_path: Path, monkeypatch):
    _, client = _load(tmp_path, monkeypatch)
    payload = {
        "data": {
            "workouts": [
                {"id": "AB-12", "name": "Hiking", "start": "2026-04-01 07:00:00 +0800",
                 "route": [{"latitude": 1, "longitude": 2}]},
                {"id": "../../etc/passwd", "name": "Swim", "start": "2026-04-02 07:00:00 +0800"},
                {"name": "No id", "start": "2026-04-03 07:00:00 +0800", "end": "2026-04-03 08:00:00 +0800"},
            ]
        }
    }
    response = client.post("/ingest", json=payload, headers=AUTH)
    assert response.json() == {"status": "saved", "workouts": 3}

    stored = {p.name: json.loads(p.read_text()) for p in (tmp_path / "workouts").glob("*.json")}
    assert "AB-12.json" in stored and "route" not in stored["AB-12.json"]
    assert "etcpasswd.json" in stored  # path characters stripped
    assert len(stored) == 3

    # Re-sending the same workouts overwrites instead of duplicating.
    client.post("/ingest", json=payload, headers=AUTH)
    assert len(list((tmp_path / "workouts").glob("*.json"))) == 3


def test_empty_workout_export_is_accepted(tmp_path: Path, monkeypatch):
    _, client = _load(tmp_path, monkeypatch)
    response = client.post("/ingest", json={"data": {"workouts": []}}, headers=AUTH)
    assert response.status_code == 200
    assert response.json()["status"] == "empty"


def test_json_sleep_nights_are_stored_by_the_morning_they_ended_on(tmp_path: Path, monkeypatch, capsys):
    _, client = _load(tmp_path, monkeypatch)
    payload = {
        "data": {
            "metrics": [
                {"name": "sleep_analysis", "units": "hr", "data": [
                    {"date": "2026-10-27", "sleepStart": "2026-10-26 23:12:00 +0800",
                     "sleepEnd": "2026-10-27 06:50:00 +0800", "totalSleep": 7.2},
                    {"date": "2026-10-26 00:00:00 +0800", "sleepEnd": "2026-10-26 07:01:00 +0800", "totalSleep": 6.9},
                    {"totalSleep": 1},  # no date at all
                ]},
                {"name": "step_count", "units": "count", "data": [{"date": "2026-10-27 08:00:00 +0800", "qty": 100}]},
            ]
        }
    }
    response = client.post("/ingest", json=payload, headers=AUTH)
    assert response.json() == {"status": "saved", "workouts": 0, "nights": 2}
    stored = {p.name: json.loads(p.read_text()) for p in (tmp_path / "sleep").glob("*.json")}
    assert set(stored) == {"2026-10-27.json", "2026-10-26.json"}
    assert stored["2026-10-27.json"]["sleepStart"] == "2026-10-26 23:12:00 +0800"
    # Other JSON metrics are ignored without a word, since nights were found.
    assert "step_count" not in capsys.readouterr().out

    # Re-sending the same nights overwrites instead of duplicating.
    client.post("/ingest", json=payload, headers=AUTH)
    assert len(list((tmp_path / "sleep").glob("*.json"))) == 2


def test_the_sleep_automation_may_send_every_metric(tmp_path: Path, monkeypatch, capsys):
    _, client = _load(tmp_path, monkeypatch)
    others = [{"name": f"metric_{i}", "units": "count", "data": []} for i in range(8)]
    night = {"date": "2026-10-27", "sleepEnd": "2026-10-27 06:50:00 +0800", "totalSleep": 7.2}
    payload = {"data": {"metrics": [*others, {"name": "sleep_analysis", "units": "hr", "data": [night]}]}}
    assert client.post("/ingest", json=payload, headers=AUTH).json() == {"status": "saved", "workouts": 0, "nights": 1}
    assert "ignoring" not in capsys.readouterr().out

    # A day without sleep data is not an error either.
    payload = {"data": {"metrics": [*others, {"name": "sleep_analysis", "units": "hr", "data": []}]}}
    response = client.post("/ingest", json=payload, headers=AUTH)
    assert response.status_code == 200
    assert "metric_0, metric_1, metric_2, metric_3, metric_4 and 3 more" in capsys.readouterr().out


def test_sleep_segments_are_ignored_with_a_hint(tmp_path: Path, monkeypatch, capsys):
    _, client = _load(tmp_path, monkeypatch)
    segment = {"startDate": "2026-10-26 23:12:00 +0800", "endDate": "2026-10-26 23:40:00 +0800",
               "qty": 0.47, "value": "Core"}
    payload = {"data": {"metrics": [{"name": "sleep_analysis", "units": "hr", "data": [segment]}]}}
    response = client.post("/ingest", json=payload, headers=AUTH)
    assert response.status_code == 422
    assert "Summarize Data" in response.json()["detail"]
    assert "turn on Summarize Data" in capsys.readouterr().out
    assert not list((tmp_path / "sleep").iterdir())


def test_ecg_recordings_are_stored_without_voltages(tmp_path: Path, monkeypatch):
    _, client = _load(tmp_path, monkeypatch)
    ecg = {
        "start": "2026-10-20 14:30:00 +0800", "end": "2026-10-20 14:30:30 +0800",
        "classification": "Sinus Rhythm", "averageHeartRate": 62, "samplingFrequency": 512,
        "numberOfVoltageMeasurements": 15360,
        "voltageMeasurements": [{"date": "2026-10-20 14:30:00.000 +0800", "voltage": 0.0001, "units": "V"}] * 3,
    }
    response = client.post("/ingest", json={"data": {"ecg": [ecg]}}, headers=AUTH)
    assert response.json() == {"status": "saved", "workouts": 0, "ecg": 1}
    stored = json.loads((tmp_path / "ecg" / "20261020_143000.json").read_text())
    assert stored["classification"] == "Sinus Rhythm"
    assert "voltageMeasurements" not in stored

    client.post("/ingest", json={"data": {"ecg": [ecg]}}, headers=AUTH)
    assert len(list((tmp_path / "ecg").iterdir())) == 1


def test_heart_rate_notifications_are_stored(tmp_path: Path, monkeypatch):
    _, client = _load(tmp_path, monkeypatch)
    note = {"start": "2026-10-21 03:10:00 +0800", "end": "2026-10-21 03:20:00 +0800", "threshold": 120,
            "heartRate": [{"hr": 131, "units": "bpm"}], "heartRateVariation": []}
    response = client.post("/ingest", json={"data": {"heartRateNotifications": [note]}}, headers=AUTH)
    assert response.json() == {"status": "saved", "workouts": 0, "heart_rate_notifications": 1}
    assert json.loads((tmp_path / "hr-notifications" / "20261021_031000.json").read_text()) == note


def test_state_of_mind_entries_are_stored_by_id(tmp_path: Path, monkeypatch):
    _, client = _load(tmp_path, monkeypatch)
    entry = {"id": "AB12-CD", "start": "2026-10-26T13:00:00Z", "end": "2026-10-26T13:00:00Z",
             "kind": "daily_mood", "valence": -0.4, "valenceClassification": "slightly_unpleasant",
             "labels": ["stressed"], "associations": ["work"]}
    response = client.post("/ingest", json={"data": {"stateOfMind": [entry]}}, headers=AUTH)
    assert response.json() == {"status": "saved", "workouts": 0, "state_of_mind": 1}
    assert json.loads((tmp_path / "state-of-mind" / "AB12-CD.json").read_text()) == entry


def test_the_service_may_write_every_directory_it_uses():
    # systemd's ProtectSystem=strict makes everything else read-only.
    unit = (REPO_ROOT / "deploy" / "systemd" / "health-ingest.service").read_text()
    writable = next(line for line in unit.splitlines() if line.startswith("ReadWritePaths="))
    install = (REPO_ROOT / "deploy" / "install.sh").read_text()
    source = (REPO_ROOT / "ingest" / "main.py").read_text()
    for name in re.findall(r'os\.getenv\("(\w+_DIR)"', source):
        folder = re.search(rf'set_env {name} "\$BASE/([\w-]+)"', install)
        assert folder, f"install.sh does not set {name}"
        assert f"@BASE@/{folder.group(1)}" in writable.split("=", 1)[1].split(), name


def test_json_health_metrics_are_rejected_with_a_hint(tmp_path: Path, monkeypatch):
    _, client = _load(tmp_path, monkeypatch)
    response = client.post("/ingest", json={"data": {"metrics": [{"name": "step_count"}]}}, headers=AUTH)
    assert response.status_code == 422
    assert "CSV" in response.json()["detail"]


def test_invalid_json_is_a_400(tmp_path: Path, monkeypatch):
    _, client = _load(tmp_path, monkeypatch)
    headers = {**AUTH, "Content-Type": "application/json"}
    assert client.post("/ingest", content=b"{oops", headers=headers).status_code == 400


def test_empty_csv_upload_writes_nothing(tmp_path: Path, monkeypatch):
    _, client = _load(tmp_path, monkeypatch)
    body = b"--Boundary-X\r\nContent-Type: text/csv\r\n\r\n\r\n--Boundary-X--\r\n"
    response = client.post("/ingest", content=body, headers={**AUTH, "Content-Type": "multipart/form-data"})
    assert response.status_code == 200
    assert response.json() == {"status": "empty"}
    assert not list((tmp_path / "data").iterdir())


def test_no_temporary_files_are_left_behind(tmp_path: Path, monkeypatch):
    _, client = _load(tmp_path, monkeypatch)
    client.post("/ingest", content=b"Date/Time,Step Count (count)\n2026-04-01 00:00:00,5\n", headers=AUTH)
    client.post("/ingest", json={"data": {"workouts": [{"id": "W1", "start": "2026-04-01 08:00:00 +0000"}]}}, headers=AUTH)
    leftovers = [p.name for p in tmp_path.rglob(".*.tmp")]
    assert leftovers == []
    assert len(list((tmp_path / "data").glob("health_*.csv"))) == 1
    assert (tmp_path / "workouts" / "W1.json").exists()


def test_any_multipart_boundary_is_unwrapped(tmp_path: Path, monkeypatch):
    _, client = _load(tmp_path, monkeypatch)
    body = (
        b"--Xyz123\r\nContent-Disposition: form-data; name=\"f\"; filename=\"a.csv\"\r\n"
        b"Content-Type: text/csv\r\n\r\nDate/Time,Step Count (count)\r\n2026-04-01 00:00:00,5\r\n--Xyz123--\r\n"
    )
    client.post("/ingest", content=body, headers={**AUTH, "Content-Type": "multipart/form-data; boundary=Xyz123"})
    saved = next((tmp_path / "data").glob("health_*.csv")).read_bytes()
    assert saved == b"Date/Time,Step Count (count)\r\n2026-04-01 00:00:00,5"
