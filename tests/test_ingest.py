"""Tests for the ingest service (ingest/main.py)."""
import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from .conftest import FIXTURES, REPO_ROOT, TEST_TOKEN

AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}


def _load(tmp_path: Path, monkeypatch, token: str = TEST_TOKEN):
    monkeypatch.setenv("HEALTH_TOKEN", token)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WORKOUTS_DIR", str(tmp_path / "workouts"))
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
