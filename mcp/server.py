"""Apple Health MCP server.

Exposes Apple Health data (CSV exports written by the ingest service) to any
MCP client over Streamable HTTP. The bearer token is embedded in the URL path
because Claude's connector UI does not currently support custom auth headers.
"""
import hmac
import io
import json
import os
import pathlib
import re
import threading
from datetime import date as date_cls
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import notify
import pandas as pd
import uvicorn
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

load_dotenv(os.environ.get("DOTENV_PATH", "/opt/health/.env"))
TOKEN = os.getenv("HEALTH_TOKEN")
# The connector URL secret can differ from the phone's ingest token, so a
# leaked connector URL cannot be used to push fake data (and vice versa).
MCP_PATH_TOKEN = os.getenv("MCP_PATH_TOKEN") or TOKEN
# Bearer token for the /api/* routes used by the daily-report routine.
# Unset = those routes answer 503.
REPORT_API_TOKEN = os.getenv("REPORT_API_TOKEN", "")
DATA_DIR = pathlib.Path(os.getenv("DATA_DIR", "/opt/health/data"))
REPORTS_DIR = pathlib.Path(os.getenv("REPORTS_DIR", "/opt/health/reports"))
WORKOUTS_DIR = pathlib.Path(os.getenv("WORKOUTS_DIR", "/opt/health/workouts"))
SLEEP_DIR = pathlib.Path(os.getenv("SLEEP_DIR", "/opt/health/sleep"))
ECG_DIR = pathlib.Path(os.getenv("ECG_DIR", "/opt/health/ecg"))
HR_NOTIFICATIONS_DIR = pathlib.Path(os.getenv("HR_NOTIFICATIONS_DIR", "/opt/health/hr-notifications"))
STATE_OF_MIND_DIR = pathlib.Path(os.getenv("STATE_OF_MIND_DIR", "/opt/health/state-of-mind"))
# Health Auto Export writes CSV timestamps in the phone's local time. Set
# USER_TZ (e.g. Asia/Shanghai) when the VPS runs in a different zone so that
# "today", "last N days" and report dates follow the user, not the server.
USER_TZ = ZoneInfo(os.environ["USER_TZ"]) if os.getenv("USER_TZ") else None

_MIN_TOKEN_LEN = 32
for _name, _value in (("HEALTH_TOKEN", TOKEN), ("MCP_PATH_TOKEN", MCP_PATH_TOKEN)):
    if not _value or len(_value) < _MIN_TOKEN_LEN:
        raise SystemExit(f"{_name} must be at least {_MIN_TOKEN_LEN} characters (openssl rand -hex 32)")

_MAX_REPORT_BYTES = 200_000
_REPORT_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MAX_METRIC_ROWS = 500

mcp = FastMCP("apple-health", stateless_http=True)
mcp.settings.transport_security.enable_dns_rebinding_protection = False


def _now() -> datetime:
    """Naive wall-clock time in the user's zone, comparable with CSV timestamps."""
    if USER_TZ is None:
        return datetime.now()
    return datetime.now(USER_TZ).replace(tzinfo=None)


def _today() -> date_cls:
    return _now().date()


def _dir_signature() -> tuple:
    """Cheap fingerprint of the data directory.

    Each tool call recomputes this and compares against the cached signature;
    a single ``stat`` per file is far cheaper than re-reading every CSV. The
    fingerprint changes when a file is added, removed, or rewritten.
    """
    return tuple(
        (f.name, f.stat().st_mtime_ns, f.stat().st_size)
        for f in sorted(DATA_DIR.glob("health_*.csv"))
    )


_cache: tuple[tuple, pd.DataFrame] | None = None
# Why each file in the cached signature could not be read, so the tools can
# say why data is missing instead of only that it is.
_unreadable: dict[str, str] = {}
_load_lock = threading.Lock()
# Health Auto Export uploads every hour or so. Past this many files the loader
# folds them into one, so a cache miss never has to re-read thousands.
_COMPACT_AFTER = 48


def _read_csv_tolerant(path: pathlib.Path) -> pd.DataFrame:
    """Read a CSV that may still carry a multipart MIME envelope.

    Files saved before the ingest service stripped the envelope start with
    ``--Boundary...`` followed by Content-Disposition / Content-Type headers,
    a blank line, the actual CSV, and finally a closing ``--Boundary--``
    marker. We detect that prefix and slice it off so pandas only sees CSV.
    """
    raw = path.read_bytes()
    if raw.startswith(b"--"):
        _, _, tail = raw.partition(b"\r\n\r\n")
        if not tail:
            _, _, tail = raw.partition(b"\n\n")
        if tail:
            cut = tail.rfind(b"\r\n--")
            if cut == -1:
                cut = tail.rfind(b"\n--")
            if cut != -1:
                tail = tail[:cut]
            raw = tail.strip()
    header = raw.split(b"\n", 1)[0]
    if b"Date/Time" not in header:
        first = header.split(b",", 1)[0].decode("utf-8", "replace").strip('\ufeff"\r ')
        raise ValueError(
            f"its header starts with {first!r} instead of 'Date/Time': Health Auto Export names "
            "the columns in the app's language, and only English names can be read. Set the "
            "app's language to English (iPhone Settings > Apps > Health Auto Export > Language), "
            "then export the missing days again"
        )
    df = pd.read_csv(io.BytesIO(raw), parse_dates=["Date/Time"])
    df["Date/Time"] = _naive_local(df["Date/Time"])
    return _normalize_units(df)


# Health Auto Export names each column after the unit chosen in the Health
# app, so the same metric can arrive as "Active Energy (kcal)" for one user
# and "Active Energy (kJ)" for another. The tools read the metric units on
# the left; the alternatives are converted into them when a file is read.
_UNIT_ALIASES = {
    "Active Energy (kJ)": ("Active Energy (kcal)", lambda v: v * 4.184),
    "Walking + Running Distance (km)": ("Walking + Running Distance (mi)", lambda v: v * 1.609344),
    "Walking Speed (km/hr)": ("Walking Speed (mi/hr)", lambda v: v * 1.609344),
    "Apple Sleeping Wrist Temperature (degC)": (
        "Apple Sleeping Wrist Temperature (degF)",
        lambda v: (v - 32) * 5 / 9,
    ),
    "Blood Glucose (mmol/L)": ("Blood Glucose (mg/dL)", lambda v: v / 18.016),
}


# Newer Health Auto Export versions spell some units differently
# ("Heart Rate [Avg] (bpm)", "Step Count (steps)", "(ºC)"). Same values,
# so they are only renamed.
_UNIT_SPELLINGS = {
    "(bpm)": "(count/min)",
    "(steps)": "(count)",
    "(ºC)": "(degC)",
    "(°C)": "(degC)",
    "(ºF)": "(degF)",
    "(°F)": "(degF)",
}


