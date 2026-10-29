"""Tests for the CSV loader and per-call cache in mcp/server.py."""
import json
import os
import shutil
import time
from pathlib import Path

from .conftest import FIXTURES, _import_server


def test_read_csv_tolerant_handles_clean_file(tmp_data_dir: Path):
    server = _import_server(tmp_data_dir)
    df = server._read_csv_tolerant(FIXTURES / "sample_clean.csv")
    assert list(df.columns)[0] == "Date/Time"
    assert len(df) == 5


def test_read_csv_tolerant_handles_multipart_envelope(tmp_data_dir: Path):
    server = _import_server(tmp_data_dir)
    df_clean = server._read_csv_tolerant(FIXTURES / "sample_clean.csv")
    df_wrapped = server._read_csv_tolerant(FIXTURES / "sample_wrapped.csv")
    # Same column set, same rows, regardless of envelope.
    assert list(df_clean.columns) == list(df_wrapped.columns)
    assert len(df_clean) == len(df_wrapped)
    assert df_clean["Date/Time"].tolist() == df_wrapped["Date/Time"].tolist()


def test_load_all_csv_returns_combined_deduped_frame(populated_data_dir: Path):
    server = _import_server(populated_data_dir)
    df = server.load_all_csv()
    # Both fixtures contain the same 5 rows; dedup on Date/Time -> 5 unique rows.
    assert len(df) == 5
    assert df["Date/Time"].is_monotonic_increasing


def test_load_all_csv_returns_same_instance_on_cache_hit(populated_data_dir: Path):
    server = _import_server(populated_data_dir)
    first = server.load_all_csv()
    second = server.load_all_csv()
    assert first is second, "warm call should return the cached DataFrame instance"


def test_load_all_csv_invalidates_when_file_mtime_changes(populated_data_dir: Path):
    server = _import_server(populated_data_dir)
    first = server.load_all_csv()
    victim = next(populated_data_dir.glob("health_*.csv"))
    # Bump mtime forward to force a signature change.
    future = time.time() + 10
    os.utime(victim, (future, future))
    second = server.load_all_csv()
    assert second is not first, "touched file should invalidate the cache"
    assert len(second) == len(first), "row count should be unchanged"


def test_load_all_csv_invalidates_when_file_added(populated_data_dir: Path):
    server = _import_server(populated_data_dir)
    first = server.load_all_csv()
    extra = populated_data_dir / "health_extra.csv"
    extra.write_bytes((FIXTURES / "sample_clean.csv").read_bytes())
    second = server.load_all_csv()
    assert second is not first


def test_load_all_csv_returns_empty_for_empty_dir(tmp_data_dir: Path):
    server = _import_server(tmp_data_dir)
    df = server.load_all_csv()
    assert df.empty


# Health Auto Export names the columns in the app's language.
ZH_HANT_CSV = "日期/時間,Apple Move 時間 (分鐘),步數 (count)\n2026-04-01 00:00:00,30,5000\n".encode()


def test_a_header_in_another_language_is_skipped_with_the_reason(tmp_data_dir: Path, capsys):
    (tmp_data_dir / "health_1.csv").write_bytes(ZH_HANT_CSV)
    server = _import_server(tmp_data_dir)
    assert server.load_all_csv().empty
    assert "starts with '日期/時間' instead of 'Date/Time'" in capsys.readouterr().out
    # The tools say why there is no data, so Claude can tell the user what to change.
    for message in (server.list_metrics(), server.get_summary(), server.get_sleep()):
        assert "health_1.csv" in message and "language to English" in message
    assert "language to English" in json.loads(server.get_daily_sleep(days=14))["problem"]
    assert "language to English" in server._freshness()["problem"]


def test_only_the_newest_upload_decides_whether_there_is_a_problem(tmp_data_dir: Path):
    (tmp_data_dir / "health_1.csv").write_bytes(ZH_HANT_CSV)
    shutil.copy(FIXTURES / "sample_clean.csv", tmp_data_dir / "health_2.csv")
    server = _import_server(tmp_data_dir)
    assert len(server.load_all_csv()) == 5
    assert server._upload_problem() is None
    assert "problem" not in json.loads(server.get_daily_sleep(days=14))

    # The phone went back to another language: older data stays readable,
    # but the newest upload is flagged.
    (tmp_data_dir / "health_3.csv").write_bytes(ZH_HANT_CSV)
    assert len(server.load_all_csv()) == 5
    assert "health_3.csv" in server._upload_problem()
