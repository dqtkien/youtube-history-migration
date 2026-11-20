"""Tests for yt-dlp importer wrapper, verification, circuit-breaker, and cookies."""

from collections.abc import Callable
import io
import os
from pathlib import Path
from types import TracebackType
from unittest.mock import patch

import yt_dlp

from yt_history_migrator.importer import (
    YtDlpWatchLogger,
    classify_exception,
    prepare_session_cookiefile,
    run_migration,
)
from yt_history_migrator.models import MigrationStatus, WatchEntry
from yt_history_migrator.progress_store import ProgressStore


def _entry(video_id: str, title: str = "Sample Video") -> WatchEntry:
    return WatchEntry(
        video_id=video_id,
        canonical_url=f"https://www.youtube.com/watch?v={video_id}",
        title=title,
        channel_name="Channel",
        watched_at_raw="2026-01-01T00:00:00Z",
        watched_at_epoch=1767225600.0,
        record_index=0,
    )


class _FakeYoutubeDL:
    """Typed fake YoutubeDL context manager simulating logger output and errors."""

    def __init__(
        self,
        ydl_opts: dict[str, object],
        behavior: Callable[[str, YtDlpWatchLogger], None],
    ) -> None:
        logger_obj = ydl_opts.get("logger")
        assert isinstance(logger_obj, YtDlpWatchLogger)
        self._logger = logger_obj
        self._behavior = behavior
        self.ydl_opts = ydl_opts

    def __enter__(self) -> "_FakeYoutubeDL":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        return None

    def extract_info(self, url: str, *, download: bool = False) -> dict[str, str]:
        assert download is False
        self._behavior(url, self._logger)
        return {"id": url.split("v=")[-1]}


def test_run_migration_happy_path_marks_watched(tmp_path: Path) -> None:
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    state_dir = tmp_path / "state"
    store = ProgressStore(state_dir / "progress.jsonl")
    store.load()

    def emit_watched(url: str, logger: YtDlpWatchLogger) -> None:
        vid = url.split("v=")[-1]
        logger.debug(f"[youtube] {vid}: Marking watched")
        logger.debug(f"[youtube] {vid}: Marking fully watched")

    captured_opts: list[dict[str, object]] = []

    def factory(ydl_opts: dict[str, object]) -> _FakeYoutubeDL:
        captured_opts.append(ydl_opts)
        return _FakeYoutubeDL(ydl_opts, emit_watched)

    entries = [_entry("vid00000001", "First"), _entry("vid00000002", "Second")]
    out = io.StringIO()

    with patch("yt_history_migrator.importer.yt_dlp.YoutubeDL", side_effect=factory):
        summary = run_migration(
            entries,
            store,
            state_dir,
            cookies_path=cookies,
            min_delay=0.0,
            max_delay=0.0,
            sleep_fn=lambda _secs: None,
            out=out,
        )

    assert summary.watched == 2
    assert summary.attempted == 2
    assert summary.aborted_on_auth_circuit_breaker is False
    assert store.should_skip("vid00000001") is True
    assert store.should_skip("vid00000002") is True
    assert captured_opts[0]["cookiefile"] == str(state_dir / "session-cookies.txt")


def test_run_migration_halts_after_three_consecutive_auth_failures(tmp_path: Path) -> None:
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    state_dir = tmp_path / "state"
    store = ProgressStore(state_dir / "progress.jsonl")
    store.load()

    call_count = 0

    def simulate_auth_failure(url: str, logger: YtDlpWatchLogger) -> None:
        nonlocal call_count
        call_count += 1
        vid = url.split("v=")[-1]
        if call_count == 1:
            # First failure: silent unauthenticated run (never emits Marking fully watched)
            return
        if call_count == 2:
            # Second failure: yt-dlp warning
            logger.warning(f"[youtube] {vid}: Unable to mark watched")
            return
        # Third failure: silent again
        return

    def factory(ydl_opts: dict[str, object]) -> _FakeYoutubeDL:
        return _FakeYoutubeDL(ydl_opts, simulate_auth_failure)

    entries = [
        _entry("vid00000001"),
        _entry("vid00000002"),
        _entry("vid00000003"),
        _entry("vid00000004"),
    ]
    out = io.StringIO()

    with patch("yt_history_migrator.importer.yt_dlp.YoutubeDL", side_effect=factory):
        summary = run_migration(
            entries,
            store,
            state_dir,
            cookies_path=cookies,
            min_delay=0.0,
            max_delay=0.0,
            max_consecutive_auth_failures=3,
            sleep_fn=lambda _secs: None,
            out=out,
        )

    assert summary.attempted == 3
    assert summary.auth_failed == 3
    assert summary.aborted_on_auth_circuit_breaker is True
    assert store.get("vid00000004") is None
    for vid in ("vid00000001", "vid00000002", "vid00000003"):
        rec = store.get(vid)
        assert rec is not None
        assert rec.status == MigrationStatus.AUTH_FAILED