def _normalize_units(df: pd.DataFrame) -> pd.DataFrame:
    for col in list(df.columns):
        name, _, unit = str(col).rpartition(" ")
        if unit in _UNIT_SPELLINGS:
            canonical = f"{name} {_UNIT_SPELLINGS[unit]}"
            df[canonical] = df[canonical].fillna(df[col]) if canonical in df.columns else df[col]
            df = df.drop(columns=[col])
    for canonical, (alias, convert) in _UNIT_ALIASES.items():
        if alias not in df.columns:
            continue
        converted = convert(pd.to_numeric(df[alias], errors="coerce"))
        df[canonical] = df[canonical].fillna(converted) if canonical in df.columns else converted
        df = df.drop(columns=[alias])
    return df


def _naive_local(series: pd.Series) -> pd.Series:
    """Coerce timestamps to naive local time in the user's zone.

    Health Auto Export writes naive local timestamps, which pass through
    unchanged. Offset-bearing values (``2026-04-01 09:00:00 +0800``) are
    converted, so every row compares cleanly against ``_now()``.
    """
    if not pd.api.types.is_datetime64_any_dtype(series):
        try:
            series = pd.to_datetime(series, errors="coerce")
        except (TypeError, ValueError):
            # Mixed UTC offsets (e.g. across a DST change) need utc=True.
            series = pd.to_datetime(series, utc=True, errors="coerce")
    if series.dt.tz is None:
        return series
    return series.dt.tz_convert(USER_TZ or datetime.now().astimezone().tzinfo).dt.tz_localize(None)


def _merge_exports(frames: list[pd.DataFrame]) -> pd.DataFrame:
    # Frames are ordered oldest first, so for a timestamp that appears in
    # several exports (a day that was still filling up at the previous sync,
    # or two automations covering different metrics) ``last()`` keeps the
    # newest non-null value of every column instead of dropping whole rows.
    return (
        pd.concat(frames, ignore_index=True)
        .groupby("Date/Time", sort=True)
        .last()
        .reset_index()
    )


def _compact(files: list[pathlib.Path]) -> None:
    """Fold ``files`` (sorted oldest first) into a single merged export.

    The result is written next to the newest input as ``<name>_merged.csv``,
    which sorts right after it, so later uploads still win. Raw uploads move
    to ``DATA_DIR/archive`` (kept for recovery, never read) and earlier merged
    files are deleted. A crash part-way leaves older duplicates behind, which
    the newest-wins merge already tolerates.
    """
    frames, inputs, unreadable = [], [], []
    for f in files:
        try:
            frames.append(_read_csv_tolerant(f))
            inputs.append(f)
        except Exception as e:
            print(f"[compact] archiving unreadable {f.name}: {e}", flush=True)
            unreadable.append(f)
    if len(inputs) < 2:
        return
    newest = inputs[-1]
    target = newest.with_name(f"{newest.stem.removesuffix('_merged')}_merged.csv")
    tmp = target.with_name(f".{target.name}.tmp")
    try:
        _merge_exports(frames).to_csv(tmp, index=False, date_format="%Y-%m-%d %H:%M:%S")
        # Keep the newest upload's mtime so the "last upload" freshness check
        # still notices when the phone stops syncing.
        mtime_ns = newest.stat().st_mtime_ns
        os.utime(tmp, ns=(mtime_ns, mtime_ns))
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    archive = DATA_DIR / "archive"
    archive.mkdir(exist_ok=True)
    for f in inputs + unreadable:
        if f == target:
            continue
        if f.stem.endswith("_merged"):
            f.unlink(missing_ok=True)
        else:
            f.rename(archive / f.name)


def load_all_csv() -> pd.DataFrame:
    global _cache, _unreadable
    with _load_lock:
        files = sorted(DATA_DIR.glob("health_*.csv"))
        if len(files) > _COMPACT_AFTER:
            try:
                _compact(files)
            except Exception as e:
                print(f"[compact] failed, reading the files as they are: {e}", flush=True)
        sig = _dir_signature()
        if _cache is not None and _cache[0] == sig:
            return _cache[1]
        dfs, unreadable = [], {}
        for name, _mtime, _size in sig:
            f = DATA_DIR / name
            try:
                dfs.append(_read_csv_tolerant(f))
            except Exception as e:
                print(f"[load_all_csv] skipping {f.name}: {e}", flush=True)
                unreadable[name] = str(e)
        df = _merge_exports(dfs) if dfs else pd.DataFrame()
        _cache, _unreadable = (sig, df), unreadable
        return df


def _upload_problem() -> str | None:
    """Why the newest health-metrics upload could not be read, if it could not.

    Older unreadable files stop mattering once a newer upload reads fine, so
    only the newest is checked. Call after load_all_csv().
    """
    if _cache is None or not _cache[0]:
        return None
    newest = _cache[0][-1][0]
    if newest not in _unreadable:
        return None
    return f"The newest health-metrics upload ({newest}) could not be read: {_unreadable[newest]}."


def _no_data() -> str:
    problem = _upload_problem()
    return f"No readable health data. {problem}" if problem else "No health data available yet."


# --- Daily-aggregation helpers used by the structured tools below -----------

_SLEEP_AGGS = [
    ("total_h", "Sleep Analysis [Total] (hr)", "first"),
    ("asleep_h", "Sleep Analysis [Asleep] (hr)", "first"),
    ("in_bed_h", "Sleep Analysis [In Bed] (hr)", "first"),
    ("core_h", "Sleep Analysis [Core] (hr)", "first"),
    ("deep_h", "Sleep Analysis [Deep] (hr)", "first"),
    ("rem_h", "Sleep Analysis [REM] (hr)", "first"),
    ("awake_h", "Sleep Analysis [Awake] (hr)", "first"),
    ("wrist_temp_c", "Apple Sleeping Wrist Temperature (degC)", "mean"),
]

_FITNESS_AGGS = [
    ("steps", "Step Count (count)", "sum"),
    ("distance_km", "Walking + Running Distance (km)", "sum"),
    ("active_energy_kj", "Active Energy (kJ)", "sum"),
    ("exercise_min", "Apple Exercise Time (min)", "sum"),
    ("stand_min", "Apple Stand Time (min)", "sum"),
    ("flights", "Flights Climbed (count)", "sum"),
    ("walking_speed_kmh_avg", "Walking Speed (km/hr)", "mean"),
    ("walking_hr_avg", "Walking Heart Rate Average (count/min)", "mean"),
    ("vo2_max", "VO2 Max (ml/(kg·min))", "last"),
]

