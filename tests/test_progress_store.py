"""Tests for append-only JSONL progress checkpoint persistence."""

from pathlib import Path

from yt_history_migrator.models import MigrationStatus, ProgressRecord
from yt_history_migrator.progress_store import ProgressStore


def test_progress_store_persists_reloads_and_controls_resume_skip(tmp_path: Path) -> None:
    progress_file = tmp_path / "state" / "progress.jsonl"
    store = ProgressStore(progress_file)
    store.load()
    assert store.total_recorded() == 0

    store.record(
        ProgressRecord(
            video_id="vid_watched",
            status=MigrationStatus.WATCHED,
            title="Watched Video",
            updated_at="2026-11-20T05:00:00+00:00",
        )
    )
    store.record(
        ProgressRecord(
            video_id="vid_unavail",
            status=MigrationStatus.UNAVAILABLE,
            title="Private Video",
            updated_at="2026-11-20T05:00:05+00:00",
            reason="Private video",
        )
    )
    store.record(
        ProgressRecord(
            video_id="vid_failed1",
            status=MigrationStatus.FAILED,
            title="Timeout Video",
            updated_at="2026-11-20T05:00:10+00:00",
            reason="HTTP 429",
        )
    )
    store.record(
        ProgressRecord(
            video_id="vid_autherr",
            status=MigrationStatus.AUTH_FAILED,
            title="Auth Video",
            updated_at="2026-11-20T05:00:15+00:00",
            reason="Unable to mark watched",
        )
    )

    reloaded = ProgressStore(progress_file)
    reloaded.load()

    assert reloaded.total_recorded() == 4
    assert reloaded.should_skip("vid_watched") is True
    assert reloaded.should_skip("vid_unavail") is True
    assert reloaded.should_skip("vid_unavail", retry_unavailable=True) is False
    assert reloaded.should_skip("vid_failed1") is False
    assert reloaded.should_skip("vid_autherr") is False
    assert reloaded.should_skip("vid_unknown") is False

    # Re-recording a previously FAILED video as WATCHED updates status on reload.
    reloaded.record(
        ProgressRecord(
            video_id="vid_failed1",
            status=MigrationStatus.WATCHED,
            title="Timeout Video",
            updated_at="2026-11-20T05:01:00+00:00",
        )
    )

    final_store = ProgressStore(progress_file)
    final_store.load()
    counts = final_store.summary_counts()
    assert final_store.should_skip("vid_failed1") is True
    assert counts[MigrationStatus.WATCHED] == 2
    assert counts[MigrationStatus.UNAVAILABLE] == 1
    assert counts[MigrationStatus.FAILED] == 0
    assert counts[MigrationStatus.AUTH_FAILED] == 1

    final_store.clear()
    assert final_store.total_recorded() == 0
    assert not progress_file.exists()
