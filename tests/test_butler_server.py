"""Tests for the server side of the butler: the alert pass, the morning-report
status, reports of each kind, and today's readiness in the chat tools."""
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from .conftest import REPO_ROOT, _import_server

API_TOKEN = "a" * 64
AUTH = {"Authorization": f"Bearer {API_TOKEN}"}
TODAY = date(2026, 11, 10)
YESTERDAY = TODAY - timedelta(days=1)


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime.combine(TODAY, datetime.min.time()).replace(hour=hour, minute=minute)


def _csv(rows: list[tuple[str, dict]]) -> str:
    columns = sorted({col for _, values in rows for col in values})
    lines = ["Date/Time," + ",".join(columns)]
    lines += [when + "," + ",".join(str(values.get(col, "")) for col in columns) for when, values in rows]
    return "\n".join(lines) + "\n"


def _server(tmp_data_dir: Path, monkeypatch, now: datetime, resting_today: float | None = 58.0,
            resting_yesterday: float = 58.0):
    """30 ordinary days of resting heart rate (yesterday's can differ), 8 ordinary nights, then today."""
    monkeypatch.setenv("USER_TZ", "Asia/Shanghai")
    monkeypatch.setenv("REPORT_API_TOKEN", API_TOKEN)
    server = _import_server(tmp_data_dir)
    monkeypatch.setattr(server, "_now", lambda: now)
    rows = []
    for back in range(30, 0, -1):
        day = TODAY - timedelta(days=back)
        rows.append((f"{day} 07:40:00", {"Resting Heart Rate (count/min)": resting_yesterday if back == 1 else 58}))
        if back <= 8:
            rows.append((f"{day} 00:00:00", {"Sleep Analysis [Total] (hr)": 7.2}))
            rows += [(f"{day} 0{h}:15:00", {"Heart Rate Variability (ms)": 60}) for h in range(1, 7)]
            _write_night(server, day)
    if resting_today is not None:
        rows.append((f"{TODAY} 07:40:00", {"Resting Heart Rate (count/min)": resting_today}))
    (tmp_data_dir / "health_1.csv").write_text(_csv(rows))
    return server


def _write_night(server, day: date) -> None:
    (Path(server.SLEEP_DIR) / f"{day}.json").write_text(json.dumps({
        "date": str(day), "sleepStart": f"{day - timedelta(days=1)} 23:30:00 +0800",
        "sleepEnd": f"{day} 07:10:00 +0800", "totalSleep": 7.2,
    }))


def _pushes(server, monkeypatch, result: str = "ok") -> list:
    sent = []
    monkeypatch.setattr(server.notify, "configured_channels", lambda: ["telegram"])
    monkeypatch.setattr(server.notify, "send_all", lambda title, body: sent.append((title, body)) or {"telegram": result})
    return sent


def _client(server) -> TestClient:
    return TestClient(Starlette(routes=[
        Route("/api/bundle", server.api_bundle, methods=["GET"]),
        Route("/api/report", server.api_report, methods=["POST"]),
        Route("/api/report-status", server.api_report_status, methods=["GET"]),
    ]))


def test_each_alert_goes_out_once_and_waits_out_the_quiet_hours(tmp_data_dir: Path, monkeypatch):
    server = _server(tmp_data_dir, monkeypatch, _at(2), resting_yesterday=67)
    sent = _pushes(server, monkeypatch)

    server.butler_tick()  # 02:00: only the welcome, which answers the user's own update
    assert [title for title, _ in sent] == ["健康管家提醒已开启"]
    assert "23:00–08:00 不打扰" in sent[0][1]

    monkeypatch.setattr(server, "_now", lambda: _at(8, 5))
    server.butler_tick()
    assert sent[1][0] == "健康提醒：静息心率偏高"
    assert "高 9" in sent[1][1]

    server.butler_tick()
    assert len(sent) == 2
    state = json.loads((Path(server.BUTLER_DIR) / "state.json").read_text(encoding="utf-8"))
    assert set(state["sent"]) == {"welcome", f"{YESTERDAY}:resting_hr"}
    bundle = server.build_bundle(days=7)
    assert [entry["title"] for entry in bundle["alerts_sent"]] == ["健康提醒：静息心率偏高"]
    assert bundle["readiness"]["level"] == "rest"


def test_todays_resting_heart_rate_counts_only_once_it_has_settled(tmp_data_dir: Path, monkeypatch):
    """The watch revises today's value through the day: a high morning reading
    neither alerts nor sets the readiness until the evening."""
    server = _server(tmp_data_dir, monkeypatch, _at(9), resting_today=73)
    sent = _pushes(server, monkeypatch)
    server.butler_tick()
    assert [title for title, _ in sent] == ["健康管家提醒已开启"]
    signal = next(s for s in server.build_bundle(days=7)["readiness"]["signals"] if s["name"] == "resting_hr")
    assert signal["value"] == 58 and signal["text"].startswith("昨天静息心率")

    monkeypatch.setattr(server, "_now", lambda: _at(18, 5))
    server.butler_tick()
    assert sent[-1][0] == "健康提醒：静息心率偏高" and "今天静息心率 73" in sent[-1][1]