_VITALS_AGGS = [
    ("hr_min", "Heart Rate [Min] (count/min)", "min"),
    ("hr_max", "Heart Rate [Max] (count/min)", "max"),
    ("hr_avg", "Heart Rate [Avg] (count/min)", "mean"),
    ("resting_hr", "Resting Heart Rate (count/min)", "mean"),
    ("hrv_ms", "Heart Rate Variability (ms)", "mean"),
    ("respiratory_rate", "Respiratory Rate (count/min)", "mean"),
    ("blood_oxygen_pct", "Blood Oxygen Saturation (%)", "mean"),
]

# Metrics tracked by get_baselines — drives the report routine's anomaly detection.
_BASELINE_AGGS = [
    ("steps", "Step Count (count)", "sum"),
    ("sleep_total_h", "Sleep Analysis [Total] (hr)", "first"),
    ("deep_h", "Sleep Analysis [Deep] (hr)", "first"),
    ("rem_h", "Sleep Analysis [REM] (hr)", "first"),
    ("hrv_ms", "Heart Rate Variability (ms)", "mean"),
    ("resting_hr", "Resting Heart Rate (count/min)", "mean"),
    ("hr_avg", "Heart Rate [Avg] (count/min)", "mean"),
    ("respiratory_rate", "Respiratory Rate (count/min)", "mean"),
    ("blood_oxygen_pct", "Blood Oxygen Saturation (%)", "mean"),
    ("active_energy_kj", "Active Energy (kJ)", "sum"),
    ("walking_speed_kmh", "Walking Speed (km/hr)", "mean"),
]

# Continuous glucose monitor (CGM) samples arrive at ~5-min cadence all day.
# The briefing cares about the overnight profile, so ``get_daily_vitals``
# aggregates the window 22:00 (previous evening) .. 08:00 and keys it to the
# wake date, matching how sleep records are keyed.
_GLUCOSE_COL = "Blood Glucose (mmol/L)"
_GLUCOSE_NIGHT_START_H = 22
_GLUCOSE_NIGHT_END_H = 8
_GLUCOSE_TIR_LOW_MMOL = 3.9
_GLUCOSE_TIR_HIGH_MMOL = 7.8
_GLUCOSE_NIGHT_FIELDS = [
    "glucose_night_mean_mmol",
    "glucose_night_min_mmol",
    "glucose_night_max_mmol",
    "glucose_night_tir_pct",
    "glucose_night_n",
]

_INTEGER_FIELDS = {"steps", "flights", "glucose_night_n"}


def _round_or_none(value, ndigits: int = 2):
    if value is None or pd.isna(value):
        return None
    return round(float(value), ndigits)


def _filter_recent(df: pd.DataFrame, days: int) -> pd.DataFrame:
    if df.empty:
        return df
    cutoff = _now() - timedelta(days=days)
    return df[df["Date/Time"] >= cutoff]


def _daily_series(df: pd.DataFrame, col: str, agg: str) -> pd.Series:
    """Daily aggregate of ``col`` keyed by date, dropping NaN inputs first."""
    if df.empty or col not in df.columns:
        return pd.Series([], dtype="float64")
    sub = df[df[col].notna()][["Date/Time", col]].copy()
    if sub.empty:
        return pd.Series([], dtype="float64")
    sub["date"] = sub["Date/Time"].dt.date
    return sub.groupby("date")[col].agg(agg)


def _build_daily_frame(df: pd.DataFrame, aggs: list[tuple]) -> pd.DataFrame:
    cols = {key: _daily_series(df, col, agg) for key, col, agg in aggs}
    return pd.DataFrame(cols)


