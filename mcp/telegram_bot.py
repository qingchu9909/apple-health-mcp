"""Answers the user's own Telegram messages with a few fixed commands.

Without this the bot only pushes. The server long-polls getUpdates and
replies to TELEGRAM_CHAT_ID only; messages from any other chat are ignored.
The replies come from the server's own data through ``answer``
(server.telegram_answer), with no AI behind them: the bot knows a handful of
words, not free questions.
"""
import os
import time

import httpx
import notify

# getUpdates holds the request open this long when nothing is new.
POLL_S = 50
_TIMEOUT = httpx.Timeout(POLL_S + 15.0)
# A message that waited longer than this (the server was down, or the
# commands were just switched on) is skipped rather than answered late.
STALE_S = 600
_RETRY_FIRST_S, _RETRY_MAX_S = 5, 300

# The menu Telegram shows next to the message box.
COMMANDS = [
    ("today", "今天的状态和到现在的活动"),
    ("sleep", "昨晚的睡眠"),
    ("report", "最近一份报告"),
    ("help", "能问什么"),
]


class TelegramError(Exception):
    pass


def configured() -> bool:
    return bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))


def _redact(text: str) -> str:
    """``text`` without the bot token, which sits in every API URL."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    return text.replace(token, "<token>") if token else text


def call(client: httpx.Client, method: str, **params):
    """One Bot API call; returns its ``result`` or raises TelegramError."""
    response = client.post(f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/{method}",
                           json=params)
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not body.get("ok"):
        raise TelegramError(f"{method}: HTTP {response.status_code} {body.get('description') or response.text[:200]}")
    return body["result"]


def poll_once(client: httpx.Client, offset: int | None, answer, now=time.time) -> int | None:
    """Wait for new messages, answer the user's, and return the next offset.

    The offset moves past every update, answered or not, so a message that
    makes ``answer`` fail is not fetched again and again.
    """
    params = {"timeout": POLL_S, "allowed_updates": ["message"]}
    if offset is not None:
        params["offset"] = offset
    chat_id = os.environ["TELEGRAM_CHAT_ID"].strip()
    for update in call(client, "getUpdates", **params):
        offset = update["update_id"] + 1
        message = update.get("message") or {}
        sender = str((message.get("chat") or {}).get("id", ""))
        if sender != chat_id:
            if sender:
                print(f"[telegram] ignored a message from chat {sender}, which is not TELEGRAM_CHAT_ID", flush=True)
            continue
        if now() - message.get("date", 0) > STALE_S:
            continue
        try:
            reply = answer(message.get("text") or "")
        except Exception as exc:  # one bad answer must not stop the others
            print(f"[telegram] answer failed: {type(exc).__name__}: {exc}", flush=True)
            reply = "这次没查出来，过一会儿再试试。"
        try:
            result = notify.send_telegram(client, "", reply)
        except httpx.HTTPError as exc:
            result = f"{type(exc).__name__}: {exc}"
        if result != "ok":
            print(f"[telegram] reply failed: {_redact(result)}", flush=True)
    return offset


def run(answer) -> None:
    """Answer the user's messages for as long as the server runs."""
    offset = None
    retry = _RETRY_FIRST_S
    with httpx.Client(timeout=_TIMEOUT) as client:
        try:
            call(client, "setMyCommands",
                 commands=[{"command": name, "description": text} for name, text in COMMANDS])
        except (httpx.HTTPError, TelegramError) as exc:  # only the menu is missing
            print(f"[telegram] cannot set the command menu: {_redact(str(exc))}", flush=True)
        while True:
            try:
                offset = poll_once(client, offset, answer)
                retry = _RETRY_FIRST_S
            except Exception as exc:  # Telegram unreachable, a webhook set, a second poller...
                print(f"[telegram] polling failed, trying again in {retry}s: "
                      f"{type(exc).__name__}: {_redact(str(exc))}", flush=True)
                time.sleep(retry)
                retry = min(retry * 2, _RETRY_MAX_S)
