"""yt-dlp wrapper with watch-ping verification, cookie protection, and checkpointing."""

from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import random
import shutil
import threading
import time
from typing import Callable, TextIO, TypedDict
import sys

import yt_dlp

from yt_history_migrator.fast_marker import FastYoutubeMarker, PrefetchedPlayer
from yt_history_migrator.models import MigrationStatus, ProgressRecord, WatchEntry
from yt_history_migrator.progress_store import ProgressStore

ProgressCallback = Callable[[int, int, WatchEntry, MigrationStatus, str | None], None]
StatusCallback = Callable[[str, str | None], None]

_FALLBACK_NODE_PATHS = (
    "/opt/homebrew/opt/node@22/bin/node",
    "/opt/homebrew/bin/node",
    "/usr/local/bin/node",
)
_UNAVAILABLE_PATTERNS = (
    "video unavailable",
    "private video",
    "video has been removed",
    "account associated with this video has been terminated",
    "no longer available",
    "copyright claim",
    "members-only content",
    "join this channel to get access",
    "premieres in",
    "live event will begin",
)
_AUTH_FAILURE_PATTERNS = (
    "sign in to confirm",
    "not a bot",
    "cookies are no longer valid",
    "unable to mark",
)


class YtDlpWatchLogger:
    """Capture yt-dlp screen, warning, and error messages for watch verification.

    yt_dlp.YoutubeDL.to_screen() routes messages to logger.debug() (not info()),
    report_warning() routes to logger.warning(), and to_stderr() routes to
    logger.error(). Inspecting debug messages lets us verify that YouTube's
    videostatsPlaybackUrl and videostatsWatchtimeUrl pings actually fired.
    """

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def reset(self) -> None:
        """Clear captured output before processing the next video."""
        self.messages.clear()
        self.warnings.clear()
        self.errors.clear()

    def debug(self, msg: str) -> None:
        """Record screen/debug output emitted by yt-dlp."""
        self.messages.append(msg)

    def info(self, msg: str) -> None:
        """Record info output emitted by yt-dlp."""
        self.messages.append(msg)

    def warning(self, msg: str) -> None:
        """Record warning output emitted by yt-dlp."""
        self.warnings.append(msg)

    def error(self, msg: str) -> None:
        """Record error output emitted by yt-dlp."""
        self.errors.append(msg)

    def confirmed_marked_watched(self) -> bool:
        """Return True only when yt-dlp emitted the full watch-ping confirmation."""
        has_full_ping = any("Marking fully watched" in msg for msg in self.messages)
        has_mark_warning = any("Unable to mark" in warn for warn in self.warnings)
        return has_full_ping and not has_mark_warning

    def failure_summary(self) -> str:
        """Return a concise single-line summary of captured errors and warnings."""
        parts = [text.strip() for text in (*self.errors, *self.warnings) if text.strip()]
        joined = " | ".join(parts)
        return joined[:400]


class _YdlOptions(TypedDict, total=False):
    simulate: bool
    skip_download: bool
    mark_watched: bool
    ignore_no_formats_error: bool
    noplaylist: bool
    quiet: bool
    no_warnings: bool
    socket_timeout: int
    extractor_retries: int
    logger: YtDlpWatchLogger
    js_runtimes: dict[str, dict[str, str]]
    cookiefile: str
    cookiesfrombrowser: tuple[str, ...]


@dataclass(frozen=True)
class MigrationSummary:
    """Counts and termination state produced by a migration run."""

    total_candidates: int
    already_skipped: int
    attempted: int
    watched: int
    unavailable: int
    failed: int
    auth_failed: int
    aborted_on_auth_circuit_breaker: bool = False
    interrupted: bool = False
    dry_run: bool = False


def detect_node_path() -> str | None:
    """Locate Node.js binary for yt-dlp's YouTube JS player challenge solver."""
    which_node = shutil.which("node")
    if which_node:
        return which_node
    for candidate in _FALLBACK_NODE_PATHS:
        if Path(candidate).is_file():
            return candidate
    return None


def prepare_session_cookiefile(source_cookies: Path, state_dir: Path) -> Path:
    """Copy cookies.txt into state_dir so yt-dlp never mutates the user's original export."""
    if not source_cookies.exists() or not source_cookies.is_file():
        raise FileNotFoundError(f"Cookies file not found: {source_cookies}")
    if source_cookies.stat().st_size == 0:
        raise ValueError(f"Cookies file is empty: {source_cookies}")

    state_dir.mkdir(parents=True, exist_ok=True)
    session_cookie_path = state_dir / "session-cookies.txt"

    should_copy = (
        not session_cookie_path.exists()
        or source_cookies.stat().st_mtime > session_cookie_path.stat().st_mtime
    )
    if should_copy:
        shutil.copy2(source_cookies, session_cookie_path)
    return session_cookie_path