def _glucose_night_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Overnight CGM aggregates keyed by wake date.

    Samples between 22:00 and 08:00 are shifted forward so the whole night
    lands on the morning's calendar date, then reduced to mean / min / max,
    percent of samples inside 3.9-7.8 mmol/L (time in range) and sample
    count. Returns an empty frame with the expected columns when the
    glucose column is absent or has no samples in the window.
    """
    empty = pd.DataFrame({k: pd.Series([], dtype="float64") for k in _GLUCOSE_NIGHT_FIELDS})
    if df.empty or _GLUCOSE_COL not in df.columns:
        return empty
    sub = df[df[_GLUCOSE_COL].notna()][["Date/Time", _GLUCOSE_COL]].copy()
    if sub.empty:
        return empty
    shift_h = 24 - _GLUCOSE_NIGHT_START_H
    shifted = sub["Date/Time"] + pd.Timedelta(hours=shift_h)
    in_window = shifted.dt.hour < _GLUCOSE_NIGHT_END_H + shift_h
    sub = sub[in_window]
    if sub.empty:
        return empty
    sub["date"] = shifted[in_window].dt.date
    values = sub[_GLUCOSE_COL]
    in_range = values.between(_GLUCOSE_TIR_LOW_MMOL, _GLUCOSE_TIR_HIGH_MMOL)
    grouped = values.groupby(sub["date"])
    return pd.DataFrame(
        {
            "glucose_night_mean_mmol": grouped.mean(),
            "glucose_night_min_mmol": grouped.min(),
            "glucose_night_max_mmol": grouped.max(),
            "glucose_night_tir_pct": in_range.groupby(sub["date"]).mean() * 100,
            "glucose_night_n": grouped.size().astype("float64"),
        }
    )


def _records_from_frame(daily: pd.DataFrame) -> list[dict]:
    if daily.empty:
        return []
    records = []
    for date, row in daily.sort_index().iterrows():
        if not row.notna().any():
            continue
        rec: dict = {"date": str(date)}
        for col in daily.columns:
            value = _round_or_none(row[col])
            if value is not None and col in _INTEGER_FIELDS:
                value = int(round(value))
            rec[col] = value
        records.append(rec)
    return records


def _format_records(records: list[dict], days_requested: int) -> str:
    result = {
        "records": records,
        "days_requested": days_requested,
        "days_returned": len(records),
    }
    if problem := _upload_problem():
        result["problem"] = problem
    return json.dumps(result, indent=2, default=str)


# --- Tools -------------------------------------------------------------------


@mcp.tool()
def list_metrics() -> str:
    """List all available health metrics that have data, then the ECG recordings,
    heart rate notifications and logged moods on record (read those with get_metric)."""
    df = load_all_csv()
    cols = [c for c in df.columns if c != "Date/Time" and df[c].notna().any()] if not df.empty else [_no_data()]
    return "\n".join([*cols, "", "Also on record, read with get_metric(<name>):", *_event_metric_lines()])


@mcp.tool()
def get_latest() -> str:
    """Get the most recent health data snapshot."""
    df = load_all_csv()
    if df.empty:
        return _no_data()
    row = df.dropna(how="all", subset=[c for c in df.columns if c != "Date/Time"]).iloc[-1]
    data = row.dropna().to_dict()
    data["Date/Time"] = str(data["Date/Time"])
    return json.dumps(data, indent=2)


@mcp.tool()
def get_metric(metric: str, days: int = 30) -> str:
    """Get a specific health metric over a date range.

    ``metric`` is a name from list_metrics: a column such as "Step Count
    (count)", or "ecg", "heart_rate_notifications" or "state_of_mind" (logged
    moods). Returns at most the 500 most recent rows; narrow ``days`` for
    older data.
    """
    if (name := _event_metric(metric)) is not None:
        return _EVENT_METRICS[name][2](days=days)
    df = load_all_csv()
    if df.empty:
        return _no_data()
    if metric not in df.columns:
        return f"Metric '{metric}' not found. Use list_metrics first."
    cutoff = _now() - timedelta(days=days)
    filtered = df[df["Date/Time"] >= cutoff][["Date/Time", metric]].dropna()
    if len(filtered) > _MAX_METRIC_ROWS:
        note = f"(showing the last {_MAX_METRIC_ROWS} of {len(filtered)} rows; narrow `days` for more)\n"
        return note + filtered.tail(_MAX_METRIC_ROWS).to_string(index=False)
    return filtered.to_string(index=False)


@mcp.tool()
def get_summary(days: int = 7) -> str:
    """Get a health summary for the past N days."""
    df = load_all_csv()
    if df.empty:
        return _no_data()
    cutoff = _now() - timedelta(days=days)
    recent = df[df["Date/Time"] >= cutoff]
    summary_cols = [
        "Step Count (count)", "Heart Rate [Avg] (count/min)", "Heart Rate Variability (ms)",
        "Resting Heart Rate (count/min)", "Sleep Analysis [Asleep] (hr)",
        "Sleep Analysis [Deep] (hr)", "Sleep Analysis [REM] (hr)",
        "Active Energy (kJ)", "VO2 Max (ml/(kg·min))", "Weight (kg)",
        "Respiratory Rate (count/min)", "Blood Oxygen Saturation (%)", _GLUCOSE_COL,
    ]
    available = [c for c in summary_cols if c in recent.columns and recent[c].notna().any()]
    out = [f"Health summary — last {days} days\n"]
    for col in available:
        vals = recent[col].dropna()
        if not vals.empty:
            out.append(
                f"{col}: avg={vals.mean():.2f}, min={vals.min():.2f}, "
                f"max={vals.max():.2f}, last={vals.iloc[-1]:.2f}"
            )
    for name, (directory, label, _) in _EVENT_METRICS.items():
        starts = [start for start, _ in _load_events(directory) if start >= cutoff]
        if starts:
            out.append(f"{label}: {len(starts)}, latest {starts[-1]:%Y-%m-%d %H:%M} (details: get_metric(\"{name}\"))")
    return "\n".join(out)


@mcp.tool()
def get_sleep(days: int = 14) -> str:
    """Get sleep analysis for the past N days."""
    df = load_all_csv()
    if df.empty:
        return _no_data()
    cutoff = _now() - timedelta(days=days)
    sleep_cols = [c for c in df.columns if "Sleep" in c or "Wrist Temperature" in c]
    recent = df[df["Date/Time"] >= cutoff][["Date/Time"] + sleep_cols].dropna(
        subset=sleep_cols, how="all"
    )
    return recent.to_string(index=False)


@mcp.tool()
def get_fitness(days: int = 14) -> str:
    """Get fitness metrics (steps, HRV, heart rate, VO2 max) for past N days."""
    df = load_all_csv()
    if df.empty:
        return _no_data()
    cutoff = _now() - timedelta(days=days)
    fitness_cols = [
        "Step Count (count)", "Heart Rate [Min] (count/min)", "Heart Rate [Max] (count/min)",
        "Heart Rate [Avg] (count/min)", "Heart Rate Variability (ms)",
        "Resting Heart Rate (count/min)", "VO2 Max (ml/(kg·min))",
        "Active Energy (kJ)", "Apple Exercise Time (min)", "Walking + Running Distance (km)",
    ]
    available = [c for c in fitness_cols if c in df.columns]
    recent = df[df["Date/Time"] >= cutoff][["Date/Time"] + available].dropna(
        subset=available, how="all"
    )
    return recent.to_string(index=False)


@mcp.tool()
def get_daily_sleep(days: int = 14) -> str:
    """One JSON record per night with sleep stages, wrist temperature and,
    when the phone also sends sleep times, when the night began and ended.

    Returns ``{"records": [...], "days_requested": N, "days_returned": M}``
    where each record has ``date`` (the morning the night ended on),
    sleep-stage fields in hours (``total_h``, ``asleep_h``, ``in_bed_h``,
    ``core_h``, ``deep_h``, ``rem_h``, ``awake_h``), ``wrist_temp_c`` (mean
    of per-minute wrist temperature samples) and, when known, ``bedtime``,
    ``wake_time``, ``in_bed_start`` and ``in_bed_end`` as local
    ``YYYY-MM-DD HH:MM``. Days with no sleep data are omitted.
    """
    df = _filter_recent(load_all_csv(), days)
    records = _records_from_frame(_build_daily_frame(df, _SLEEP_AGGS)) if not df.empty else []
    return _format_records(_with_sleep_times(records, days), days)


@mcp.tool()
def get_daily_fitness(days: int = 14) -> str:
    """One JSON record per day with activity and movement metrics.

    Per-minute event streams (steps, energy, distance, exercise time,
    stand time, flights) are summed. Walking speed and walking heart
    rate are averaged. VO2 max takes the last non-null measurement of
    the day. Days with no fitness data are omitted.
    """
    df = _filter_recent(load_all_csv(), days)
    if df.empty:
        return _format_records([], days)
    daily = _build_daily_frame(df, _FITNESS_AGGS)
    return _format_records(_records_from_frame(daily), days)


@mcp.tool()
def get_daily_vitals(days: int = 14) -> str:
    """One JSON record per day with cardio, respiratory, oxygen and glucose vitals.

    HR uses min-of-mins, max-of-maxes, mean-of-avgs across the day.
    Resting HR, HRV, respiratory rate, and blood-oxygen saturation are
    averaged across all per-minute samples. Continuous glucose monitor
    samples are reduced to an overnight profile (``glucose_night_mean_mmol``,
    ``glucose_night_min_mmol``, ``glucose_night_max_mmol``,
    ``glucose_night_tir_pct`` = percent of samples in 3.9-7.8 mmol/L,
    ``glucose_night_n``) over 22:00-08:00, keyed to the wake date; the
    fields are null when no CGM data exists. Days with no vitals data
    are omitted.
    """
    df = _filter_recent(load_all_csv(), days)
    if df.empty:
        return _format_records([], days)
    daily = _build_daily_frame(df, _VITALS_AGGS).join(_glucose_night_frame(df), how="outer")
    return _format_records(_records_from_frame(daily), days)


@mcp.tool()
def get_baselines(days: int = 30) -> str:
    """Distribution + recency stats for headline metrics over the last N days.

    For each metric: ``p10``, ``p50``, ``p90`` of the per-day series,
    ``yesterday`` (most recent day's value), ``yesterday_date``,
    ``yesterday_vs_p50_pct``, ``trend_7d_vs_30d_pct``, and ``n_days``.
    Useful for asking "is today unusual?" without re-analysing a long
    history.
    """
    df = _filter_recent(load_all_csv(), days)
    if df.empty:
        return json.dumps({"baselines": [], "days_requested": days}, indent=2)

    # Today's totals (steps, energy) are still filling up; comparing them with
    # full days would always read as a drop.
    today = _today()
    per_day = [
        (key, series[series.index != today] if agg == "sum" else series)
        for key, col, agg in _BASELINE_AGGS
        for series in [_daily_series(df, col, agg)]
    ]
    per_day.append(("glucose_night_mean_mmol", _glucose_night_frame(df)["glucose_night_mean_mmol"]))

    baselines = []
    for key, series in per_day:
        series = series.sort_index()
        if series.empty:
            continue
        p50 = float(series.quantile(0.50))
        latest = float(series.iloc[-1])
        mean_30 = float(series.mean())
        mean_7 = float(series.tail(7).mean()) if len(series) > 0 else None
        rec = {
            "metric": key,
            "p10": _round_or_none(series.quantile(0.10)),
            "p50": _round_or_none(p50),
            "p90": _round_or_none(series.quantile(0.90)),
            "yesterday": _round_or_none(latest),
            "yesterday_date": str(series.index[-1]),
            "yesterday_vs_p50_pct": (
                _round_or_none((latest - p50) / p50 * 100, ndigits=1) if p50 else None
            ),
            "trend_7d_vs_30d_pct": (
                _round_or_none((mean_7 - mean_30) / mean_30 * 100, ndigits=1)
                if mean_30 and mean_7 is not None
                else None
            ),
            "n_days": int(len(series)),
        }
        baselines.append(rec)

    return json.dumps(
        {"baselines": baselines, "days_requested": days},
        indent=2,
        default=str,
    )


# --- Daily report read/write tools ------------------------------------------


def _resolve_report_date(date: str | None) -> str:
    """Validate and normalise a YYYY-MM-DD report date, defaulting to today."""
    if date is None:
        return _today().isoformat()
    if not _REPORT_DATE_RE.match(date):
        raise ValueError(f"invalid date: {date!r}; expected YYYY-MM-DD")
    # Strict parse — rejects e.g. 2026-13-01.
    datetime.strptime(date, "%Y-%m-%d")
    return date


@mcp.tool()
def save_health_report(content: str, date: str | None = None) -> str:
    """Persist a daily HEALTH REPORT (markdown) to the reports directory.

    The scheduled report-generation routine calls this at the end of its run.
    Files are written as ``REPORTS_DIR/<YYYY-MM-DD>.md`` and overwrite any
    existing file for the same date. ``date`` defaults to today; pass an
    explicit ``YYYY-MM-DD`` to back-date or future-date a report.

    Returns ``{"saved": "<path>", "bytes": N, "date": "<YYYY-MM-DD>"}`` on
    success or ``{"error": "<message>"}`` on validation failure.
    """
    if not isinstance(content, str) or not content.strip():
        return json.dumps({"error": "content must be a non-empty string"})
    encoded = content.encode("utf-8")
    if len(encoded) > _MAX_REPORT_BYTES:
        return json.dumps({"error": f"content exceeds {_MAX_REPORT_BYTES} bytes"})
    try:
        resolved = _resolve_report_date(date)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    target = REPORTS_DIR / f"{resolved}.md"
    target.write_bytes(encoded)
    return json.dumps(
        {"saved": str(target), "bytes": target.stat().st_size, "date": resolved}
    )


@mcp.tool()
def get_health_report(date: str | None = None, max_age_days: int = 7) -> str:
    """Read a daily HEALTH REPORT from the reports directory.

    With ``date`` set to ``YYYY-MM-DD``: returns that day's report or a
    no-report sentinel.

    With ``date`` left as ``None``: returns the most recent report whose
    filename date is within ``max_age_days`` of today. Useful for the
    Fitness Coach Agent ("show me today's report").

    Returns ``{"date": "<YYYY-MM-DD>", "content": "<markdown>"}`` on hit or
    ``{"date": null, "note": "<message>"}`` on miss.
    """
    if date is not None:
        try:
            resolved = _resolve_report_date(date)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        target = REPORTS_DIR / f"{resolved}.md"
        if target.exists():
            return json.dumps(
                {"date": resolved, "content": target.read_text(encoding="utf-8")},
                ensure_ascii=False,
            )
        return json.dumps({"date": None, "note": f"No report for {resolved}."})

    if not REPORTS_DIR.exists():
        return json.dumps({"date": None, "note": "Reports directory does not exist yet."})

    today = _today()
    cutoff = today - timedelta(days=max_age_days)
    candidates: list[tuple] = []
    for path in REPORTS_DIR.glob("*.md"):
        if not _REPORT_DATE_RE.match(path.stem):
            continue
        try:
            day = datetime.strptime(path.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day >= cutoff:
            candidates.append((day, path))

    if not candidates:
        return json.dumps(
            {"date": None, "note": f"No report in the last {max_age_days} day(s)."}
        )
    candidates.sort()
    latest_day, latest_path = candidates[-1]
    return json.dumps(
        {"date": latest_day.isoformat(), "content": latest_path.read_text(encoding="utf-8")},
        ensure_ascii=False,
    )


# --- Workouts ----------------------------------------------------------------
#
# The ingest service stores each workout from a Health Auto Export "Workouts"
# automation (JSON, export version 2) as WORKOUTS_DIR/<id>.json.

_WORKOUT_TIME_FORMAT = "%Y-%m-%d %H:%M:%S %z"


def _parse_workout_time(value) -> datetime | None:
    """Parse ``yyyy-MM-dd HH:mm:ss Z`` into naive local time in the user's zone."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.strptime(value.strip(), _WORKOUT_TIME_FORMAT)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(USER_TZ).replace(tzinfo=None)


def _quantity(value) -> dict | None:
    """``{"qty": 5.2, "units": "km"}`` -> ``{"value": 5.2, "unit": "km"}``."""
    if isinstance(value, dict) and isinstance(value.get("qty"), int | float):
        return {"value": round(float(value["qty"]), 2), "unit": value.get("units")}
    if isinstance(value, int | float) and not isinstance(value, bool):
        return {"value": round(float(value), 2), "unit": None}
    return None


_event_caches: dict[pathlib.Path, tuple[tuple, list]] = {}


def _load_events(directory: pathlib.Path) -> list[tuple[datetime, dict]]:
    """JSON files with a ``start`` time (workouts, ECGs, notifications), oldest first."""
    if not directory.exists():
        return []
    paths = sorted(directory.glob("*.json"))
    sig = tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in paths)
    cached = _event_caches.get(directory)
    if cached is not None and cached[0] == sig:
        return cached[1]
    items = []
    for path in paths:
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"[load_events] skipping {path}: {exc}", flush=True)
            continue
        start = _parse_workout_time(event.get("start")) if isinstance(event, dict) else None
        if start is not None:
            items.append((start, event))
    items.sort(key=lambda item: item[0])
    _event_caches[directory] = (sig, items)
    return items


