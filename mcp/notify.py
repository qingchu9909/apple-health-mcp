"""Push notifications for the daily HEALTH REPORT.

Every channel whose credentials are present in the environment receives the
message; channels without credentials are skipped:

- Telegram:  TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
- Server酱:   SERVERCHAN_SENDKEY  (WeChat; both the Turbo ``SCT…`` and the
              Server酱³ ``sctp…`` key formats are supported)
- PushPlus:  PUSHPLUS_TOKEN      (WeChat)

Credentials are read at call time so a changed ``.env`` only needs a service
restart, and tests can set them with ``monkeypatch.setenv``.
"""
import html
import os
import re

import httpx

# Telegram rejects messages over 4096 characters; leave headroom for tags.
TELEGRAM_LIMIT = 4000
_TIMEOUT = httpx.Timeout(20.0)
_SERVERCHAN3_RE = re.compile(r"^sctp(\d+)t", re.IGNORECASE)


def configured_channels() -> list[str]:
    channels = []
    if os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"):
        channels.append("telegram")
    if os.getenv("SERVERCHAN_SENDKEY"):
        channels.append("serverchan")
    if os.getenv("PUSHPLUS_TOKEN"):
        channels.append("pushplus")
    return channels


def _inline_to_html(text: str) -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    return re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)


def markdown_to_telegram_html(markdown: str) -> str:
    """Convert the small Markdown subset the report uses into Telegram HTML.

    Headings become bold lines, ``**bold**`` stays bold, inline code stays
    code and ``-``/``*`` bullets become ``•``. Everything else is escaped, so
    Telegram's strict HTML parser never sees a stray ``<`` or ``&``.
    """
    lines = []
    for line in markdown.splitlines():
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.*)$", line)
        if heading:
            lines.append(f"<b>{_inline_to_html(heading.group(1))}</b>")
            continue
        bullet = re.match(r"^(\s*)[-*]\s+(.*)$", line)
        if bullet:
            lines.append(f"{bullet.group(1)}• {_inline_to_html(bullet.group(2))}")
            continue
        lines.append(_inline_to_html(line))
    return "\n".join(lines)


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split on line boundaries into chunks of at most ``limit`` characters."""
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current.strip():
        chunks.append(current)
    return chunks


def _strip_tags(chunk: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", chunk))


def send_telegram(client: httpx.Client, title: str, body: str) -> str:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    # A report whose first line is already its title gets no second copy of it.
    first_line = body.lstrip().split("\n", 1)[0]
    has_title = bool(title) and first_line.startswith("#") and first_line.lstrip("#").strip() == title.strip()
    text = markdown_to_telegram_html(f"# {title}\n\n{body}" if title and not has_title else body)
    for chunk in split_message(text):
        payload = {"chat_id": chat_id, "text": chunk, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        response = client.post(url, json=payload)
        if response.status_code == 400:
            # Telegram rejected the markup; the plain text still gets through.
            response = client.post(url, json={"chat_id": chat_id, "text": _strip_tags(chunk)})
        if response.status_code != 200:
            return f"failed: HTTP {response.status_code} {response.text[:200]}"
    return "ok"


def send_serverchan(client: httpx.Client, title: str, body: str) -> str:
    key = os.environ["SERVERCHAN_SENDKEY"]
    v3 = _SERVERCHAN3_RE.match(key)
    url = (
        f"https://{v3.group(1)}.push.ft07.com/send/{key}.send"
        if v3
        else f"https://sctapi.ftqq.com/{key}.send"
    )
    # Server酱 caps the title at 32 characters; the body is Markdown.
    response = client.post(url, data={"title": (title or "Health report")[:32], "desp": body})
    if response.status_code == 200 and response.json().get("code") == 0:
        return "ok"
    return f"failed: HTTP {response.status_code} {response.text[:200]}"


def send_pushplus(client: httpx.Client, title: str, body: str) -> str:
    payload = {"token": os.environ["PUSHPLUS_TOKEN"], "title": title or "Health report",
               "content": body, "template": "markdown"}
    response = client.post("https://www.pushplus.plus/send", json=payload)
    if response.status_code == 200 and response.json().get("code") == 200:
        return "ok"
    return f"failed: HTTP {response.status_code} {response.text[:200]}"


_SENDERS = {"telegram": send_telegram, "serverchan": send_serverchan, "pushplus": send_pushplus}


def send_all(title: str, body: str, client: httpx.Client | None = None) -> dict[str, str]:
    """Send ``body`` to every configured channel; returns ``{channel: status}``."""
    channels = configured_channels()
    if not channels:
        return {}
    own_client = client is None
    client = client or httpx.Client(timeout=_TIMEOUT)
    results: dict[str, str] = {}
    try:
        for channel in channels:
            try:
                results[channel] = _SENDERS[channel](client, title, body)
            except (httpx.HTTPError, ValueError) as exc:
                results[channel] = f"failed: {type(exc).__name__}: {exc}"[:300]
    finally:
        if own_client:
            client.close()
    return results
