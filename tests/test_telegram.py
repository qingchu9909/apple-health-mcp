"""Tests for the Telegram commands: what the bot answers and whom it answers."""
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from .conftest import _import_server

TODAY = date(2026, 11, 10)
NOW = datetime(2026, 11, 10, 15, 40)
CHAT_ID = "4242"
BOT_TOKEN = "123456:secret-token"


def _csv(rows: list[tuple[str, dict]]) -> str:
    columns = sorted({col for _, values in rows for col in values})
    lines = ["Date/Time," + ",".join(columns)]
    lines += [when + "," + ",".join(str(values.get(col, "")) for col in columns) for when, values in rows]
    return "\n".join(lines) + "\n"


def _night(server, day: date, start: str = "23:30", end: str = "07:10") -> None:
    (Path(server.SLEEP_DIR) / f"{day}.json").write_text(json.dumps({
        "date": str(day), "sleepStart": f"{day - timedelta(days=1)} {start}:00 +0800",
        "sleepEnd": f"{day} {end}:00 +0800", "totalSleep": 7.2,
    }))


@pytest.fixture
def server(tmp_data_dir: Path, monkeypatch):
    """Eight ordinary nights, last night, and this morning's walk and ride."""
    monkeypatch.setenv("USER_TZ", "Asia/Shanghai")
    server = _import_server(tmp_data_dir)
    monkeypatch.setattr(server, "_now", lambda: NOW)
    rows = []
    for back in range(8, -1, -1):
        day = TODAY - timedelta(days=back)
        rows.append((f"{day} 00:00:00", {
            "Sleep Analysis [Total] (hr)": 7.2 if back else 6.5, "Sleep Analysis [Deep] (hr)": 1.0,
            "Sleep Analysis [REM] (hr)": 1.5, "Sleep Analysis [Core] (hr)": 4.0 if back else 3.75,
            "Sleep Analysis [Awake] (hr)": 0.25,
        }))
        rows += [(f"{day} 0{h}:15:00", {"Heart Rate Variability (ms)": 55}) for h in range(1, 7)]
        rows.append((f"{day} 07:40:00", {"Resting Heart Rate (count/min)": 58}))
        _night(server, day)
    rows += [
        (f"{TODAY} 09:00:00", {"Step Count (count)": 3000, "Active Energy (kJ)": 500,
                               "Apple Exercise Time (min)": 20, "Walking + Running Distance (km)": 2.2}),
        (f"{TODAY} 15:30:00", {"Step Count (count)": 2500, "Active Energy (kJ)": 336.8,
                               "Apple Exercise Time (min)": 5, "Walking + Running Distance (km)": 1.9}),
    ]
    (tmp_data_dir / "health_1.csv").write_text(_csv(rows))
    (Path(server.WORKOUTS_DIR) / "ride.json").write_text(json.dumps({
        "id": "ride", "name": "Outdoor Cycling", "start": f"{TODAY} 08:30:00 +0800",
        "end": f"{TODAY} 08:48:00 +0800", "duration": 1080, "distance": {"qty": 4.04, "units": "km"},
        "heartRate": {"avg": {"qty": 118.4, "units": "bpm"}},
    }))
    return server


def test_today_gives_readiness_activity_and_workouts(server):
    reply = server.telegram_answer("今天怎么样")
    assert reply.startswith("**今天 11月10日**\n🟢 状态正常，照常练\n中等强度就好")
    assert "- 昨晚睡了 6 小时 30 分（近两周中位数 7 小时 12 分）" in reply
    assert "- 步数 5,500\n- 走路和跑步 4.1 公里\n- 活动能量 837 kJ（约 200 千卡）\n- 锻炼 25 分钟" in reply
    assert "- 08:30 户外骑行 18 分钟，4.0 公里，平均心率 118" in reply
    assert reply.endswith("数据截至 15:30。")
    assert server.telegram_answer("/today@diver_health_bot") == reply


def test_today_asks_for_a_sync_when_the_data_is_old(server, monkeypatch):
    monkeypatch.setattr(server, "_now", lambda: NOW + timedelta(days=1))
    reply = server.telegram_answer("/today")
    assert "今天还没有活动数据。" in reply
    assert reply.endswith("⚠️ 数据只到昨天 15:30：解锁一下 iPhone，让 Health Auto Export 同步。")


def test_sleep_gives_last_night_or_says_it_is_missing(server, monkeypatch):
    reply = server.telegram_answer("昨晚睡得好吗")
    assert reply.splitlines() == [
        "**11月10日早上醒的那一晚**",
        "- 23:30 入睡，07:10 醒，睡着 6 小时 30 分（近两周中位数 7 小时 12 分）",
        "- 深睡 1 小时，REM 1 小时 30 分，核心 3 小时 45 分，清醒 15 分钟",
        "- 夜间 HRV 55 ms",
    ]
    monkeypatch.setattr(server, "_now", lambda: NOW + timedelta(days=1))
    reply = server.telegram_answer("/sleep")
    assert reply.startswith("昨晚的睡眠还没传上来")
    assert "**11月10日早上醒的那一晚**" in reply


