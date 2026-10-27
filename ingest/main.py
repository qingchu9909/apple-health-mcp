"""HTTPS endpoint that receives Apple Health exports from Health Auto Export.

Every automation posts to the same /ingest URL:

- Health Metrics as CSV, sent as a multipart body. We strip the MIME envelope
  and write the inner CSV to DATA_DIR.
- Workouts as JSON (export version 2). Each workout is written to
  WORKOUTS_DIR/<id>.json, so re-sent workouts overwrite instead of duplicating.
- Sleep Analysis alone as JSON: the CSV only has durations, the JSON also
  says when each night began and ended. Each night is written to
  SLEEP_DIR/<date it ended on>.json.
- ECG recordings, heart rate notifications and State of Mind entries as JSON,
  one automation each, written to ECG_DIR, HR_NOTIFICATIONS_DIR and
  STATE_OF_MIND_DIR as <id or start time>.json.
"""
import hashlib
import hmac
import json
import os
import pathlib
import re
import uuid
from datetime import datetime

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request

load_dotenv(os.environ.get("DOTENV_PATH", "/opt/health/.env"))

TOKEN = os.getenv("HEALTH_TOKEN")
if not TOKEN or len(TOKEN) < 32:
    raise SystemExit("HEALTH_TOKEN must be at least 32 characters (openssl rand -hex 32)")