def test_a_push_no_channel_took_is_tried_again(tmp_data_dir: Path, monkeypatch):
    server = _server(tmp_data_dir, monkeypatch, _at(9), resting_yesterday=67)
    sent = _pushes(server, monkeypatch, result="failed: HTTP 502")
    server.butler_tick()
    assert len(sent) == 2
    sent = _pushes(server, monkeypatch)
    server.butler_tick()
    assert [title for title, _ in sent] == ["健康管家提醒已开启", "健康提醒：静息心率偏高"]


def test_nothing_is_pushed_while_the_state_cannot_be_saved(tmp_data_dir: Path, monkeypatch):
    server = _server(tmp_data_dir, monkeypatch, _at(9), resting_yesterday=67)
    Path(server.BUTLER_DIR).rmdir()
    Path(server.BUTLER_DIR).write_text("not a folder")
    sent = _pushes(server, monkeypatch)
    server.butler_tick()
    assert sent == []


def test_no_channel_or_alerts_off_means_no_pass(tmp_data_dir: Path, monkeypatch):
    server = _server(tmp_data_dir, monkeypatch, _at(9), resting_yesterday=67)
    monkeypatch.setattr(server.notify, "configured_channels", lambda: [])
    assert server.butler_tick() == []
    monkeypatch.setenv("BUTLER_ALERTS", "off")
    server = _server(tmp_data_dir, monkeypatch, _at(9), resting_yesterday=67)
    _pushes(server, monkeypatch)
    assert server.butler_tick() == []


def test_settings_that_do_not_parse_keep_the_defaults(tmp_data_dir: Path, monkeypatch):
    monkeypatch.setenv("BUTLER_QUIET_HOURS", "22:30 to 7")
    monkeypatch.setenv("MORNING_REPORT_HOURS", "7-11")
    server = _import_server(tmp_data_dir)
    assert server.QUIET_HOURS == (23, 8)
    assert server.MORNING_REPORT_HOURS == (7, 11)
    monkeypatch.setenv("BUTLER_QUIET_HOURS", "off")
    assert _import_server(tmp_data_dir).QUIET_HOURS is None


def test_the_morning_report_waits_for_last_nights_sleep(tmp_data_dir: Path, monkeypatch):
    server = _server(tmp_data_dir, monkeypatch, _at(7, 5))
    client = _client(server)

    def status(hour: int, minute: int = 5) -> dict:
        monkeypatch.setattr(server, "_now", lambda: _at(hour, minute))
        response = client.get("/api/report-status?kind=morning", headers=AUTH)
        assert response.status_code == 200
        return response.json()

    assert status(7)["due"] is False and "too early" in status(7)["reason"]
    assert status(9) == {"kind": "morning", "date": str(TODAY), "now": f"{TODAY} 09:05", "night_ready": False,
                         "due": False, "reason": status(9)["reason"]}
    assert status(12)["due"] is True and "still missing" in status(12)["reason"]
    _write_night(server, TODAY)
    assert status(9)["due"] is True and status(9)["night_ready"] is True

    assert client.post("/api/report", headers=AUTH, json={"content": "# 早报", "kind": "morning"}).status_code == 200
    assert status(10)["due"] is False and "already posted" in status(10)["reason"]
    evening = client.get("/api/report-status?kind=evening", headers=AUTH).json()
    assert evening["due"] is True and "night_ready" not in evening
    assert client.get("/api/report-status?kind=daily", headers=AUTH).status_code == 400


def test_the_morning_report_waits_for_both_uploads_of_the_night(tmp_data_dir: Path, monkeypatch):
    """The metric CSV and the sleep-times upload bring a night in separately,
    often an hour apart; the report waits up to two hours for the second."""
    def status(server, hour: int) -> dict:
        monkeypatch.setattr(server, "_now", lambda: _at(hour, 5))
        return _client(server).get("/api/report-status?kind=morning", headers=AUTH).json()

    # The CSV's stages come first: wait for the bedtime and wake time.
    server = _server(tmp_data_dir, monkeypatch, _at(8, 5))
    (tmp_data_dir / "health_2.csv").write_text(_csv([(f"{TODAY} 00:00:00", {"Sleep Analysis [Total] (hr)": 6.3})]))
    assert status(server, 8)["night_ready"] is True
    assert status(server, 9)["due"] is False and "only part" in status(server, 9)["reason"]
    assert status(server, 10)["due"] is True and "still missing at 10:00" in status(server, 10)["reason"]
    _write_night(server, TODAY)
    assert status(server, 8)["due"] is True and "has arrived" in status(server, 8)["reason"]

    # The times come first: wait for the CSV rows up to the wake time.
    server = _server(tmp_data_dir, monkeypatch, _at(8, 5), resting_today=None)
    (tmp_data_dir / "health_2.csv").unlink()
    _write_night(server, TODAY)
    assert status(server, 9)["night_ready"] is True and status(server, 9)["due"] is False
    assert status(server, 10)["due"] is True
    (tmp_data_dir / "health_2.csv").write_text(_csv([(f"{TODAY} 06:50:00", {"Heart Rate (count/min)": 70})]))
    assert status(server, 9)["due"] is False
    (tmp_data_dir / "health_3.csv").write_text(_csv([(f"{TODAY} 07:45:00", {"Heart Rate (count/min)": 72})]))
    assert status(server, 8)["due"] is True

    # A server that never got sleep times goes by the CSV's stages alone.
    for path in Path(server.SLEEP_DIR).glob("*.json"):
        path.unlink()
    (tmp_data_dir / "health_2.csv").write_text(_csv([(f"{TODAY} 00:00:00", {"Sleep Analysis [Total] (hr)": 6.3})]))
    assert status(server, 8)["due"] is True