def test_reports_come_back_by_kind_or_newest(server):
    reports = Path(server.REPORTS_DIR)
    assert server.telegram_answer("早报") == "还没有早报。"
    assert server.telegram_answer("/report") == "还没有报告。"
    (reports / f"{TODAY - timedelta(days=1)}-morning.md").write_text("# 早报 · 11月9日")
    assert server.telegram_answer("早报") == "今天的早报还没写，这是 11月9日的。\n\n# 早报 · 11月9日"
    (reports / f"{TODAY}-morning.md").write_text("# 早报 · 11月10日")
    (reports / f"{TODAY - timedelta(days=1)}-weekly.md").write_text("# 周报")
    os.utime(reports / f"{TODAY}-morning.md", (1, 1))  # older than the weekly one
    assert server.telegram_answer("今天的早报") == "# 早报 · 11月10日"
    assert server.telegram_answer("周报") == "# 周报"
    assert server.telegram_answer("/report") == "# 周报"


def test_anything_else_gets_the_list_of_commands(server):
    assert server.telegram_answer("/start") == server._TELEGRAM_HELP
    assert server.telegram_answer("帮助") == server._TELEGRAM_HELP
    reply = server.telegram_answer("你好")
    assert reply.startswith("这句我答不了")
    assert reply.endswith(server._TELEGRAM_HELP)
    assert server.telegram_answer("") == reply  # a sticker or a photo


def test_answers_without_any_data(tmp_data_dir: Path, monkeypatch):
    monkeypatch.setenv("USER_TZ", "Asia/Shanghai")
    server = _import_server(tmp_data_dir)
    assert "服务器还没有数据" in server.telegram_answer("今天")
    assert server.telegram_answer("睡眠").startswith("最近两周没有睡眠数据")


# --- Polling ---------------------------------------------------------------------


@pytest.fixture
def bot(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT_ID)
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp"))
    import telegram_bot  # noqa: PLC0415
    return telegram_bot


def _telegram(updates: list[dict], requests: list[tuple[str, dict]]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[1]
        requests.append((method, json.loads(request.content)))
        result = updates if method == "getUpdates" else {"message_id": 1}
        return httpx.Response(200, json={"ok": True, "result": result})
    return httpx.Client(transport=httpx.MockTransport(handler))


def _update(update_id: int, chat: int | str, text: str | None, sent: float) -> dict:
    message = {"message_id": update_id, "chat": {"id": int(chat)}, "date": int(sent)}
    if text is not None:
        message["text"] = text
    return {"update_id": update_id, "message": message}


def test_only_the_users_fresh_messages_are_answered(bot):
    now = 1_800_000_000
    updates = [
        _update(7, "999", "今天", now),  # a stranger
        _update(8, CHAT_ID, "睡眠", now - bot.STALE_S - 1),  # sent while the server was down
        _update(9, CHAT_ID, "今天", now - 5),
        _update(10, CHAT_ID, None, now),  # a sticker
    ]
    requests = []
    offset = bot.poll_once(_telegram(updates, requests), 7, lambda text: f"answer to {text!r}", now=lambda: now)
    assert offset == 11
    assert requests[0] == ("getUpdates", {"timeout": bot.POLL_S, "allowed_updates": ["message"], "offset": 7})
    assert [(method, body["chat_id"], body["text"]) for method, body in requests[1:]] == [
        ("sendMessage", CHAT_ID, "answer to '今天'"),
        ("sendMessage", CHAT_ID, "answer to ''"),
    ]


def test_a_failing_answer_is_apologised_for_and_not_fetched_again(bot, capsys):
    def broken(text):
        raise ValueError("no data")

    requests = []
    offset = bot.poll_once(_telegram([_update(3, CHAT_ID, "今天", 100)], requests), None, broken, now=lambda: 100)
    assert offset == 4
    assert "offset" not in requests[0][1]
    assert requests[1][1]["text"] == "这次没查出来，过一会儿再试试。"
    assert "answer failed: ValueError: no data" in capsys.readouterr().out


def test_errors_never_print_the_token(bot):
    assert bot._redact(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates") == \
        "https://api.telegram.org/bot<token>/getUpdates"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"ok": False, "description": "Conflict: terminated by other getUpdates"})

    with pytest.raises(bot.TelegramError, match="getUpdates: HTTP 409 Conflict"):
        bot.poll_once(httpx.Client(transport=httpx.MockTransport(handler)), None, str)


def test_the_bot_starts_only_when_switched_on_and_configured(server, bot, monkeypatch):
    started = []
    monkeypatch.setattr(server.threading, "Thread", lambda **kw: started.append(kw) or type(
        "T", (), {"start": lambda self: None})())
    server.start_telegram_bot()
    assert [kw["name"] for kw in started] == ["telegram"]
    monkeypatch.setattr(server, "TELEGRAM_COMMANDS", False)
    server.start_telegram_bot()
    monkeypatch.setattr(server, "TELEGRAM_COMMANDS", True)
    monkeypatch.delenv("TELEGRAM_CHAT_ID")
    server.start_telegram_bot()
    assert len(started) == 1
