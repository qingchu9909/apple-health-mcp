"""Hourly uploads get folded into one merged export without losing data."""
import os
from pathlib import Path

from .conftest import _import_server


def _write_exports(data_dir: Path, count: int) -> None:
    # One upload per hour, each re-sending "yesterday + today so far" the way
    # Health Auto Export's Default date range does: today's steps keep growing.
    for i in range(count):
        path = data_dir / f"health_20260401_{i:02d}0000_000000_abc{i:03d}.csv"
        path.write_text(
            "Date/Time,Step Count (count),Resting Heart Rate (count/min)\n"
            f"2026-03-31 00:00:00,9000,{60 if i == 0 else ''}\n"
            f"2026-04-01 00:00:00,{100 * (i + 1)},\n"
        )
        mtime = 1_775_000_000 + i * 3600
        os.utime(path, (mtime, mtime))


def test_many_exports_are_compacted_into_one(tmp_data_dir: Path):
    server = _import_server(tmp_data_dir)
    _write_exports(tmp_data_dir, server._COMPACT_AFTER + 2)
    newest_mtime = max(f.stat().st_mtime for f in tmp_data_dir.glob("health_*.csv"))

    df = server.load_all_csv()

    remaining = sorted(tmp_data_dir.glob("health_*.csv"))
    assert [f.name for f in remaining] == [f"health_20260401_{server._COMPACT_AFTER + 1:02d}0000_000000_abc{server._COMPACT_AFTER + 1:03d}_merged.csv"]
    assert len(list((tmp_data_dir / "archive").glob("health_*.csv"))) == server._COMPACT_AFTER + 2
    assert remaining[0].stat().st_mtime == newest_mtime  # freshness stays honest
    by_day = df.set_index(df["Date/Time"].dt.strftime("%Y-%m-%d"))
    assert by_day.loc["2026-04-01", "Step Count (count)"] == 100 * (server._COMPACT_AFTER + 2)
    assert by_day.loc["2026-03-31", "Resting Heart Rate (count/min)"] == 60  # sparse value survives


def test_later_uploads_still_win_over_the_merged_file(tmp_data_dir: Path):
    server = _import_server(tmp_data_dir)
    _write_exports(tmp_data_dir, server._COMPACT_AFTER + 1)
    server.load_all_csv()
    (tmp_data_dir / "health_20260402_090000_000000_zzz000.csv").write_text(
        "Date/Time,Step Count (count)\n2026-04-01 00:00:00,12345\n"
    )

    df = server.load_all_csv()

    by_day = df.set_index(df["Date/Time"].dt.strftime("%Y-%m-%d"))
    assert by_day.loc["2026-04-01", "Step Count (count)"] == 12345


def test_second_compaction_replaces_the_previous_merged_file(tmp_data_dir: Path):
    server = _import_server(tmp_data_dir)
    _write_exports(tmp_data_dir, server._COMPACT_AFTER + 1)
    server.load_all_csv()
    for i in range(server._COMPACT_AFTER + 1):
        (tmp_data_dir / f"health_20260403_{i:06d}_000000_new{i:03d}.csv").write_text(
            f"Date/Time,Step Count (count)\n2026-04-03 00:00:00,{i}\n"
        )

    df = server.load_all_csv()

    remaining = sorted(tmp_data_dir.glob("health_*.csv"))
    assert len(remaining) == 1 and remaining[0].name.endswith("_merged.csv")
    assert not list((tmp_data_dir / "archive").glob("*_merged.csv"))
    assert len(df) == 3  # 03-31, 04-01, 04-03


def test_unreadable_files_are_archived_not_lost(tmp_data_dir: Path):
    server = _import_server(tmp_data_dir)
    _write_exports(tmp_data_dir, server._COMPACT_AFTER)
    (tmp_data_dir / "health_20260401_000000_000000_aaaaaa.csv").write_bytes(b"")

    server.load_all_csv()

    assert (tmp_data_dir / "archive" / "health_20260401_000000_000000_aaaaaa.csv").exists()
    assert len(list(tmp_data_dir.glob("health_*.csv"))) == 1


def test_few_files_are_left_alone(tmp_data_dir: Path):
    server = _import_server(tmp_data_dir)
    _write_exports(tmp_data_dir, 3)
    server.load_all_csv()
    assert len(list(tmp_data_dir.glob("health_*.csv"))) == 3
    assert not (tmp_data_dir / "archive").exists()
