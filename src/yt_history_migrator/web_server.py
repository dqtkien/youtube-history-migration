"""Local HTTP backend for the 3-step YouTube watch history migration wizard."""

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import secrets
import threading
import time
from typing import cast
from urllib.parse import unquote, urlparse

from yt_dlp.cookies import YoutubeDLCookieJar

from yt_history_migrator.auth import (
    BrowserLoginSession,
    _close_browser_process,
    finalize_browser_login_session,
    has_youtube_auth_cookies,
    list_available_browsers,
    start_browser_login_session,
)
from yt_history_migrator.fast_marker import SPEED_PRESETS, SpeedPreset
from yt_history_migrator.filter_dedup import parse_date_bound, prepare_watch_entries
from yt_history_migrator.importer import run_migration
from yt_history_migrator.models import (
    FilterOptions,
    FilterReport,
    MigrationStatus,
    RawHistoryItem,
    SkipReason,
    WatchEntry,
)
from yt_history_migrator.parser import parse_takeout_file
from yt_history_migrator.progress_store import ProgressStore

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_ALLOWED_UPLOAD_EXTENSIONS = frozenset({".zip", ".html", ".htm", ".json"})
_SECONDS_PER_VIDEO_ESTIMATE = SPEED_PRESETS["fast"].estimated_seconds_per_video
_UPLOAD_CHUNK_BYTES = 1024 * 1024
_MAX_JSON_BODY_BYTES = 64 * 1024
_STATIC_ASSETS: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
_SUMMARY_SCOPES = ("test5", "this_year", "last_3_years", "all")
_AUTO_TAKEOUT_CANDIDATES = (
    "uploaded-takeout.zip",
    "uploaded-takeout.html",
    "uploaded-takeout.htm",
    "uploaded-takeout.json",
    "watch-history.html",
    "watch-history.json",
)


class HttpApiError(Exception):
    """Structured HTTP error carrying status code and user-friendly message."""

    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _current_year() -> int:
    return datetime.now(timezone.utc).year


def _resolve_scope(
    scope: str,
    *,
    after_date: str | None = None,
    before_date: str | None = None,
) -> tuple[str | None, str | None, int | None]:
    """Resolve UI scope key (including legacy year aliases) to (after_date, before_date, limit)."""
    year = _current_year()
    if scope == "test5":
        return (None, None, 5)
    if scope in ("this_year", "2026"):
        return (f"{year}-01-01", None, None)
    if scope in ("last_3_years", "2024"):
        return (f"{year - 2}-01-01", None, None)
    if scope == "all":
        return (None, None, None)
    if scope != "custom":
        raise HttpApiError(HTTPStatus.BAD_REQUEST, f"Unknown scope: '{scope}'")

    try:
        after_epoch = parse_date_bound(after_date, end_of_day=False)
        before_epoch = parse_date_bound(before_date, end_of_day=True)
    except ValueError as exc:
        raise HttpApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc

    if after_epoch is not None and before_epoch is not None and after_epoch > before_epoch:
        raise HttpApiError(HTTPStatus.BAD_REQUEST, "Start date must be on or before end date.")

    return (after_date, before_date, None)


def _serialize_preview_entry(entry: WatchEntry) -> dict[str, object]:
    return {
        "videoId": entry.video_id,
        "url": entry.canonical_url,
        "title": entry.title or "(untitled)",
        "channel": entry.channel_name or "",
        "watchedAt": entry.watched_at_raw or "",
    }


@dataclass
class MigrationRuntimeState:
    """Thread-safe snapshot of the background migration runner."""

    running: bool = False
    stopping: bool = False
    finished: bool = False
    phase: str = "idle"
    phase_detail: str | None = None
    scope: str = "test5"
    speed: str = "fast"
    after_date: str | None = None
    before_date: str | None = None
    include_music: bool = False
    exclude_shorts: bool = False
    retry_unavailable: bool = False
    current_index: int = 0
    total_target: int = 0
    already_skipped: int = 0
    watched: int = 0
    unavailable: int = 0
    failed: int = 0
    auth_failed: int = 0
    aborted_on_auth: bool = False
    interrupted: bool = False
    last_error: str | None = None
    started_at: float | None = None
    recent_events: deque[dict[str, object]] = field(default_factory=lambda: deque(maxlen=200))