DATA_DIR = pathlib.Path(os.getenv("DATA_DIR", "/opt/health/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
WORKOUTS_DIR = pathlib.Path(os.getenv("WORKOUTS_DIR", "/opt/health/workouts"))
WORKOUTS_DIR.mkdir(parents=True, exist_ok=True)
SLEEP_DIR = pathlib.Path(os.getenv("SLEEP_DIR", "/opt/health/sleep"))
SLEEP_DIR.mkdir(parents=True, exist_ok=True)
ECG_DIR = pathlib.Path(os.getenv("ECG_DIR", "/opt/health/ecg"))
ECG_DIR.mkdir(parents=True, exist_ok=True)
HR_NOTIFICATIONS_DIR = pathlib.Path(os.getenv("HR_NOTIFICATIONS_DIR", "/opt/health/hr-notifications"))
HR_NOTIFICATIONS_DIR.mkdir(parents=True, exist_ok=True)
STATE_OF_MIND_DIR = pathlib.Path(os.getenv("STATE_OF_MIND_DIR", "/opt/health/state-of-mind"))
STATE_OF_MIND_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI()


@app.get("/health")
def health_check():
    return {"status": "ok"}


def extract_csv(body: bytes) -> bytes:
    """Strip a multipart/form-data envelope when present.

    Health Auto Export sends data as multipart even though only one part is
    used. The CSV sits between the first blank line and the closing boundary
    (`--Boundary-...--`).
    """
    if body.startswith(b"--"):  # a CSV export always starts with its header row
        parts = body.split(b"\r\n\r\n", 1)
        if len(parts) == 2:
            csv_and_tail = parts[1]
            lines = csv_and_tail.rsplit(b"\r\n--", 1)
            return lines[0].strip()
    return body


# RFC 6750 token characters. Anything else (quotes, backticks, invisible or
# non-breaking spaces picked up while copying) only separates words.
_TOKEN_RE = re.compile(r"[A-Za-z0-9._~+/-]+=*")


def _token_pieces(header: str | None) -> list[str]:
    """The pieces of the token in an Authorization header.

    The phone app takes the header value from a free-text field, so accept
    "Bearer <token>" in any capitalisation or the bare token, ignoring stray
    spaces, quotes, backticks and invisible characters around it. A token
    copied across a line break arrives in several pieces.
    """
    words = _TOKEN_RE.findall(header or "")
    lowered = [w.lower() for w in words]
    if "bearer" in lowered:
        words = words[len(lowered) - lowered[::-1].index("bearer") :]
    return words


def _matches(pieces: list[str], secret: str) -> bool:
    candidates = {"".join(pieces), pieces[-1] if pieces else ""}
    return any(hmac.compare_digest(c.encode(), secret.encode()) for c in candidates)


def _authorized(header: str | None) -> bool:
    return _matches(_token_pieces(header), TOKEN)


def _rejection_reason(header: str | None) -> str:
    """Why a request was refused, for the service log. Never logs the token."""
    if not header:
        return "no Authorization header (the header name must be Authorization)"
    pieces = _token_pieces(header)
    for name in ("REPORT_API_TOKEN", "MCP_PATH_TOKEN"):
        other = os.getenv(name)
        if other and _matches(pieces, other):
            return f"the value is {name}; use the HEALTH_TOKEN line (Bearer ...) instead"
    split = f" in {len(pieces)} pieces" if len(pieces) > 1 else ""
    return f"token mismatch: got {len(''.join(pieces))} characters{split}, expected {len(TOKEN)}"


def _file_stamp() -> str:
    # Microseconds keep names in arrival order; the suffix keeps two uploads in
    # the same instant (two automations, batched requests) from overwriting.
    return f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{uuid.uuid4().hex[:6]}"


def _write_atomic(dest: pathlib.Path, data: bytes) -> None:
    # The MCP server may read (and compact) the directory at any moment, so a
    # file only appears under its real name once it is complete.
    tmp = dest.with_name(f".{dest.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, dest)


def _workout_filename(workout: dict) -> str:
    workout_id = re.sub(r"[^A-Za-z0-9_-]", "", str(workout.get("id") or ""))[:80]
    if not workout_id:
        key = f"{workout.get('name')}|{workout.get('start')}|{workout.get('end')}"
        workout_id = hashlib.sha1(key.encode()).hexdigest()[:20]
    return f"{workout_id}.json"


_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _night_filename(night: dict) -> str | None:
    """A night is filed under the morning it ended on, like the CSV rows."""
    for key in ("sleepEnd", "inBedEnd", "date"):
        match = _DATE_PREFIX_RE.match(str(night.get(key) or ""))
        if match:
            return f"{match.group()}.json"
    return None


def save_sleep(metrics: list) -> int:
    """Store each night of a JSON sleep_analysis metric; other metrics are ignored."""
    nights, segments, ignored = 0, 0, []
    for metric in metrics:
        if not isinstance(metric, dict):
            continue
        if metric.get("name") != "sleep_analysis":
            ignored.append(str(metric.get("name")))
            continue
        for night in metric.get("data") or []:
            if not isinstance(night, dict):
                continue
            # Without Summarize Data the app sends each sleep stage as its own
            # segment, which says nothing about the night as a whole.
            if isinstance(night.get("value"), str) and "startDate" in night:
                segments += 1
            elif name := _night_filename(night):
                _write_atomic(SLEEP_DIR / name, json.dumps(night, ensure_ascii=False).encode("utf-8"))
                nights += 1
    if ignored:
        print(f"[ingest] ignoring JSON metrics (send them as CSV): {', '.join(ignored)}", flush=True)
    if segments:
        print(f"[ingest] ignoring {segments} sleep segments: turn on Summarize Data in the sleep automation", flush=True)
        if not nights:
            raise HTTPException(status_code=422, detail="Got single sleep stages: turn on Summarize Data in this automation.")
    return nights


def _event_filename(event: dict) -> str:
    """<id>.json, else <start as YYYYMMDD_HHMMSS>.json, so a re-sent event overwrites itself."""
    if event_id := re.sub(r"[^A-Za-z0-9_-]", "", str(event.get("id") or ""))[:80]:
        return f"{event_id}.json"
    digits = re.sub(r"\D", "", str(event.get("start") or ""))[:14]
    if len(digits) == 14:
        return f"{digits[:8]}_{digits[8:]}.json"
    return f"{hashlib.sha1(json.dumps(event, sort_keys=True, default=str).encode()).hexdigest()[:20]}.json"


def save_events(events, directory: pathlib.Path, drop: tuple[str, ...] = ()) -> int:
    saved = 0
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict):
            continue
        event = {k: v for k, v in event.items() if k not in drop}
        _write_atomic(directory / _event_filename(event), json.dumps(event, ensure_ascii=False).encode("utf-8"))
        saved += 1
    return saved


def save_json(payload) -> dict:
    data = payload.get("data", payload) if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Expected a Health Auto Export JSON payload")
    workouts = data.get("workouts") or []
    counts = {
        "nights": save_sleep(data.get("metrics") or []),
        # About 15,000 voltage samples per recording, not used by any tool.
        "ecg": save_events(data.get("ecg"), ECG_DIR, drop=("voltageMeasurements",)),
        "heart_rate_notifications": save_events(data.get("heartRateNotifications"), HR_NOTIFICATIONS_DIR),
        "state_of_mind": save_events(data.get("stateOfMind"), STATE_OF_MIND_DIR),
    }
    if not workouts and not any(counts.values()) and data.get("metrics"):
        raise HTTPException(
            status_code=422,
            detail="Send Health Metrics as CSV; JSON is only read for Sleep Analysis, Workouts, "
            "ECG, Heart Rate Notifications and State of Mind.",
        )
    saved = []
    for workout in workouts:
        if not isinstance(workout, dict):
            continue
        # GPS routes can run to hundreds of MB and are not used by any tool.
        workout = {k: v for k, v in workout.items() if k != "route"}
        dest = WORKOUTS_DIR / _workout_filename(workout)
        _write_atomic(dest, json.dumps(workout, ensure_ascii=False).encode("utf-8"))
        saved.append(dest.name)
    result = {"status": "saved" if saved or any(counts.values()) else "empty", "workouts": len(saved)}
    result.update({key: count for key, count in counts.items() if count})
    return result


@app.post("/ingest")
async def ingest(request: Request, authorization: str | None = Header(None)):
    if not _authorized(authorization):
        print(f"[ingest] 401: {_rejection_reason(authorization)}", flush=True)
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.body()
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON") from exc
        return save_json(payload)

    csv_data = extract_csv(body)
    if not csv_data.strip():
        # Nothing new on the phone yet; answer 200 so the app does not retry.
        return {"status": "empty"}
    dest = DATA_DIR / f"health_{_file_stamp()}.csv"
    _write_atomic(dest, csv_data)

    return {"status": "saved", "file": dest.name, "bytes": len(csv_data)}