def parse_cookies_from_browser(spec: str) -> tuple[str, ...]:
    """Parse 'browser' or 'browser:profile' into yt-dlp's cookiesfrombrowser tuple."""
    browser, _, profile = spec.partition(":")
    cleaned_browser = browser.strip()
    if not cleaned_browser:
        raise ValueError(f"Invalid --cookies-from-browser value: '{spec}'")
    cleaned_profile = profile.strip()
    if cleaned_profile:
        return (cleaned_browser, cleaned_profile)
    return (cleaned_browser,)


def classify_exception(exc_text: str) -> MigrationStatus:
    """Classify a yt-dlp extraction failure as unavailable, auth_failed, or transient failed."""
    lowered = exc_text.lower()
    if any(pattern in lowered for pattern in _UNAVAILABLE_PATTERNS):
        return MigrationStatus.UNAVAILABLE
    if any(pattern in lowered for pattern in _AUTH_FAILURE_PATTERNS):
        return MigrationStatus.AUTH_FAILED
    return MigrationStatus.FAILED


def build_ydl_options(
    *,
    watch_logger: YtDlpWatchLogger,
    session_cookiefile: Path | None,
    cookies_from_browser: str | None,
    node_path: str | None,
) -> _YdlOptions:
    """Construct YoutubeDL options without using Any."""
    opts: _YdlOptions = {
        "simulate": True,
        "skip_download": True,
        "mark_watched": True,
        "ignore_no_formats_error": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 20,
        "extractor_retries": 2,
        "logger": watch_logger,
    }
    if node_path is not None:
        opts["js_runtimes"] = {"node": {"path": node_path}}
    if session_cookiefile is not None:
        opts["cookiefile"] = str(session_cookiefile)
    if cookies_from_browser is not None:
        opts["cookiesfrombrowser"] = parse_cookies_from_browser(cookies_from_browser)
    return opts


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _format_entry_date(entry: WatchEntry) -> str:
    if entry.watched_at_raw:
        return entry.watched_at_raw[:10]
    return "unknown-date"


def _format_entry_label(entry: WatchEntry) -> str:
    title = entry.title or "(untitled)"
    if entry.channel_name:
        return f"{entry.channel_name} - {title}"
    return title


def _process_single_video(
    ydl: yt_dlp.YoutubeDL,
    watch_logger: YtDlpWatchLogger,
    entry: WatchEntry,
) -> tuple[MigrationStatus, str | None]:
    watch_logger.reset()
    try:
        ydl.extract_info(entry.canonical_url, download=False)
    except Exception as exc:
        combined = f"{exc} | {watch_logger.failure_summary()}".strip(" |")
        return (classify_exception(combined), combined[:400])

    if watch_logger.confirmed_marked_watched():
        return (MigrationStatus.WATCHED, None)

    summary = watch_logger.failure_summary()
    reason = summary or (
        "yt-dlp did not emit 'Marking fully watched' "
        "(cookies may be logged out or rotated)"
    )
    return (MigrationStatus.AUTH_FAILED, reason)


def _print_dry_run_preview(
    pending: list[WatchEntry],
    already_skipped: int,
    out: TextIO,
) -> None:
    print(
        f"Dry run: {len(pending)} pending videos ({already_skipped} already completed/skipped).",
        file=out,
    )
    if not pending:
        return
    print("First up (oldest -> newest):", file=out)
    for idx, entry in enumerate(pending[:5], start=1):
        print(
            f"  {idx}. {entry.video_id} | {_format_entry_date(entry)} | {_format_entry_label(entry)}",
            file=out,
        )
    if len(pending) <= 5:
        return
    print("Last up:", file=out)
    for idx, entry in enumerate(pending[-3:], start=len(pending) - 2):
        print(
            f"  {idx}. {entry.video_id} | {_format_entry_date(entry)} | {_format_entry_label(entry)}",
            file=out,
        )


def _interruptible_wait(
    seconds: float,
    stop_event: threading.Event | None,
    sleep_fn: Callable[[float], None],
) -> bool:
    if seconds <= 0:
        return False
    if stop_event is not None:
        return stop_event.wait(seconds)
    sleep_fn(seconds)
    return False