class WebAppController:
    """Coordinates Takeout state, isolated browser auth, and background migration."""

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = project_dir
        self.data_dir = project_dir / "data"
        self.state_dir = project_dir / "state"
        self.output_dir = project_dir / "output"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self.csrf_token = secrets.token_urlsafe(24)
        self._lock = threading.RLock()

        self._takeout_path: Path | None = None
        self._takeout_display_name: str | None = None
        self._raw_items: list[RawHistoryItem] = []

        self._login_session: BrowserLoginSession | None = None
        self._authenticated: bool = False
        self._account_label: str | None = None
        self._last_auth_error: str | None = None

        self._migration = MigrationRuntimeState()
        self._stop_event = threading.Event()
        self._migration_thread: threading.Thread | None = None
        self._shutdown_callback: Callable[[], None] | None = None

        self._load_initial_takeout()
        self._load_initial_cookies()

    def set_shutdown_callback(self, callback: Callable[[], None]) -> None:
        """Register the server shutdown hook invoked by POST /api/server/shutdown."""
        with self._lock:
            self._shutdown_callback = callback

    @property
    def cookies_path(self) -> Path:
        """Default Netscape cookies destination path."""
        return self.data_dir / "cookies.txt"

    @property
    def progress_path(self) -> Path:
        """Append-only checkpoint path."""
        return self.state_dir / "progress.jsonl"

    def _load_initial_takeout(self) -> None:
        existing_files = [
            self.data_dir / name
            for name in _AUTO_TAKEOUT_CANDIDATES
            if (self.data_dir / name).is_file() and (self.data_dir / name).stat().st_size > 0
        ]
        if not existing_files:
            return
        newest = max(existing_files, key=lambda path: path.stat().st_mtime)
        try:
            items = parse_takeout_file(newest)
        except Exception:
            return
        self._takeout_path = newest
        self._takeout_display_name = newest.name
        self._raw_items = items

    def _load_initial_cookies(self) -> None:
        cookies_file = self.cookies_path
        if not cookies_file.is_file() or cookies_file.stat().st_size == 0:
            return
        jar = YoutubeDLCookieJar()
        try:
            jar.load(str(cookies_file), ignore_discard=True, ignore_expires=True)
        except Exception:
            return
        self._authenticated = has_youtube_auth_cookies(jar)
        if not self._authenticated:
            return
        meta_path = self.state_dir / "account-meta.json"
        if not meta_path.is_file():
            return
        try:
            decoded: object = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            return
        if isinstance(decoded, dict) and isinstance(decoded.get("accountLabel"), str):
            self._account_label = decoded["accountLabel"]

    def _open_store(self) -> ProgressStore:
        store = ProgressStore(self.progress_path)
        store.load()
        return store

    def _checkpoint_summary(self, store: ProgressStore | None = None) -> dict[str, int]:
        active_store = store if store is not None else self._open_store()
        counts = active_store.summary_counts()
        return {
            "totalRecorded": active_store.total_recorded(),
            "watched": counts[MigrationStatus.WATCHED],
            "unavailable": counts[MigrationStatus.UNAVAILABLE],
            "failed": counts[MigrationStatus.FAILED],
            "authFailed": counts[MigrationStatus.AUTH_FAILED],
        }

    def _compute_scope_summary(
        self,
        entries: list[WatchEntry],
        store: ProgressStore,
        *,
        retry_unavailable: bool = False,
        seconds_per_video: float = _SECONDS_PER_VIDEO_ESTIMATE,
    ) -> dict[str, dict[str, object]]:
        pending_entries = [
            entry
            for entry in entries
            if not store.should_skip(entry.video_id, retry_unavailable=retry_unavailable)
        ]
        summaries: dict[str, dict[str, object]] = {}

        for scope_key in _SUMMARY_SCOPES:
            after_date, _before_date, limit = _resolve_scope(scope_key)
            after_epoch = parse_date_bound(after_date, end_of_day=False)
            if after_epoch is None:
                eligible = len(entries)
                pending = len(pending_entries)
            else:
                eligible = sum(
                    1
                    for entry in entries
                    if entry.watched_at_epoch is None or entry.watched_at_epoch >= after_epoch
                )
                pending = sum(
                    1
                    for entry in pending_entries
                    if entry.watched_at_epoch is None or entry.watched_at_epoch >= after_epoch
                )

            if limit is not None:
                pending = min(pending, limit)
                eligible = min(eligible, limit)

            summaries[scope_key] = {
                "eligible": eligible,
                "pending": pending,
                "etaSeconds": round(pending * seconds_per_video),
            }
        return summaries

    def get_takeout_summary(
        self,
        *,
        include_music: bool = False,
        exclude_shorts: bool = False,
        retry_unavailable: bool = False,
        speed: str = "fast",
    ) -> dict[str, object]:
        """Return summary statistics and scope counts for the loaded Takeout export."""
        preset = SPEED_PRESETS.get(speed)
        if preset is None:
            raise HttpApiError(HTTPStatus.BAD_REQUEST, f"Unknown speed preset: '{speed}'")

        with self._lock:
            if self._takeout_path is None:
                return {"loaded": False}

            options = FilterOptions(
                include_music=include_music,
                exclude_shorts=exclude_shorts,
            )
            report = prepare_watch_entries(self._raw_items, options)
            store = self._open_store()
            skipped = {
                reason.value: report.skipped_counts.get(reason, 0)
                for reason in SkipReason
                if report.skipped_counts.get(reason, 0) > 0
            }
            oldest_date = report.entries[0].watched_at_raw if report.entries else None
            newest_date = report.entries[-1].watched_at_raw if report.entries else None
            year = _current_year()

            return {
                "loaded": True,
                "filename": self._takeout_display_name or self._takeout_path.name,
                "totalRaw": report.total_raw,
                "uniqueVideos": len(report.entries),
                "duplicatesMerged": report.duplicate_watches_collapsed,
                "skippedCounts": skipped,
                "oldestDate": oldest_date,
                "newestDate": newest_date,
                "currentYear": year,
                "threeYearsAgoYear": year - 2,
                "includeMusic": include_music,
                "excludeShorts": exclude_shorts,
                "retryUnavailable": retry_unavailable,
                "scopeCounts": self._compute_scope_summary(
                    report.entries,
                    store,
                    retry_unavailable=retry_unavailable,
                    seconds_per_video=preset.estimated_seconds_per_video,
                ),
            }

    def preview_scope(
        self,
        *,
        scope: str,
        speed: str = "fast",
        include_music: bool,
        exclude_shorts: bool,
        after_date: str | None = None,
        before_date: str | None = None,
        retry_unavailable: bool = False,
    ) -> dict[str, object]:
        """Compute filtered eligible, pending, and sample queue entries for chosen UI options."""
        resolved_after, resolved_before, limit = _resolve_scope(
            scope,
            after_date=after_date,
            before_date=before_date,
        )
        preset = SPEED_PRESETS.get(speed)
        if preset is None:
            raise HttpApiError(HTTPStatus.BAD_REQUEST, f"Unknown speed preset: '{speed}'")

        with self._lock:
            if self._takeout_path is None:
                raise HttpApiError(HTTPStatus.BAD_REQUEST, "Load a Takeout file in Step 1 first.")
            options = FilterOptions(
                include_music=include_music,
                exclude_shorts=exclude_shorts,
                after_date=resolved_after,
                before_date=resolved_before,
            )
            report: FilterReport = prepare_watch_entries(self._raw_items, options)
            store = self._open_store()
            pending_entries = [
                entry
                for entry in report.entries
                if not store.should_skip(entry.video_id, retry_unavailable=retry_unavailable)
            ]
            pending_count = len(pending_entries)
            already_skipped = len(report.entries) - pending_count
            queued = pending_entries[:limit] if limit is not None else pending_entries
            target_count = len(queued)

            sample_first = [_serialize_preview_entry(entry) for entry in queued[:3]]
            sample_last = (
                [_serialize_preview_entry(entry) for entry in queued[max(3, len(queued) - 3) :]]
                if len(queued) > 3
                else []
            )

            return {
                "scope": scope,
                "speed": speed,
                "afterDate": resolved_after,
                "beforeDate": resolved_before,
                "retryUnavailable": retry_unavailable,
                "totalEligible": len(report.entries),
                "alreadySkipped": already_skipped,
                "pendingCount": pending_count,
                "targetCount": target_count,
                "etaSeconds": round(target_count * preset.estimated_seconds_per_video),
                "sampleFirst": sample_first,
                "sampleLast": sample_last,
            }

    def upload_takeout_stream(
        self,
        *,
        raw_filename: str,
        content_length: int,
        stream: io.BufferedIOBase | object,
    ) -> dict[str, object]:
        """Stream an uploaded .zip/.html/.json Takeout file to disk and parse it."""
        if content_length <= 0:
            raise HttpApiError(HTTPStatus.BAD_REQUEST, "Uploaded file is empty.")

        safe_name = Path(unquote(raw_filename)).name
        suffix = Path(safe_name).suffix.lower()
        if suffix not in _ALLOWED_UPLOAD_EXTENSIONS:
            raise HttpApiError(
                HTTPStatus.BAD_REQUEST,
                "Unsupported file type. Upload your Google Takeout .zip, watch-history.html, or watch-history.json.",
            )

        with self._lock:
            if self._migration.running:
                raise HttpApiError(
                    HTTPStatus.CONFLICT,
                    "Pause the active import before uploading a different Takeout file.",
                )

        tmp_path = self.data_dir / f".upload-tmp{suffix}"
        reader = cast(io.BufferedIOBase, stream)
        remaining = content_length
        try:
            with tmp_path.open("wb") as handle:
                while remaining > 0:
                    chunk = reader.read(min(_UPLOAD_CHUNK_BYTES, remaining))
                    if not chunk:
                        break
                    handle.write(chunk)
                    remaining -= len(chunk)
            if remaining > 0:
                raise HttpApiError(HTTPStatus.BAD_REQUEST, "Upload was interrupted before completion.")
            items = parse_takeout_file(tmp_path)
        except HttpApiError:
            tmp_path.unlink(missing_ok=True)
            raise
        except Exception as exc:
            tmp_path.unlink(missing_ok=True)
            raise HttpApiError(HTTPStatus.BAD_REQUEST, f"Could not read Takeout file: {exc}") from exc

        target_path = self.data_dir / f"uploaded-takeout{suffix}"
        with self._lock:
            for ext in _ALLOWED_UPLOAD_EXTENSIONS:
                stale = self.data_dir / f"uploaded-takeout{ext}"
                if stale != target_path:
                    stale.unlink(missing_ok=True)
            tmp_path.replace(target_path)
            self._takeout_path = target_path
            self._takeout_display_name = safe_name
            self._raw_items = items

        return self.get_takeout_summary()

    def get_auth_status(self) -> dict[str, object]:
        """Check login window state and finalize automatically if user closed it."""
        with self._lock:
            session = self._login_session
            if session is not None and not session.is_running():
                self._finalize_locked_session(session)

            return {
                "authenticated": self._authenticated,
                "accountLabel": self._account_label,
                "cookiesPath": str(self.cookies_path) if self._authenticated else None,
                "loginWindowOpen": self._login_session is not None and self._login_session.is_running(),
                "browserName": self._login_session.browser_name if self._login_session else None,
                "availableBrowsers": list_available_browsers(),
                "lastAuthError": self._last_auth_error,
            }

    def _finalize_locked_session(self, session: BrowserLoginSession) -> None:
        self._login_session = None
        try:
            verification = finalize_browser_login_session(
                session,
                cookies_out=self.cookies_path,
            )
            self._authenticated = True
            self._account_label = verification.account_label
            self._last_auth_error = None
        except Exception as exc:
            self._last_auth_error = str(exc)

    def start_auth_window(
        self,
        *,
        browser: str | None,
        fresh: bool,
        reset_progress: bool = False,
    ) -> dict[str, object]:
        """Launch an isolated browser window for destination Gmail / YouTube login."""
        with self._lock:
            if reset_progress:
                if self._migration.running:
                    raise HttpApiError(
                        HTTPStatus.CONFLICT,
                        "Pause the active import before resetting the checkpoint.",
                    )
                self._open_store().clear()
                self._migration = MigrationRuntimeState()

            if self._login_session is not None:
                _close_browser_process(self._login_session.proc, sleep_fn=lambda _s: None)
                self._login_session = None

            try:
                session = start_browser_login_session(
                    state_dir=self.state_dir,
                    preferred_browser=browser,
                    fresh_profile=fresh,
                )
            except (FileNotFoundError, ValueError) as exc:
                raise HttpApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc

            self._login_session = session
            self._last_auth_error = None
            if fresh:
                self._authenticated = False
                self._account_label = None
            return self.get_auth_status()

    def complete_auth_window(self) -> dict[str, object]:
        """Close the open sign-in window, extract cookies, and verify YouTube login."""
        with self._lock:
            session = self._login_session
            if session is None:
                if self._authenticated:
                    return self.get_auth_status()
                raise HttpApiError(
                    HTTPStatus.BAD_REQUEST,
                    "No sign-in window is open. Click 'Open Sign-In Window' first.",
                )

            self._login_session = None
            try:
                verification = finalize_browser_login_session(
                    session,
                    cookies_out=self.cookies_path,
                )
            except Exception as exc:
                self._last_auth_error = str(exc)
                raise HttpApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc

            self._authenticated = True
            self._account_label = verification.account_label
            self._last_auth_error = None
            return self.get_auth_status()

    def start_migration(
        self,
        *,
        scope: str,
        speed: str = "fast",
        include_music: bool,
        exclude_shorts: bool,
        after_date: str | None = None,
        before_date: str | None = None,
        retry_unavailable: bool = False,
    ) -> dict[str, object]:
        """Start background watch history migration for the chosen scope."""
        resolved_after, resolved_before, limit = _resolve_scope(
            scope,
            after_date=after_date,
            before_date=before_date,
        )
        preset = SPEED_PRESETS.get(speed)
        if preset is None:
            raise HttpApiError(HTTPStatus.BAD_REQUEST, f"Unknown speed preset: '{speed}'")

        with self._lock:
            if self._migration.running:
                raise HttpApiError(HTTPStatus.CONFLICT, "Import is already running.")
            if self._takeout_path is None:
                raise HttpApiError(HTTPStatus.BAD_REQUEST, "Choose a Takeout file in Step 1 first.")
            if not self._authenticated or not self.cookies_path.is_file():
                raise HttpApiError(
                    HTTPStatus.BAD_REQUEST,
                    "Sign in to your new YouTube Google account in Step 2 first.",
                )

            options = FilterOptions(
                include_music=include_music,
                exclude_shorts=exclude_shorts,
                after_date=resolved_after,
                before_date=resolved_before,
            )
            report = prepare_watch_entries(self._raw_items, options)
            store = self._open_store()
            pending_count = sum(
                1
                for entry in report.entries
                if not store.should_skip(entry.video_id, retry_unavailable=retry_unavailable)
            )
            already_skipped = len(report.entries) - pending_count
            target_count = min(pending_count, limit) if limit is not None else pending_count

            self._stop_event = threading.Event()
            self._migration = MigrationRuntimeState(
                running=True,
                stopping=False,
                finished=False,
                phase="connecting",
                phase_detail="Connecting to YouTube session...",
                scope=scope,
                speed=speed,
                after_date=resolved_after,
                before_date=resolved_before,
                include_music=include_music,
                exclude_shorts=exclude_shorts,
                retry_unavailable=retry_unavailable,
                total_target=target_count,
                already_skipped=already_skipped,
                started_at=time.monotonic(),
            )

            thread = threading.Thread(
                target=self._run_migration_worker,
                args=(report.entries, store, limit, preset, retry_unavailable),
                daemon=True,
            )
            self._migration_thread = thread
            thread.start()
            return self.get_migration_status()

    def _on_migration_phase(self, phase: str, detail: str | None) -> None:
        with self._lock:
            self._migration.phase = phase
            self._migration.phase_detail = detail

    def _on_video_result(
        self,
        index: int,
        _total: int,
        entry: WatchEntry,
        status: MigrationStatus,
        reason: str | None,
    ) -> None:
        with self._lock:
            self._migration.current_index = index
            if status == MigrationStatus.WATCHED:
                self._migration.watched += 1
            if status == MigrationStatus.UNAVAILABLE:
                self._migration.unavailable += 1
            if status == MigrationStatus.FAILED:
                self._migration.failed += 1
            if status == MigrationStatus.AUTH_FAILED:
                self._migration.auth_failed += 1

            self._migration.recent_events.appendleft({
                "videoId": entry.video_id,
                "url": entry.canonical_url,
                "title": entry.title or "(untitled)",
                "channel": entry.channel_name or "",
                "watchedAt": entry.watched_at_raw or "",
                "status": status.value,
                "reason": reason,
            })

    def _run_migration_worker(
        self,
        entries: list[WatchEntry],
        store: ProgressStore,
        limit: int | None,
        preset: SpeedPreset,
        retry_unavailable: bool,
    ) -> None:
        try:
            summary = run_migration(
                entries,
                store,
                self.state_dir,
                cookies_path=self.cookies_path,
                limit=limit,
                min_delay=preset.min_delay,
                max_delay=preset.max_delay,
                cooldown_every=preset.cooldown_every,
                cooldown_seconds=preset.cooldown_seconds,
                prefetch_workers=preset.prefetch_workers,
                retry_unavailable=retry_unavailable,
                stop_event=self._stop_event,
                on_video_result=self._on_video_result,
                on_status_change=self._on_migration_phase,
                out=io.StringIO(),
            )
            with self._lock:
                self._migration.aborted_on_auth = summary.aborted_on_auth_circuit_breaker
                self._migration.interrupted = summary.interrupted
                if summary.aborted_on_auth_circuit_breaker:
                    self._migration.last_error = (
                        "YouTube rejected authentication 3 times in a row. "
                        "Return to Step 2 and sign in again."
                    )
        except Exception as exc:
            with self._lock:
                self._migration.last_error = str(exc)
        finally:
            with self._lock:
                self._migration.running = False
                self._migration.stopping = False
                self._migration.finished = True
                self._migration.phase_detail = None
                if self._migration.aborted_on_auth or self._migration.last_error:
                    self._migration.phase = "error"
                elif self._migration.interrupted:
                    self._migration.phase = "paused"
                else:
                    self._migration.phase = "completed"

    def stop_migration(self) -> dict[str, object]:
        """Request cooperative pause of the running migration thread."""
        with self._lock:
            if self._migration.running:
                self._stop_event.set()
                self._migration.stopping = True
            return self.get_migration_status()

    def reset_progress(self) -> dict[str, object]:
        """Clear saved checkpoint progress when no import is running."""
        with self._lock:
            if self._migration.running:
                raise HttpApiError(
                    HTTPStatus.CONFLICT,
                    "Pause the active import before resetting saved progress.",
                )
            self._open_store().clear()
            self._migration = MigrationRuntimeState()
            return {
                "checkpoint": self._checkpoint_summary(),
                "takeout": self.get_takeout_summary(),
                "migration": self.get_migration_status(),
            }

    def request_server_shutdown(self) -> dict[str, object]:
        """Gracefully pause any active import and stop the HTTP server shortly after responding."""
        with self._lock:
            callback = self._shutdown_callback

        def _shutdown() -> None:
            self.shutdown_gracefully(timeout=2.0)
            if callback is not None:
                callback()

        timer = threading.Timer(0.15, _shutdown)
        timer.daemon = True
        timer.start()
        return {"stopping": True}

    def shutdown_gracefully(self, timeout: float = 2.0) -> None:
        """Pause any active import and wait briefly for its checkpoint to flush."""
        self.stop_migration()
        with self._lock:
            thread = self._migration_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    def get_migration_status(self) -> dict[str, object]:
        """Return current live migration counters, ETA, and recent video events."""
        with self._lock:
            m = self._migration
            remaining = max(0, m.total_target - m.current_index)
            preset = SPEED_PRESETS.get(m.speed, SPEED_PRESETS["fast"])
            eta_seconds = round(remaining * preset.estimated_seconds_per_video)
            if m.running and m.current_index > 0 and m.started_at is not None:
                elapsed = max(0.1, time.monotonic() - m.started_at)
                avg_per_video = elapsed / m.current_index
                eta_seconds = round(remaining * avg_per_video)

            return {
                "running": m.running,
                "stopping": m.stopping,
                "finished": m.finished,
                "phase": m.phase,
                "phaseDetail": m.phase_detail,
                "scope": m.scope,
                "speed": m.speed,
                "afterDate": m.after_date,
                "beforeDate": m.before_date,
                "includeMusic": m.include_music,
                "excludeShorts": m.exclude_shorts,
                "retryUnavailable": m.retry_unavailable,
                "currentIndex": m.current_index,
                "totalTarget": m.total_target,
                "alreadySkipped": m.already_skipped,
                "watched": m.watched,
                "unavailable": m.unavailable,
                "failed": m.failed,
                "authFailed": m.auth_failed,
                "remaining": remaining,
                "etaSeconds": eta_seconds,
                "abortedOnAuth": m.aborted_on_auth,
                "interrupted": m.interrupted,
                "lastError": m.last_error,
                "recentEvents": list(m.recent_events),
                "checkpoint": self._checkpoint_summary(),
            }

    def get_bootstrap_payload(self) -> dict[str, object]:
        """Return initial state payload for the single-page wizard."""
        year = _current_year()
        return {
            "csrfToken": self.csrf_token,
            "currentYear": year,
            "threeYearsAgoYear": year - 2,
            "checkpoint": self._checkpoint_summary(),
            "takeout": self.get_takeout_summary(),
            "auth": self.get_auth_status(),
            "migration": self.get_migration_status(),
        }


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict[str, object]:
    raw_len = handler.headers.get("Content-Length") or "0"
    try:
        length = int(raw_len)
    except ValueError as exc:
        raise HttpApiError(HTTPStatus.BAD_REQUEST, "Invalid Content-Length header.") from exc

    if length == 0:
        return {}
    if length > _MAX_JSON_BODY_BYTES:
        raise HttpApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "JSON payload too large.")

    raw_bytes = handler.rfile.read(length)
    try:
        decoded: object = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HttpApiError(HTTPStatus.BAD_REQUEST, f"Invalid JSON body: {exc}") from exc

    if not isinstance(decoded, dict):
        raise HttpApiError(HTTPStatus.BAD_REQUEST, "Expected JSON object body.")
    return decoded