def _load_workouts() -> list[tuple[datetime, dict]]:
    return _load_events(WORKOUTS_DIR)


def _workout_summary(start: datetime, workout: dict) -> dict:
    end = _parse_workout_time(workout.get("end"))
    duration_s = workout.get("duration")
    if not isinstance(duration_s, int | float) and end is not None:
        duration_s = (end - start).total_seconds()
    heart_rate = workout.get("heartRate") if isinstance(workout.get("heartRate"), dict) else {}
    summary = {
        "id": workout.get("id"),
        "name": workout.get("name"),
        "date": start.date().isoformat(),
        "start": start.strftime("%Y-%m-%d %H:%M"),
        "end": end.strftime("%Y-%m-%d %H:%M") if end else None,
        "duration_min": round(duration_s / 60, 1) if isinstance(duration_s, int | float) else None,
        "location": workout.get("location"),
        "active_energy": _quantity(workout.get("activeEnergyBurned")),
        "total_energy": _quantity(workout.get("totalEnergy")),
        "distance": _quantity(workout.get("distance")),
        "elevation_up": _quantity(workout.get("elevationUp")),
        "hr_avg": _quantity(heart_rate.get("avg")) or _quantity(workout.get("avgHeartRate")),
        "hr_max": _quantity(heart_rate.get("max")) or _quantity(workout.get("maxHeartRate")),
        "hr_min": _quantity(heart_rate.get("min")),
        "intensity": _quantity(workout.get("intensity")),
        "temperature": _quantity(workout.get("temperature")),
        "humidity": _quantity(workout.get("humidity")),
    }
    return {key: value for key, value in summary.items() if value is not None}


