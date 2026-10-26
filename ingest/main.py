"""HTTPS endpoint that receives Apple Health exports from Health Auto Export.

Two automations post to the same /ingest URL:

- Health Metrics as CSV, sent as a multipart body. We strip the MIME envelope
  and write the inner CSV to DATA_DIR.
- Workouts as JSON (export version 2). Each workout is written to
  WORKOUTS_DIR/<id>.json, so re-sent workouts overwrite instead of duplicating.
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


def _authorized(header: str | None) -> bool:
    return header is not None and hmac.compare_digest(header.encode(), f"Bearer {TOKEN}".encode())


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


def save_workouts(payload) -> dict:
    data = payload.get("data", payload) if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Expected a Health Auto Export JSON payload")
    workouts = data.get("workouts") or []
    if not workouts and data.get("metrics"):
        raise HTTPException(
            status_code=422,
            detail="Send Health Metrics as CSV; JSON is only accepted for the Workouts automation.",
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
    return {"status": "saved" if saved else "empty", "workouts": len(saved)}


@app.post("/ingest")
async def ingest(request: Request, authorization: str | None = Header(None)):
    if not _authorized(authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.body()
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON") from exc
        return save_workouts(payload)

    csv_data = extract_csv(body)
    if not csv_data.strip():
        # Nothing new on the phone yet; answer 200 so the app does not retry.
        return {"status": "empty"}
    dest = DATA_DIR / f"health_{_file_stamp()}.csv"
    _write_atomic(dest, csv_data)

    return {"status": "saved", "file": dest.name, "bytes": len(csv_data)}