def _as_bool(value: object, *, default: bool = False) -> bool:
    return value if isinstance(value, bool) else default


def _as_optional_str(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def make_request_handler(controller: WebAppController) -> type[BaseHTTPRequestHandler]:
    """Create a ThreadingHTTPServer handler bound to the given WebAppController."""

    class WizardRequestHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            # Suppress noisy 1-second polling access logs on stdout/stderr.
            return

        def _send_bytes(self, status: HTTPStatus, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send_bytes(status, "application/json; charset=utf-8", encoded)

        def _verify_csrf(self) -> None:
            token = self.headers.get("X-CSRF-Token")
            if not token or not secrets.compare_digest(token, controller.csrf_token):
                raise HttpApiError(
                    HTTPStatus.FORBIDDEN,
                    "Missing or expired session token. Reload the page and try again.",
                )

        def _handle_static(self, path: str) -> None:
            asset = _STATIC_ASSETS.get(path)
            if asset is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Not found: {path}"})
                return
            filename, content_type = asset
            asset_path = _STATIC_DIR / filename
            if not asset_path.is_file():
                self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Static asset missing: {filename}"})
                return
            self._send_bytes(HTTPStatus.OK, content_type, asset_path.read_bytes())

        def _handle_get_bootstrap(self) -> None:
            self._send_json(HTTPStatus.OK, controller.get_bootstrap_payload())

        def _handle_get_auth_status(self) -> None:
            self._send_json(HTTPStatus.OK, controller.get_auth_status())

        def _handle_get_migration_status(self) -> None:
            self._send_json(HTTPStatus.OK, controller.get_migration_status())

        def _handle_post_upload(self) -> None:
            raw_len = self.headers.get("Content-Length")
            if raw_len is None:
                raise HttpApiError(HTTPStatus.LENGTH_REQUIRED, "Content-Length header required.")
            try:
                content_length = int(raw_len)
            except ValueError as exc:
                raise HttpApiError(HTTPStatus.BAD_REQUEST, "Invalid Content-Length header.") from exc

            filename = self.headers.get("X-Filename") or "takeout.zip"
            summary = controller.upload_takeout_stream(
                raw_filename=filename,
                content_length=content_length,
                stream=self.rfile,
            )
            self._send_json(HTTPStatus.OK, {"takeout": summary})

        def _handle_post_summary(self) -> None:
            body = _read_json_body(self)
            speed = _as_optional_str(body.get("speed")) or "fast"
            summary = controller.get_takeout_summary(
                include_music=_as_bool(body.get("includeMusic")),
                exclude_shorts=_as_bool(body.get("excludeShorts")),
                retry_unavailable=_as_bool(body.get("retryUnavailable")),
                speed=speed,
            )
            self._send_json(HTTPStatus.OK, {"takeout": summary})

        def _handle_post_preview(self) -> None:
            body = _read_json_body(self)
            scope = _as_optional_str(body.get("scope")) or "test5"
            speed = _as_optional_str(body.get("speed")) or "fast"
            result = controller.preview_scope(
                scope=scope,
                speed=speed,
                include_music=_as_bool(body.get("includeMusic")),
                exclude_shorts=_as_bool(body.get("excludeShorts")),
                after_date=_as_optional_str(body.get("afterDate")),
                before_date=_as_optional_str(body.get("beforeDate")),
                retry_unavailable=_as_bool(body.get("retryUnavailable")),
            )
            self._send_json(HTTPStatus.OK, result)

        def _handle_post_auth_start(self) -> None:
            body = _read_json_body(self)
            auth = controller.start_auth_window(
                browser=_as_optional_str(body.get("browser")),
                fresh=_as_bool(body.get("fresh")),
                reset_progress=_as_bool(body.get("resetProgress")),
            )
            self._send_json(HTTPStatus.OK, auth)

        def _handle_post_auth_complete(self) -> None:
            auth = controller.complete_auth_window()
            self._send_json(HTTPStatus.OK, auth)

        def _handle_post_migration_start(self) -> None:
            body = _read_json_body(self)
            scope = _as_optional_str(body.get("scope")) or "test5"
            speed = _as_optional_str(body.get("speed")) or "fast"
            status_payload = controller.start_migration(
                scope=scope,
                speed=speed,
                include_music=_as_bool(body.get("includeMusic")),
                exclude_shorts=_as_bool(body.get("excludeShorts")),
                after_date=_as_optional_str(body.get("afterDate")),
                before_date=_as_optional_str(body.get("beforeDate")),
                retry_unavailable=_as_bool(body.get("retryUnavailable")),
            )
            self._send_json(HTTPStatus.OK, status_payload)

        def _handle_post_migration_stop(self) -> None:
            self._send_json(HTTPStatus.OK, controller.stop_migration())

        def _handle_post_progress_reset(self) -> None:
            self._send_json(HTTPStatus.OK, controller.reset_progress())

        def _handle_post_server_shutdown(self) -> None:
            self._send_json(HTTPStatus.OK, controller.request_server_shutdown())

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            routes: dict[str, Callable[[], None]] = {
                "/api/bootstrap": self._handle_get_bootstrap,
                "/api/auth/status": self._handle_get_auth_status,
                "/api/migration/status": self._handle_get_migration_status,
            }
            handler = routes.get(path)
            if handler is not None:
                handler()
                return
            self._handle_static(path)

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            routes: dict[str, Callable[[], None]] = {
                "/api/takeout/upload": self._handle_post_upload,
                "/api/takeout/summary": self._handle_post_summary,
                "/api/takeout/preview": self._handle_post_preview,
                "/api/auth/start": self._handle_post_auth_start,
                "/api/auth/complete": self._handle_post_auth_complete,
                "/api/migration/start": self._handle_post_migration_start,
                "/api/migration/stop": self._handle_post_migration_stop,
                "/api/progress/reset": self._handle_post_progress_reset,
                "/api/server/shutdown": self._handle_post_server_shutdown,
            }
            handler = routes.get(path)
            if handler is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Unknown API endpoint: {path}"})
                return
            try:
                self._verify_csrf()
                handler()
            except HttpApiError as exc:
                self._send_json(exc.status, {"error": exc.message})

    return WizardRequestHandler


class _QuickExitHTTPServer(ThreadingHTTPServer):
    """HTTP server that does not block server_close() on idle browser Keep-Alive sockets."""

    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True


def create_ui_server(
    project_dir: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> tuple[ThreadingHTTPServer, WebAppController]:
    """Build a ThreadingHTTPServer bound to host:port serving the migration wizard."""
    controller = WebAppController(project_dir)
    handler_cls = make_request_handler(controller)
    server = _QuickExitHTTPServer((host, port), handler_cls)
    controller.set_shutdown_callback(server.shutdown)
    return (server, controller)