def _emit_status(cb: StatusCallback | None, phase: str, detail: str | None = None) -> None:
    if cb is not None:
        cb(phase, detail)


def _maybe_sleep_between_videos(
    index: int,
    total: int,
    *,
    min_delay: float,
    max_delay: float,
    cooldown_every: int,
    cooldown_seconds: float,
    sleep_fn: Callable[[float], None],
    stop_event: threading.Event | None,
    on_status_change: StatusCallback | None,
    out: TextIO,
) -> bool:
    if index >= total:
        return False
    delay = random.uniform(min_delay, max_delay) if max_delay > 0 else 0.0
    if _interruptible_wait(delay, stop_event, sleep_fn):
        return True
    if cooldown_every > 0 and cooldown_seconds > 0 and index % cooldown_every == 0:
        _emit_status(
            on_status_change,
            "cooldown",
            f"Safety cooldown pause ({cooldown_seconds:.0f}s) after {index} videos...",
        )
        print(
            f"Cooldown pause ({cooldown_seconds:.1f}s) after {index} videos...",
            file=out,
        )
        interrupted = _interruptible_wait(cooldown_seconds, stop_event, sleep_fn)
        if not interrupted:
            _emit_status(on_status_change, "importing", None)
        return interrupted
    return False


def _record_and_emit(
    *,
    index: int,
    total: int,
    entry: WatchEntry,
    status: MigrationStatus,
    reason: str | None,
    store: ProgressStore,
    counts: dict[MigrationStatus, int],
    consecutive_auth_failures: int,
    max_consecutive_auth_failures: int,
    on_video_result: ProgressCallback | None,
    output: TextIO,
) -> tuple[int, bool]:
    counts[status] += 1
    store.record(
        ProgressRecord(
            video_id=entry.video_id,
            status=status,
            title=entry.title,
            updated_at=_utc_now_iso(),
            reason=reason,
        )
    )
    if on_video_result is not None:
        on_video_result(index, total, entry, status, reason)

    reason_suffix = f" ({reason})" if reason else ""
    print(
        f"[{index}/{total}] {status.value.upper()} | "
        f"{entry.video_id} | {_format_entry_date(entry)} | "
        f"{_format_entry_label(entry)}{reason_suffix}",
        file=output,
        flush=True,
    )

    next_auth_failures = consecutive_auth_failures + 1 if status == MigrationStatus.AUTH_FAILED else 0
    if next_auth_failures >= max_consecutive_auth_failures:
        print(
            f"\nStopping early: {next_auth_failures} consecutive auth/tracking failures.\n"
            "Run './.venv/bin/yt-migrate login --fresh' to sign in again and refresh data/cookies.txt.",
            file=output,
        )
        return (next_auth_failures, True)
    return (next_auth_failures, False)