@mcp.tool()
def get_workouts(days: int = 14) -> str:
    """Workouts from the last N days, oldest first, one compact record each.

    Each record has ``id``, ``name`` (workout type), ``start``/``end``,
    ``duration_min``, and when recorded: ``distance``, ``active_energy``,
    ``total_energy``, ``elevation_up``, ``hr_avg``/``hr_max``/``hr_min``,
    ``intensity`` (MET), ``temperature``, ``humidity`` — each as
    ``{"value", "unit"}``. Use ``get_workout_detail`` for the minute-by-minute
    heart rate of one workout.
    """
    cutoff = _now() - timedelta(days=days)
    records = [_workout_summary(start, w) for start, w in _load_workouts() if start >= cutoff]
    return json.dumps(
        {"records": records, "days_requested": days, "days_returned": len(records)},
        indent=2,
        ensure_ascii=False,
    )


def _series(entries, fields: tuple[str, ...]) -> list[dict]:
    rows = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        when = _parse_workout_time(entry.get("date"))
        row = {"time": when.strftime("%H:%M") if when else entry.get("date")}
        for field in fields:
            if isinstance(entry.get(field), int | float):
                row[field.lower()] = round(float(entry[field]), 1)
        rows.append(row)
    return rows


@mcp.tool()
def get_workout_detail(workout_id: str) -> str:
    """Full detail for one workout (``id`` from ``get_workouts``).

    Adds the minute-by-minute heart rate (``heart_rate``: time, min/avg/max),
    heart-rate recovery after the workout, total steps, and the workout's
    metadata as exported by Health Auto Export.
    """
    for start, workout in _load_workouts():
        if workout.get("id") != workout_id:
            continue
        detail = _workout_summary(start, workout)
        detail["heart_rate"] = _series(workout.get("heartRateData"), ("Min", "Avg", "Max"))
        detail["heart_rate_recovery"] = _series(workout.get("heartRateRecovery"), ("Min", "Avg", "Max"))
        steps = [e.get("qty") for e in workout.get("stepCount") or [] if isinstance(e, dict)]
        if steps:
            detail["steps"] = int(sum(q for q in steps if isinstance(q, int | float)))
        if workout.get("metadata"):
            detail["metadata"] = workout["metadata"]
        return json.dumps(detail, indent=2, ensure_ascii=False, default=str)
    return json.dumps({"error": f"workout {workout_id!r} not found; use get_workouts first"})


# --- Notifications -------------------------------------------------------------


@mcp.tool()
async def send_message(text: str, title: str = "") -> str:
    """Push a message to the user's phone via Telegram and/or WeChat.

    Only call this when the user asks to send or push something. ``text`` may
    use simple Markdown (headings, **bold**, bullets). Returns the delivery
    status per configured channel.
    """
    if not text.strip():
        return json.dumps({"error": "text must be a non-empty string"})
    if not notify.configured_channels():
        return json.dumps({"error": "no push channel configured on the server"})
    results = await run_in_threadpool(notify.send_all, title, text)
    return json.dumps(results, ensure_ascii=False)


# --- ECG, heart rate notifications and State of Mind --------------------------
#
# One Health Auto Export automation each (JSON); the ingest service stores
# every recording, notification or mood entry as <DIR>/<id or start>.json.


def _records_json(records: list[dict], days: int) -> str:
    return json.dumps(
        {"records": records, "days_requested": days, "days_returned": len(records)},
        indent=2,
        ensure_ascii=False,
    )


@mcp.tool()
def get_ecg(days: int = 90) -> str:
    """ECG recordings taken with the Apple Watch ECG app in the last N days, oldest first.

    Each record has ``start``, ``classification`` ("Sinus Rhythm",
    "Atrial Fibrillation", "Inconclusive", ...), ``average_hr`` (bpm) and,
    when present, ``severity``. The voltage samples are not kept.
    """
    cutoff = _now() - timedelta(days=days)
    records = []
    for start, ecg in _load_events(ECG_DIR):
        if start < cutoff:
            continue
        rec = {"start": start.strftime("%Y-%m-%d %H:%M"), "classification": ecg.get("classification")}
        if (hr := _quantity(ecg.get("averageHeartRate"))) is not None:
            rec["average_hr"] = hr["value"]
        if ecg.get("severity"):
            rec["severity"] = ecg["severity"]
        records.append(rec)
    return _records_json(records, days)


