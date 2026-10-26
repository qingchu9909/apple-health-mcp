"""Tests for mcp/notify.py (Telegram / Server酱 / PushPlus delivery)."""
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp"))
import notify  # noqa: E402


@pytest.fixture(autouse=True)
def _no_channels(monkeypatch):
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SERVERCHAN_SENDKEY", "PUSHPLUS_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_markdown_to_telegram_html_escapes_and_formats():
    md = "# 健康日报\n**恢复状态：绿** <b>raw</b> & more\n- 睡眠 7.2 h\n用 `get_metric`"
    assert notify.markdown_to_telegram_html(md) == (
        "<b>健康日报</b>\n"
        "<b>恢复状态：绿</b> &lt;b&gt;raw&lt;/b&gt; &amp; more\n"
        "• 睡眠 7.2 h\n"
        "用 <code>get_metric</code>"
    )


def test_split_message_respects_limit_and_keeps_text():
    text = "\n".join(f"line {i:02d} " + "x" * 40 for i in range(20))
    chunks = notify.split_message(text, limit=120)
    assert all(len(c) <= 120 for c in chunks)
    assert "\n".join(chunks) == text


def test_split_message_hard_splits_overlong_lines():
    chunks = notify.split_message("y" * 250, limit=100)
    assert [len(c) for c in chunks] == [100, 100, 50]


def test_send_all_without_channels_sends_nothing():
    def handler(request):
        raise AssertionError("no request expected")

    assert notify.send_all("t", "b", client=_client(handler)) == {}


def test_telegram_sends_html_then_falls_back_to_plain_text(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    seen = []

    def handler(request):
        payload = json.loads(request.content)
        seen.append(payload)
        if payload.get("parse_mode") == "HTML":
            return httpx.Response(400, json={"ok": False})
        return httpx.Response(200, json={"ok": True})

    result = notify.send_all("日报", "**绿**", client=_client(handler))
    assert result == {"telegram": "ok"}
    assert seen[0]["chat_id"] == "42"
    assert seen[0]["text"] == "<b>日报</b>\n\n<b>绿</b>"
    assert "parse_mode" not in seen[1]
    assert seen[1]["text"] == "日报\n\n绿"


def test_telegram_failure_is_reported(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    result = notify.send_all("t", "b", client=_client(lambda r: httpx.Response(403, text="Forbidden")))
    assert result["telegram"].startswith("failed: HTTP 403")


def test_serverchan_turbo_and_v3_urls(monkeypatch):
    urls = []

    def handler(request):
        urls.append(str(request.url))
        return httpx.Response(200, json={"code": 0})

    monkeypatch.setenv("SERVERCHAN_SENDKEY", "SCT1234abcd")
    assert notify.send_all("a" * 40, "body", client=_client(handler)) == {"serverchan": "ok"}
    monkeypatch.setenv("SERVERCHAN_SENDKEY", "sctp987tXYZ")
    assert notify.send_all("t", "body", client=_client(handler)) == {"serverchan": "ok"}
    assert urls == [
        "https://sctapi.ftqq.com/SCT1234abcd.send",
        "https://987.push.ft07.com/send/sctp987tXYZ.send",
    ]


def test_pushplus_payload(monkeypatch):
    monkeypatch.setenv("PUSHPLUS_TOKEN", "pp-token")
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"code": 200})

    assert notify.send_all("标题", "正文", client=_client(handler)) == {"pushplus": "ok"}
    assert seen == [{"token": "pp-token", "title": "标题", "content": "正文", "template": "markdown"}]


def test_network_errors_do_not_raise(monkeypatch):
    monkeypatch.setenv("PUSHPLUS_TOKEN", "pp-token")

    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    result = notify.send_all("t", "b", client=_client(handler))
    assert result["pushplus"].startswith("failed: ConnectError")