def _run_fast_loop(
    pending: list[WatchEntry],
    *,
    store: ProgressStore,
    session_cookiefile: Path | None,
    cookies_from_browser: str | None,
    prefetch_workers: int,
    min_delay: float,
    max_delay: float,
    cooldown_every: int,
    cooldown_seconds: float,
    max_consecutive_auth_failures: int,
    sleep_fn: Callable[[float], None],
    stop_event: threading.Event | None,
    on_video_result: ProgressCallback | None,
    on_status_change: StatusCallback | None,
    output: TextIO,
) -> tuple[dict[MigrationStatus, int], int, bool, bool]:
    counts: dict[MigrationStatus, int] = {status: 0 for status in MigrationStatus}
    attempted = 0
    consecutive_auth_failures = 0
    aborted_auth = False
    interrupted = False

    browser_tuple = parse_cookies_from_browser(cookies_from_browser) if cookies_from_browser else None
    workers = max(1, prefetch_workers)
    window_size = workers * 2
    pending_iter = iter(enumerate(pending, start=1))
    in_flight: deque[tuple[int, WatchEntry, Future[PrefetchedPlayer]]] = deque()

    def fill_window(pool: ThreadPoolExecutor, marker: FastYoutubeMarker) -> None:
        while len(in_flight) < window_size:
            if stop_event is not None and stop_event.is_set():
                return
            nxt = next(pending_iter, None)
            if nxt is None:
                return
            idx, item = nxt
            in_flight.append((idx, item, pool.submit(marker.prefetch_player, item.video_id)))

    _emit_status(on_status_change, "connecting", "Connecting to YouTube session...")
    with FastYoutubeMarker(
        session_cookiefile=session_cookiefile,
        cookies_from_browser=browser_tuple,
    ) as marker:
        _emit_status(on_status_change, "importing", None)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            try:
                fill_window(pool, marker)
                while in_flight:
                    if stop_event is not None and stop_event.is_set():
                        interrupted = True
                        break

                    index, entry, future = in_flight.popleft()
                    fill_window(pool, marker)
                    prefetched = future.result()

                    if prefetched.rate_limited:
                        _emit_status(
                            on_status_change,
                            "rate_limited",
                            "YouTube rate-limited requests (HTTP 429). Cooling down 25s before retrying...",
                        )
                        print("Rate limited (HTTP 429); cooling down 25s before retry...", file=output)
                        if _interruptible_wait(25.0, stop_event, sleep_fn):
                            interrupted = True
                            break
                        _emit_status(on_status_change, "importing", None)
                        prefetched = marker.prefetch_player(entry.video_id)
                        if prefetched.rate_limited:
                            prefetched = PrefetchedPlayer(
                                video_id=entry.video_id,
                                outcome=MigrationStatus.FAILED,
                                reason="HTTP 429 Too Many Requests",
                            )

                    status, reason = marker.commit_watchtime(prefetched)
                    attempted += 1
                    consecutive_auth_failures, aborted_auth = _record_and_emit(
                        index=index,
                        total=len(pending),
                        entry=entry,
                        status=status,
                        reason=reason,
                        store=store,
                        counts=counts,
                        consecutive_auth_failures=consecutive_auth_failures,
                        max_consecutive_auth_failures=max_consecutive_auth_failures,
                        on_video_result=on_video_result,
                        output=output,
                    )
                    if aborted_auth:
                        break

                    if _maybe_sleep_between_videos(
                        index,
                        len(pending),
                        min_delay=min_delay,
                        max_delay=max_delay,
                        cooldown_every=cooldown_every,
                        cooldown_seconds=cooldown_seconds,
                        sleep_fn=sleep_fn,
                        stop_event=stop_event,
                        on_status_change=on_status_change,
                        out=output,
                    ):
                        interrupted = True
                        break
            except KeyboardInterrupt:
                interrupted = True
                print("\nInterrupted by user. Progress up to the last completed video is saved.", file=output)
            finally:
                for _, _, remaining_future in in_flight:
                    remaining_future.cancel()

    return (counts, attempted, aborted_auth, interrupted)


def _run_ytdlp_loop(
    pending: list[WatchEntry],
    *,
    store: ProgressStore,
    session_cookiefile: Path | None,
    cookies_from_browser: str | None,
    min_delay: float,
    max_delay: float,
    cooldown_every: int,
    cooldown_seconds: float,
    max_consecutive_auth_failures: int,
    sleep_fn: Callable[[float], None],
    stop_event: threading.Event | None,
    on_video_result: ProgressCallback | None,
    on_status_change: StatusCallback | None,
    output: TextIO,
) -> tuple[dict[MigrationStatus, int], int, bool, bool]:
    watch_logger = YtDlpWatchLogger()
    ydl_opts = build_ydl_options(
        watch_logger=watch_logger,
        session_cookiefile=session_cookiefile,
        cookies_from_browser=cookies_from_browser,
        node_path=detect_node_path(),
    )

    counts: dict[MigrationStatus, int] = {status: 0 for status in MigrationStatus}
    attempted = 0
    consecutive_auth_failures = 0
    aborted_auth = False
    interrupted = False

    _emit_status(on_status_change, "connecting", "Connecting to YouTube session...")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        _emit_status(on_status_change, "importing", None)
        try:
            for index, entry in enumerate(pending, start=1):
                if stop_event is not None and stop_event.is_set():
                    interrupted = True
                    break

                status, reason = _process_single_video(ydl, watch_logger, entry)
                attempted += 1
                consecutive_auth_failures, aborted_auth = _record_and_emit(
                    index=index,
                    total=len(pending),
                    entry=entry,
                    status=status,
                    reason=reason,
                    store=store,
                    counts=counts,
                    consecutive_auth_failures=consecutive_auth_failures,
                    max_consecutive_auth_failures=max_consecutive_auth_failures,
                    on_video_result=on_video_result,
                    output=output,
                )
                if aborted_auth:
                    break

                if _maybe_sleep_between_videos(
                    index,
                    len(pending),
                    min_delay=min_delay,
                    max_delay=max_delay,
                    cooldown_every=cooldown_every,
                    cooldown_seconds=cooldown_seconds,
                    sleep_fn=sleep_fn,
                    stop_event=stop_event,
                    on_status_change=on_status_change,
                    out=output,
                ):
                    interrupted = True
                    break
        except KeyboardInterrupt:
            interrupted = True
            print("\nInterrupted by user. Progress up to the last completed video is saved.", file=output)

    return (counts, attempted, aborted_auth, interrupted)