def _sample_values(samples, field: str) -> list[float]:
    if not isinstance(samples, list):
        return []
    return [
        float(item[field])
        for item in samples
        if isinstance(item, dict) and isinstance(item.get(field), int | float)
    ]


@mcp.tool()
def get_heart_rate_notifications(days: int = 30) -> str:
    """High, low and irregular-rhythm heart rate notifications from the last N days.

    Each record has ``start``, ``end``, ``kind`` ("high", "low" or
    "irregular_rhythm"), ``threshold_bpm`` for high and low notifications,
    and the heart rate samples behind it summarised as ``hr_min``,
    ``hr_avg``, ``hr_max`` (bpm) plus ``hrv_avg_ms`` when present.
    """
    cutoff = _now() - timedelta(days=days)
    records = []
    for start, note in _load_events(HR_NOTIFICATIONS_DIR):
        if start < cutoff:
            continue
        end = _parse_workout_time(note.get("end"))
        rates = _sample_values(note.get("heartRate"), "hr")
        rec = {"start": start.strftime("%Y-%m-%d %H:%M"), "end": end.strftime("%Y-%m-%d %H:%M") if end else None}
        threshold = note.get("threshold")
        if isinstance(threshold, int | float):
            # High and low notifications carry the threshold they crossed.
            rec["kind"] = "low" if rates and sum(rates) / len(rates) < threshold else "high"
            rec["threshold_bpm"] = round(float(threshold), 1)
        else:
            rec["kind"] = "irregular_rhythm"
        if rates:
            rec.update(hr_min=min(rates), hr_avg=round(sum(rates) / len(rates), 1), hr_max=max(rates))
        if hrv := _sample_values(note.get("heartRateVariation"), "hrv"):
            rec["hrv_avg_ms"] = round(sum(hrv) / len(hrv), 1)
        records.append(rec)
    return _records_json(records, days)


@mcp.tool()
def get_state_of_mind(days: int = 14) -> str:
    """Moods and emotions the user logged (State of Mind) in the last N days, oldest first.

    The watch does not measure mood; these are entries the user made in the
    Mindfulness app or the Health app. Each record has ``time`` (local),
    ``kind`` ("daily_mood" for the day as a whole, "momentary_emotion" for
    one moment), ``valence`` from -1 (very unpleasant) to 1 (very pleasant),
    ``feeling`` (``valenceClassification``, e.g. "slightly_unpleasant"),
    ``labels`` (e.g. "stressed", "calm") and ``associations`` (what it was
    about, e.g. "work", "family").
    """
    cutoff = _now() - timedelta(days=days)
    records = []
    for start, entry in _load_events(STATE_OF_MIND_DIR):
        if start < cutoff:
            continue
        rec = {"time": start.strftime("%Y-%m-%d %H:%M"), "kind": entry.get("kind")}
        if isinstance(entry.get("valence"), int | float):
            rec["valence"] = round(float(entry["valence"]), 2)
        rec["feeling"] = entry.get("valenceClassification")
        for key in ("labels", "associations"):
            if isinstance(entry.get(key), list) and entry[key]:
                rec[key] = entry[key]
        records.append(rec)
    return _records_json(records, days)


# Claude's connectors keep the tool list they first saw (anthropics/claude-ai-mcp
# issues #45 and #137), so a client added before the three tools above existed
# cannot call them. list_metrics, get_metric and get_summary, which every client
# has, point to them and serve them too.
_EVENT_METRICS = {
    "ecg": (ECG_DIR, "ECG recordings", get_ecg),
    "heart_rate_notifications": (
        HR_NOTIFICATIONS_DIR, "high, low and irregular heart rate notifications", get_heart_rate_notifications,
    ),
    "state_of_mind": (STATE_OF_MIND_DIR, "logged moods and emotions", get_state_of_mind),
}
_EVENT_ALIASES = {
    "electrocardiogram": "ecg",
    "心电图": "ecg",
    "心電圖": "ecg",
    "heart_rate_notification": "heart_rate_notifications",
    "心率通知": "heart_rate_notifications",
    "mood": "state_of_mind",
    "moods": "state_of_mind",
    "emotion": "state_of_mind",
    "emotions": "state_of_mind",
    "心情": "state_of_mind",
    "情绪": "state_of_mind",
    "情緒": "state_of_mind",
    "心理状态": "state_of_mind",
    "心理狀態": "state_of_mind",
}


def _event_metric(name: str) -> str | None:
    key = re.sub(r"[\s-]+", "_", str(name).strip().lower())
    key = _EVENT_ALIASES.get(key, key)
    return key if key in _EVENT_METRICS else None


def _event_metric_lines() -> list[str]:
    lines = []
    for name, (directory, label, _) in _EVENT_METRICS.items():
        events = _load_events(directory)
        latest = f", latest {events[-1][0]:%Y-%m-%d %H:%M}" if events else ""
        lines.append(f"{name} ({label}): {len(events)}{latest}")
    return lines


# --- Sleep times -------------------------------------------------------------
#
# The CSV export only has durations. A third Health Auto Export automation
# (Health Metrics with only Sleep Analysis selected, JSON) also says when each
# night began and ended; the ingest service stores it as SLEEP_DIR/<date>.json,
# named after the morning the night ended on.

_SLEEP_JSON_TIMES = {
    "sleepStart": "bedtime",
    "sleepEnd": "wake_time",
    "inBedStart": "in_bed_start",
    "inBedEnd": "in_bed_end",
}
_SLEEP_JSON_HOURS = {
    "totalSleep": "total_h",
    "asleep": "asleep_h",
    "inBed": "in_bed_h",
    "core": "core_h",
    "deep": "deep_h",
    "rem": "rem_h",
    "awake": "awake_h",
}

_sleep_cache: tuple[tuple, dict] | None = None


def _load_sleep_times() -> dict[str, dict]:
    """Record fields per night, keyed by date, from the JSON sleep uploads."""
    global _sleep_cache
    if not SLEEP_DIR.exists():
        return {}
    paths = sorted(p for p in SLEEP_DIR.glob("*.json") if _REPORT_DATE_RE.match(p.stem))
    sig = tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in paths)
    if _sleep_cache is not None and _sleep_cache[0] == sig:
        return _sleep_cache[1]
    nights = {}
    for path in paths:
        try:
            night = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"[load_sleep] skipping {path.name}: {exc}", flush=True)
            continue
        if not isinstance(night, dict):
            continue
        fields = {}
        for source, key in _SLEEP_JSON_TIMES.items():
            if (when := _parse_workout_time(night.get(source))) is not None:
                fields[key] = when.strftime("%Y-%m-%d %H:%M")
        for source, key in _SLEEP_JSON_HOURS.items():
            if (hours := _round_or_none(night.get(source))) is not None:
                fields[key] = hours
        if fields:
            nights[path.stem] = fields
    _sleep_cache = (sig, nights)
    return nights


