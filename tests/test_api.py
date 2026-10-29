"""Tests for the /api routes used by the scheduled report routines."""
import json
from pathlib import Path

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from .conftest import FIXTURES, _import_server

API_TOKEN = "a" * 64


def _client(server) -> TestClient:
    app = Starlette(
        routes=[
            Route("/api/bundle", server.api_bundle, methods=["GET"]),
            Route("/api/report", server.api_report, methods=["POST"]),
        ]
    )
    return TestClient(app)


def _server(tmp_data_dir: Path, monkeypatch, token: str | None = API_TOKEN):
    if token is None:
        monkeypatch.delenv("REPORT_API_TOKEN", raising=False)
    else:
        monkeypatch.setenv("REPORT_API_TOKEN", token)
    (tmp_data_dir / "health_smart.csv").write_bytes((FIXTURES / "sample_smart.csv").read_bytes())
    return _import_server(tmp_data_dir)


def test_api_is_disabled_without_token(tmp_data_dir: Path, monkeypatch):
    client = _client(_server(tmp_data_dir, monkeypatch, token=None))
    assert client.get("/api/bundle").status_code == 503


def test_api_rejects_wrong_token(tmp_data_dir: Path, monkeypatch):
    client = _client(_server(tmp_data_dir, monkeypatch))
    assert client.get("/api/bundle").status_code == 401
    assert client.get("/api/bundle", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_bundle_contains_every_section(tmp_data_dir: Path, monkeypatch):
    client = _client(_server(tmp_data_dir, monkeypatch))
    response = client.get("/api/bundle?days=10000", headers={"Authorization": f"Bearer {API_TOKEN}"})
    assert response.status_code == 200
    bundle = response.json()
    for key in ("today", "freshness", "baselines", "daily_sleep", "daily_vitals", "daily_fitness", "workouts",
                "other_metrics"):
        assert key in bundle
    # days is clamped to 90, which still covers nothing from the fixed 2026-04 fixture
    # unless today is close to it; the structure is what matters here.
    assert bundle["daily_sleep"]["days_requested"] == 90
    assert bundle["freshness"]["latest_metric_row"].startswith("2026-04-03")
    assert bundle["report_7_days_ago"] is None


def test_bad_days_parameter(tmp_data_dir: Path, monkeypatch):
    client = _client(_server(tmp_data_dir, monkeypatch))
    response = client.get("/api/bundle?days=abc", headers={"Authorization": f"Bearer {API_TOKEN}"})
    assert response.status_code == 400


def test_post_report_saves_and_pushes(tmp_data_dir: Path, monkeypatch):
    server = _server(tmp_data_dir, monkeypatch)
    sent = []
    monkeypatch.setattr(server.notify, "send_all", lambda title, body: sent.append((title, body)) or {"telegram": "ok"})
    body = "# 健康日报\n\n**恢复状态：绿**"
    response = _client(server).post(
        "/api/report",
        headers={"Authorization": f"Bearer {API_TOKEN}"},
        json={"content": body, "title": "健康日报 2026-05-10", "date": "2026-05-10"},
    )
    assert response.status_code == 200
    assert response.json()["push"] == {"telegram": "ok"}
    assert sent == [("健康日报 2026-05-10", body)]
    assert (Path(server.REPORTS_DIR) / "2026-05-10.md").read_text(encoding="utf-8") == body
    # The saved report is what the routine will read back a week later.
    fetched = json.loads(server.get_health_report(date="2026-05-10"))
    assert fetched["content"] == body


def test_post_report_without_push(tmp_data_dir: Path, monkeypatch):
    server = _server(tmp_data_dir, monkeypatch)
    monkeypatch.setattr(server.notify, "send_all", lambda *a: (_ for _ in ()).throw(AssertionError("pushed")))
    response = _client(server).post(
        "/api/report",
        headers={"Authorization": f"Bearer {API_TOKEN}"},
        json={"content": "x", "date": "2026-05-11", "push": False},
    )
    assert response.status_code == 200
    assert "push" not in response.json()


def test_post_report_validation(tmp_data_dir: Path, monkeypatch):
    client = _client(_server(tmp_data_dir, monkeypatch))
    headers = {"Authorization": f"Bearer {API_TOKEN}"}
    assert client.post("/api/report", headers=headers, json={"content": "  "}).status_code == 400
    assert client.post("/api/report", headers=headers, json={"content": "x", "date": "../x"}).status_code == 400
    assert client.post("/api/report", headers=headers, json=["not", "an", "object"]).status_code == 400
    assert client.post("/api/report", headers=headers, content=b"not json").status_code == 400


def test_mcp_path_token_can_differ_from_ingest_token(tmp_data_dir: Path, monkeypatch):
    monkeypatch.setenv("MCP_PATH_TOKEN", "m" * 64)
    server = _import_server(tmp_data_dir)
    assert server.MCP_PATH_TOKEN == "m" * 64
    assert server.mcp.settings.streamable_http_path == f"/mcp/{'m' * 64}"


def test_push_crash_still_returns_the_saved_report(tmp_data_dir: Path, monkeypatch):
    server = _server(tmp_data_dir, monkeypatch)

    def boom(title, body):
        raise PermissionError("cannot read CA bundle")

    monkeypatch.setattr(server.notify, "send_all", boom)
    response = _client(server).post(
        "/api/report", json={"content": "# 日报"}, headers={"Authorization": f"Bearer {API_TOKEN}"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["saved"]["bytes"] > 0
    assert "PermissionError" in body["push"]["error"]