def test_run_migration_records_unavailable_without_tripping_auth_circuit_breaker(tmp_path: Path) -> None:
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    state_dir = tmp_path / "state"
    store = ProgressStore(state_dir / "progress.jsonl")
    store.load()

    def behavior(url: str, logger: YtDlpWatchLogger) -> None:
        vid = url.split("v=")[-1]
        if vid.startswith("priv"):
            raise yt_dlp.utils.DownloadError("ERROR: [youtube] priv: Private video")
        logger.debug(f"[youtube] {vid}: Marking fully watched")

    def factory(ydl_opts: dict[str, object]) -> _FakeYoutubeDL:
        return _FakeYoutubeDL(ydl_opts, behavior)

    entries = [
        _entry("priv0000001"),
        _entry("priv0000002"),
        _entry("priv0000003"),
        _entry("priv0000004"),
        _entry("good0000005"),
    ]
    out = io.StringIO()

    with patch("yt_history_migrator.importer.yt_dlp.YoutubeDL", side_effect=factory):
        summary = run_migration(
            entries,
            store,
            state_dir,
            cookies_path=cookies,
            min_delay=0.0,
            max_delay=0.0,
            sleep_fn=lambda _secs: None,
            out=out,
        )

    assert summary.attempted == 5
    assert summary.unavailable == 4
    assert summary.watched == 1
    assert summary.aborted_on_auth_circuit_breaker is False


def test_prepare_session_cookiefile_protects_original_and_refreshes_on_newer_mtime(tmp_path: Path) -> None:
    source = tmp_path / "cookies.txt"
    source.write_text("initial-cookie-data\n", encoding="utf-8")
    state_dir = tmp_path / "state"

    session = prepare_session_cookiefile(source, state_dir)
    assert session == state_dir / "session-cookies.txt"
    assert session.read_text(encoding="utf-8") == "initial-cookie-data\n"

    # Mutate session copy as yt-dlp would; source stays untouched and older-or-equal mtime keeps session.
    session.write_text("yt-dlp-mutated\n", encoding="utf-8")
    os.utime(source, (session.stat().st_atime - 10, session.stat().st_mtime - 10))
    prepare_session_cookiefile(source, state_dir)
    assert session.read_text(encoding="utf-8") == "yt-dlp-mutated\n"

    # When user exports a newer cookies.txt, session-cookies.txt refreshes automatically.
    source.write_text("fresh-cookie-data\n", encoding="utf-8")
    newer_time = session.stat().st_mtime + 20
    os.utime(source, (newer_time, newer_time))
    prepare_session_cookiefile(source, state_dir)
    assert session.read_text(encoding="utf-8") == "fresh-cookie-data\n"


def test_classify_exception_and_dry_run(tmp_path: Path) -> None:
    assert classify_exception("ERROR: Video unavailable") == MigrationStatus.UNAVAILABLE
    assert classify_exception("Sign in to confirm you're not a bot") == MigrationStatus.AUTH_FAILED
    assert classify_exception("HTTP Error 429: Too Many Requests") == MigrationStatus.FAILED

    state_dir = tmp_path / "state"
    store = ProgressStore(state_dir / "progress.jsonl")
    store.load()
    out = io.StringIO()

    summary = run_migration(
        [_entry("vid00000001"), _entry("vid00000002")],
        store,
        state_dir,
        dry_run=True,
        out=out,
    )
    assert summary.dry_run is True
    assert summary.attempted == 0
    assert "Dry run: 2 pending videos" in out.getvalue()