def _with_sleep_times(records: list[dict], days: int) -> list[dict]:
    """Add the night's times to each CSV record; nights the CSV lacks get their own."""
    nights = _load_sleep_times()
    if not nights:
        return records
    cutoff = _now() - timedelta(days=days)
    by_date = {rec["date"]: rec for rec in records}
    for date, fields in nights.items():
        if datetime.fromisoformat(date) < cutoff:  # the window of the CSV rows, stamped at midnight
            continue
        rec = by_date.setdefault(date, {"date": date})
        for key, value in fields.items():
            if rec.get(key) is None:  # the CSV value wins where both have one
                rec[key] = value
    return [by_date[date] for date in sorted(by_date)]


# --- HTTP API for the scheduled report routine --------------------------------
#
# Scheduled cloud routines cannot reliably attach a custom MCP connector, so
# the daily-report routine reads the same data over plain HTTPS instead:
#   GET  /api/bundle?days=14   everything a morning briefing needs, one call
#   POST /api/report           {"content", "title"?, "date"?, "push"?}
# Both require ``Authorization: Bearer $REPORT_API_TOKEN``.


def _freshness() -> dict:
    files = sorted(DATA_DIR.glob("health_*.csv"), key=lambda f: f.stat().st_mtime)
    df = load_all_csv()
    workouts = _load_workouts()
    tz = USER_TZ or datetime.now().astimezone().tzinfo
    last_upload = (
        datetime.fromtimestamp(files[-1].stat().st_mtime, tz).strftime("%Y-%m-%d %H:%M")
        if files
        else None
    )
    nights = _load_sleep_times()
    result = {
        "last_upload": last_upload,
        "latest_metric_row": str(df["Date/Time"].max()) if not df.empty else None,
        "latest_workout_start": workouts[-1][0].strftime("%Y-%m-%d %H:%M") if workouts else None,
        "latest_sleep_night": max(nights) if nights else None,
    }
    if problem := _upload_problem():
        result["problem"] = problem
    return result


# Columns already summarised by the structured tools above.
_STRUCTURED_COLUMNS = {
    col for aggs in (_SLEEP_AGGS, _FITNESS_AGGS, _VITALS_AGGS, _BASELINE_AGGS) for _, col, _ in aggs
} | {_GLUCOSE_COL}


def other_metrics(days: int = 7) -> dict:
    """Daily values of every other exported metric that has data in the window.

    Health Auto Export CSV rows are already daily aggregates, so this is the
    exported value per day (the mean if a day has several rows).
    """
    df = _filter_recent(load_all_csv(), days)
    result = {}
    for col in df.columns:
        if col == "Date/Time" or col in _STRUCTURED_COLUMNS or not pd.api.types.is_numeric_dtype(df[col]):
            continue
        daily = _daily_series(df, col, "mean")
        if not daily.empty:
            result[col] = {str(day): _round_or_none(value) for day, value in daily.sort_index().items()}
    return result


def build_bundle(days: int = 14) -> dict:
    """All inputs for the morning briefing in one JSON document."""
    days = max(1, min(days, 90))
    week_ago = (_today() - timedelta(days=7)).isoformat()
    previous = json.loads(get_health_report(date=week_ago))
    return {
        "generated_at": _now().strftime("%Y-%m-%d %H:%M"),
        "timezone": str(USER_TZ) if USER_TZ else "server local time",
        "today": _today().isoformat(),
        "freshness": _freshness(),
        "baselines": json.loads(get_baselines(days=30)),
        "daily_sleep": json.loads(get_daily_sleep(days=days)),
        "daily_vitals": json.loads(get_daily_vitals(days=days)),
        "daily_fitness": json.loads(get_daily_fitness(days=days)),
        "workouts": json.loads(get_workouts(days=days)),
        "ecg": json.loads(get_ecg(days=days)),
        "heart_rate_notifications": json.loads(get_heart_rate_notifications(days=days)),
        "state_of_mind": json.loads(get_state_of_mind(days=days)),
        "other_metrics": other_metrics(days=min(days, 7)),
        "report_7_days_ago": previous if previous.get("date") else None,
    }


def publish_report(content, title: str | None = None, date: str | None = None, push: bool = True) -> dict:
    """Save the report, then push it to every configured channel."""
    saved = json.loads(save_health_report(content=content, date=date))
    if "error" in saved:
        return saved
    result = {"saved": saved}
    if push:
        try:
            sent = notify.send_all(title or f"Health report {saved['date']}", content)
        except Exception as exc:  # the report is saved either way
            sent = {"error": f"{type(exc).__name__}: {exc}"[:300]}
        result["push"] = sent or {"error": "no push channel configured on the server"}
    return result


def _bearer_ok(request: Request) -> bool:
    expected = f"Bearer {REPORT_API_TOKEN}".encode()
    supplied = request.headers.get("authorization", "").encode()
    return hmac.compare_digest(supplied, expected)


def _api_guard(request: Request) -> JSONResponse | None:
    if len(REPORT_API_TOKEN) < _MIN_TOKEN_LEN:
        return JSONResponse({"error": "REPORT_API_TOKEN is not configured"}, status_code=503)
    if not _bearer_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return None


@mcp.custom_route("/api/bundle", methods=["GET"])
async def api_bundle(request: Request) -> JSONResponse:
    if (denied := _api_guard(request)) is not None:
        return denied
    try:
        days = int(request.query_params.get("days", "14"))
    except ValueError:
        return JSONResponse({"error": "days must be an integer"}, status_code=400)
    return JSONResponse(await run_in_threadpool(build_bundle, days))


@mcp.custom_route("/api/report", methods=["POST"])
async def api_report(request: Request) -> JSONResponse:
    if (denied := _api_guard(request)) is not None:
        return denied
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse({"error": "body must be JSON"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    result = await run_in_threadpool(
        publish_report,
        payload.get("content"),
        payload.get("title"),
        payload.get("date"),
        bool(payload.get("push", True)),
    )
    return JSONResponse(result, status_code=400 if "error" in result else 200)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


mcp.settings.streamable_http_path = f"/mcp/{MCP_PATH_TOKEN}"

inner_app = mcp.streamable_http_app()


async def app(scope, receive, send):
    await inner_app(scope, receive, send)


if __name__ == "__main__":
    # Bind to loopback only; Nginx/Caddy terminates TLS in front of it. The
    # access log is off because every MCP request line contains the URL token.
    uvicorn.run(
        app,
        host=os.getenv("MCP_HOST", "127.0.0.1"),
        port=int(os.getenv("MCP_PORT", "8002")),
        access_log=False,
    )
