"""Local HTTP backend for the 3-step YouTube watch history migration wizard."""

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
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
_SCOPE_SPECS: dict[str, tuple[str | None, int | None]] = {
    "test5": (None, 5),
    "2026": ("2026-01-01", None),
    "2024": ("2024-01-01", None),
    "all": (None, None),
}
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


@dataclass
class MigrationRuntimeState:
    """Thread-safe snapshot of the background migration runner."""

    running: bool = False
    stopping: bool = False
    finished: bool = False
    scope: str = "test5"
    speed: str = "fast"
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
    recent_events: deque[dict[str, object]] = field(default_factory=lambda: deque(maxlen=50))


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

        self._load_initial_takeout()
        self._load_initial_cookies()

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

    def _compute_scope_summary(
        self,
        entries: list[WatchEntry],
        store: ProgressStore,
    ) -> dict[str, dict[str, object]]:
        pending_entries = [entry for entry in entries if not store.should_skip(entry.video_id)]
        summaries: dict[str, dict[str, object]] = {}

        for scope_key, (after_date, limit) in _SCOPE_SPECS.items():
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
                "etaSeconds": round(pending * _SECONDS_PER_VIDEO_ESTIMATE),
            }
        return summaries

    def get_takeout_summary(self) -> dict[str, object]:
        """Return summary statistics and scope counts for the loaded Takeout export."""
        with self._lock:
            if self._takeout_path is None:
                return {"loaded": False}

            report = prepare_watch_entries(self._raw_items, FilterOptions())
            store = self._open_store()
            skipped = {
                reason.value: report.skipped_counts.get(reason, 0)
                for reason in SkipReason
                if report.skipped_counts.get(reason, 0) > 0
            }
            oldest_date = report.entries[0].watched_at_raw if report.entries else None
            newest_date = report.entries[-1].watched_at_raw if report.entries else None

            return {
                "loaded": True,
                "filename": self._takeout_display_name or self._takeout_path.name,
                "totalRaw": report.total_raw,
                "uniqueVideos": len(report.entries),
                "duplicatesMerged": report.duplicate_watches_collapsed,
                "skippedCounts": skipped,
                "oldestDate": oldest_date,
                "newestDate": newest_date,
                "scopeCounts": self._compute_scope_summary(report.entries, store),
            }

    def preview_scope(
        self,
        *,
        scope: str,
        speed: str = "fast",
        include_music: bool,
        exclude_shorts: bool,
    ) -> dict[str, object]:
        """Compute filtered eligible and pending counts for chosen UI options."""
        if scope not in _SCOPE_SPECS:
            raise HttpApiError(HTTPStatus.BAD_REQUEST, f"Unknown scope: '{scope}'")
        preset = SPEED_PRESETS.get(speed)
        if preset is None:
            raise HttpApiError(HTTPStatus.BAD_REQUEST, f"Unknown speed preset: '{speed}'")

        with self._lock:
            if self._takeout_path is None:
                raise HttpApiError(HTTPStatus.BAD_REQUEST, "Load a Takeout file in Step 1 first.")
            after_date, limit = _SCOPE_SPECS[scope]
            options = FilterOptions(
                include_music=include_music,
                exclude_shorts=exclude_shorts,
                after_date=after_date,
            )
            report: FilterReport = prepare_watch_entries(self._raw_items, options)
            store = self._open_store()
            pending_count = sum(1 for entry in report.entries if not store.should_skip(entry.video_id))
            already_skipped = len(report.entries) - pending_count
            target_count = min(pending_count, limit) if limit is not None else pending_count

            return {
                "scope": scope,
                "speed": speed,
                "totalEligible": len(report.entries),
                "alreadySkipped": already_skipped,
                "pendingCount": pending_count,
                "targetCount": target_count,
                "etaSeconds": round(target_count * preset.estimated_seconds_per_video),
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

    def start_auth_window(self, *, browser: str | None, fresh: bool) -> dict[str, object]:
        """Launch an isolated browser window for destination Gmail / YouTube login."""
        with self._lock:
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
    ) -> dict[str, object]:
        """Start background watch history migration for the chosen scope."""
        if scope not in _SCOPE_SPECS:
            raise HttpApiError(HTTPStatus.BAD_REQUEST, f"Unknown scope: '{scope}'")
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

            after_date, limit = _SCOPE_SPECS[scope]
            options = FilterOptions(
                include_music=include_music,
                exclude_shorts=exclude_shorts,
                after_date=after_date,
            )
            report = prepare_watch_entries(self._raw_items, options)
            store = self._open_store()
            pending_count = sum(1 for entry in report.entries if not store.should_skip(entry.video_id))
            already_skipped = len(report.entries) - pending_count
            target_count = min(pending_count, limit) if limit is not None else pending_count

            self._stop_event = threading.Event()
            self._migration = MigrationRuntimeState(
                running=True,
                stopping=False,
                finished=False,
                scope=scope,
                speed=speed,
                total_target=target_count,
                already_skipped=already_skipped,
                started_at=time.monotonic(),
            )

            thread = threading.Thread(
                target=self._run_migration_worker,
                args=(report.entries, store, limit, preset),
                daemon=True,
            )
            self._migration_thread = thread
            thread.start()
            return self.get_migration_status()

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
                stop_event=self._stop_event,
                on_video_result=self._on_video_result,
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

    def stop_migration(self) -> dict[str, object]:
        """Request cooperative pause of the running migration thread."""
        with self._lock:
            if self._migration.running:
                self._stop_event.set()
                self._migration.stopping = True
            return self.get_migration_status()

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
                "scope": m.scope,
                "speed": m.speed,
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
            }

    def get_bootstrap_payload(self) -> dict[str, object]:
        """Return initial state payload for the single-page wizard."""
        return {
            "csrfToken": self.csrf_token,
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

        def _handle_post_preview(self) -> None:
            body = _read_json_body(self)
            scope = _as_optional_str(body.get("scope")) or "test5"
            speed = _as_optional_str(body.get("speed")) or "fast"
            result = controller.preview_scope(
                scope=scope,
                speed=speed,
                include_music=_as_bool(body.get("includeMusic")),
                exclude_shorts=_as_bool(body.get("excludeShorts")),
            )
            self._send_json(HTTPStatus.OK, result)

        def _handle_post_auth_start(self) -> None:
            body = _read_json_body(self)
            auth = controller.start_auth_window(
                browser=_as_optional_str(body.get("browser")),
                fresh=_as_bool(body.get("fresh")),
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
            )
            self._send_json(HTTPStatus.OK, status_payload)

        def _handle_post_migration_stop(self) -> None:
            self._send_json(HTTPStatus.OK, controller.stop_migration())

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
                "/api/takeout/preview": self._handle_post_preview,
                "/api/auth/start": self._handle_post_auth_start,
                "/api/auth/complete": self._handle_post_auth_complete,
                "/api/migration/start": self._handle_post_migration_start,
                "/api/migration/stop": self._handle_post_migration_stop,
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
    return (server, controller)