def test_reports_of_each_kind_are_kept_apart(tmp_data_dir: Path, monkeypatch):
    server = _server(tmp_data_dir, monkeypatch, _at(20, 5))
    pushed = _pushes(server, monkeypatch)
    client = _client(server)
    post = lambda **body: client.post("/api/report", headers=AUTH, json=body)  # noqa: E731

    assert post(content="# 周报 上周", kind="weekly", date="2026-11-09", title="周报").status_code == 200
    assert post(content="# 早报", kind="morning", title="早报").status_code == 200
    assert post(content="# 晚报", kind="evening", title="晚报").status_code == 200
    again = post(content="# 晚报 又一份", kind="evening")
    assert again.status_code == 409 and "replace" in again.json()["error"]
    assert post(content="# 晚报 改过", kind="evening", replace=True, push=False).status_code == 200
    assert post(content="x", kind="yearly").status_code == 400
    assert [title for title, _ in pushed] == ["周报", "早报", "晚报"]

    reports = Path(server.REPORTS_DIR)
    assert sorted(p.name for p in reports.iterdir()) == [
        "2026-11-09-weekly.md", "2026-11-10-evening.md", "2026-11-10-morning.md",
    ]
    today = json.loads(server.get_health_report())
    assert today["date"] == str(TODAY)
    assert today["kinds"] == ["morning", "evening"]
    assert today["content"] == "# 早报\n\n---\n\n# 晚报 改过"
    assert today["latest_weekly"] == {"date": "2026-11-09", "content": "# 周报 上周"}
    assert today["latest_monthly"] is None

    bundle = client.get("/api/bundle?days=7", headers=AUTH).json()
    assert bundle["reports_today"] == {"morning": "# 早报", "evening": "# 晚报 改过"}
    assert bundle["latest_weekly"]["date"] == "2026-11-09"


def test_night_values_leave_out_the_midnight_daily_averages(tmp_data_dir: Path, monkeypatch):
    server = _server(tmp_data_dir, monkeypatch, _at(9))
    _write_night(server, TODAY)
    extra = _csv([
        (f"{TODAY} 00:00:00", {"Heart Rate Variability (ms)": 20}),  # a backfill's daily average
        *[(f"{TODAY} 0{h}:15:00", {"Heart Rate Variability (ms)": 70}) for h in range(1, 7)],
        (f"{TODAY} 09:00:00", {"Heart Rate Variability (ms)": 25}),  # awake
    ])
    (tmp_data_dir / "health_2.csv").write_text(extra)

    vitals = {rec["date"]: rec for rec in json.loads(server.get_daily_vitals(days=3))["records"]}
    assert vitals[str(TODAY)]["hrv_night_ms"] == 70.0
    assert vitals[str(TODAY - timedelta(days=1))]["hrv_night_ms"] == 60.0
    assert vitals[str(TODAY)]["hrv_ms"] < 70  # the daily mean mixes in the rest


def test_the_chat_tools_give_todays_readiness(tmp_data_dir: Path, monkeypatch):
    server = _server(tmp_data_dir, monkeypatch, _at(9))
    _write_night(server, TODAY)
    for name in ("readiness", "Readiness", "今天状态", "恢复"):
        ready = json.loads(server.get_metric(name))
        assert ready["date"] == str(TODAY)
        assert ready["level"] == "good"
    assert "Today's readiness: 🟢 状态不错，可以练强一点" in server.get_summary(days=7)
    assert "readiness (today's recovery and how hard to train, from last night): 🟢" in server.list_metrics()


def test_the_mcp_service_may_write_every_directory_it_writes():
    # systemd's ProtectSystem=strict makes everything else read-only.
    unit = (REPO_ROOT / "deploy" / "systemd" / "health-mcp.service").read_text()
    writable = next(line for line in unit.splitlines() if line.startswith("ReadWritePaths="))
    install = (REPO_ROOT / "deploy" / "install.sh").read_text()
    for name in ("DATA_DIR", "REPORTS_DIR", "BUTLER_DIR"):
        folder = re.search(rf'set_env {name} "\$BASE/([\w-]+)"', install)
        assert folder, f"install.sh does not set {name}"
        assert f"@BASE@/{folder.group(1)}" in writable.split("=", 1)[1].split(), name
        assert f'"$BASE/{folder.group(1)}"' in next(line for line in install.splitlines() if "install -d -m 750" in line)