def run_migration(
    entries: list[WatchEntry],
    store: ProgressStore,
    state_dir: Path,
    *,
    cookies_path: Path | None = None,
    cookies_from_browser: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    min_delay: float = 0.15,
    max_delay: float = 0.35,
    cooldown_every: int = 250,
    cooldown_seconds: float = 5.0,
    prefetch_workers: int = 4,
    use_fast_engine: bool = True,
    retry_unavailable: bool = False,
    max_consecutive_auth_failures: int = 3,
    sleep_fn: Callable[[float], None] = time.sleep,
    stop_event: threading.Event | None = None,
    on_video_result: ProgressCallback | None = None,
    on_status_change: StatusCallback | None = None,
    out: TextIO | None = None,
) -> MigrationSummary:
    """Replay watch history entries oldest -> newest and persist progress after every video."""
    output = out if out is not None else sys.stdout
    if min_delay < 0 or max_delay < min_delay:
        raise ValueError("Expected 0 <= min_delay <= max_delay")
    if limit is not None and limit <= 0:
        raise ValueError("Expected --limit to be a positive integer")

    filtered_pending = [
        entry
        for entry in entries
        if not store.should_skip(entry.video_id, retry_unavailable=retry_unavailable)
    ]
    already_skipped = len(entries) - len(filtered_pending)
    pending = filtered_pending[:limit] if limit is not None else filtered_pending

    if dry_run:
        _print_dry_run_preview(pending, already_skipped, output)
        return MigrationSummary(
            total_candidates=len(entries),
            already_skipped=already_skipped,
            attempted=0,
            watched=0,
            unavailable=0,
            failed=0,
            auth_failed=0,
            dry_run=True,
        )

    if cookies_path is None and cookies_from_browser is None:
        raise ValueError("Provide --cookies <cookies.txt> or --cookies-from-browser (or use --dry-run)")

    if not pending:
        print(
            f"Nothing left to migrate ({already_skipped} videos already completed/skipped).",
            file=output,
        )
        return MigrationSummary(
            total_candidates=len(entries),
            already_skipped=already_skipped,
            attempted=0,
            watched=0,
            unavailable=0,
            failed=0,
            auth_failed=0,
        )

    session_cookiefile = (
        prepare_session_cookiefile(cookies_path, state_dir)
        if cookies_path is not None
        else None
    )

    is_real_ytdlp = getattr(yt_dlp.YoutubeDL, "__module__", "") == "yt_dlp.YoutubeDL"
    runner = _run_fast_loop if (use_fast_engine and is_real_ytdlp) else _run_ytdlp_loop

    common_kwargs = {
        "store": store,
        "session_cookiefile": session_cookiefile,
        "cookies_from_browser": cookies_from_browser,
        "min_delay": min_delay,
        "max_delay": max_delay,
        "cooldown_every": cooldown_every,
        "cooldown_seconds": cooldown_seconds,
        "max_consecutive_auth_failures": max_consecutive_auth_failures,
        "sleep_fn": sleep_fn,
        "stop_event": stop_event,
        "on_video_result": on_video_result,
        "on_status_change": on_status_change,
        "output": output,
    }

    with store:
        if runner is _run_fast_loop:
            counts, attempted, aborted_auth, interrupted = _run_fast_loop(
                pending,
                prefetch_workers=prefetch_workers,
                **common_kwargs,
            )
        else:
            counts, attempted, aborted_auth, interrupted = _run_ytdlp_loop(
                pending,
                **common_kwargs,
            )

    return MigrationSummary(
        total_candidates=len(entries),
        already_skipped=already_skipped,
        attempted=attempted,
        watched=counts[MigrationStatus.WATCHED],
        unavailable=counts[MigrationStatus.UNAVAILABLE],
        failed=counts[MigrationStatus.FAILED],
        auth_failed=counts[MigrationStatus.AUTH_FAILED],
        aborted_on_auth_circuit_breaker=aborted_auth,
        interrupted=interrupted,
    )
