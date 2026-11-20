"""Command-line interface for yt-history-migrator."""

import argparse
from collections import Counter
from collections.abc import Callable, Sequence
import errno
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.request
import webbrowser

from yt_history_migrator.auth import SUPPORTED_BROWSERS, interactive_browser_login
from yt_history_migrator.fast_marker import SPEED_PRESETS
from yt_history_migrator.filter_dedup import prepare_watch_entries
from yt_history_migrator.importer import MigrationSummary, detect_node_path, run_migration
from yt_history_migrator.models import (
    FilterOptions,
    FilterReport,
    MigrationStatus,
    SkipReason,
    WatchEntry,
)
from yt_history_migrator.parser import parse_takeout_file
from yt_history_migrator.progress_store import ProgressStore
from yt_history_migrator.web_server import WebAppController, create_ui_server

_ESTIMATED_SECONDS_PER_VIDEO = 0.40


def _add_filter_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--include-music",
        action="store_true",
        help="Include YouTube Music entries (excluded by default)",
    )
    parser.add_argument(
        "--include-removed",
        action="store_true",
        help="Include pre-deleted/private videos that lack title and channel metadata in Takeout",
    )
    parser.add_argument(
        "--exclude-shorts",
        action="store_true",
        help="Exclude /shorts/ URLs",
    )
    parser.add_argument(
        "--after",
        metavar="YYYY-MM-DD",
        default=None,
        help="Only include videos watched on or after this UTC date",
    )
    parser.add_argument(
        "--before",
        metavar="YYYY-MM-DD",
        default=None,
        help="Only include videos watched on or before this UTC date",
    )
    parser.add_argument(
        "--channel",
        action="append",
        default=[],
        metavar="NAME",
        help="Include only this exact channel name (repeatable, case-insensitive)",
    )


def _build_filter_options(args: argparse.Namespace) -> FilterOptions:
    raw_channels: list[str] = list(args.channel)
    normalized_channels = frozenset(ch.strip().lower() for ch in raw_channels if ch.strip())
    return FilterOptions(
        include_music=bool(args.include_music),
        include_removed=bool(args.include_removed),
        exclude_shorts=bool(args.exclude_shorts),
        after_date=args.after,
        before_date=args.before,
        channels=normalized_channels,
    )


def _load_filtered_report(takeout_path: Path, options: FilterOptions) -> FilterReport:
    raw_items = parse_takeout_file(takeout_path)
    return prepare_watch_entries(raw_items, options)


def _format_duration(seconds: float) -> str:
    total_seconds = int(round(seconds))
    hours, rem = divmod(total_seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    if minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _format_entry_date(entry: WatchEntry) -> str:
    if entry.watched_at_raw:
        return entry.watched_at_raw
    return "unknown"


def _handle_inspect(args: argparse.Namespace) -> int:
    takeout_path = Path(args.takeout)
    options = _build_filter_options(args)
    report = _load_filtered_report(takeout_path, options)

    print(f"Takeout file              : {takeout_path}")
    print(f"Total raw activity items  : {report.total_raw}")

    if report.skipped_counts:
        print("Skipped items by reason   :")
        for reason in SkipReason:
            count = report.skipped_counts.get(reason, 0)
            if count > 0:
                print(f"  - {reason.value:<20}: {count}")

    print(f"Duplicate watches merged  : {report.duplicate_watches_collapsed}")
    print(f"Unique videos to migrate  : {len(report.entries)}")

    if not report.entries:
        return 0

    oldest = _format_entry_date(report.entries[0])
    newest = _format_entry_date(report.entries[-1])
    est_runtime = _format_duration(len(report.entries) * _ESTIMATED_SECONDS_PER_VIDEO)
    print(f"Date span (oldest->newest): {oldest} -> {newest}")
    print(f"Estimated runtime (~3.5s) : {est_runtime}")

    channel_counts = Counter(
        entry.channel_name for entry in report.entries if entry.channel_name
    )
    if not channel_counts:
        return 0

    print("Top 10 channels (unique videos):")
    for rank, (channel, count) in enumerate(channel_counts.most_common(10), start=1):
        print(f"  {rank:>2}. {channel} ({count})")
    return 0


def _handle_export_urls(args: argparse.Namespace) -> int:
    takeout_path = Path(args.takeout)
    out_urls = Path(args.out_urls)
    out_json = Path(args.out_json)
    options = _build_filter_options(args)
    report = _load_filtered_report(takeout_path, options)

    out_urls.parent.mkdir(parents=True, exist_ok=True)
    url_lines = [entry.canonical_url for entry in report.entries]
    out_urls.write_text("\n".join(url_lines) + ("\n" if url_lines else ""), encoding="utf-8")

    out_json.parent.mkdir(parents=True, exist_ok=True)
    serialized = [
        {
            "video_id": entry.video_id,
            "url": entry.canonical_url,
            "title": entry.title,
            "channel_name": entry.channel_name,
            "watched_at": entry.watched_at_raw,
            "watch_count": entry.watch_count,
        }
        for entry in report.entries
    ]
    out_json.write_text(json.dumps(serialized, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"Wrote {len(report.entries)} URLs (oldest -> newest) to {out_urls}")
    print(f"Wrote cleaned metadata JSON to {out_json}")
    return 0


def _print_migration_summary(summary: MigrationSummary, progress_path: Path) -> None:
    print("\n--- Migration Summary ---")
    print(f"Total eligible videos : {summary.total_candidates}")
    print(f"Skipped (checkpoint)  : {summary.already_skipped}")
    print(f"Attempted this run    : {summary.attempted}")
    print(f"  Watched             : {summary.watched}")
    print(f"  Unavailable         : {summary.unavailable}")
    print(f"  Failed (transient)  : {summary.failed}")
    print(f"  Auth failed         : {summary.auth_failed}")
    print(f"Checkpoint file       : {progress_path}")

    remaining = summary.total_candidates - summary.already_skipped - summary.watched - summary.unavailable
    if remaining > 0 and not summary.dry_run:
        print(f"Remaining to migrate  : {remaining} (re-run the same command to resume)")


def _handle_login(args: argparse.Namespace) -> int:
    interactive_browser_login(
        cookies_out=Path(args.out),
        state_dir=Path(args.state_dir),
        preferred_browser=args.browser,
        fresh_profile=bool(args.fresh),
    )
    return 0


def _resolve_migration_cookies(args: argparse.Namespace, state_dir: Path) -> Path | None:
    if args.dry_run or args.cookies_from_browser:
        return None
    if args.cookies:
        return Path(args.cookies)

    default_cookies = Path("data/cookies.txt")
    if default_cookies.exists() and default_cookies.stat().st_size > 0:
        print(f"Using existing cookies at {default_cookies}")
        return default_cookies

    if not sys.stdin.isatty():
        raise ValueError(
            "No data/cookies.txt found. Run `./.venv/bin/yt-migrate login` in an interactive terminal "
            "or pass --cookies."
        )

    return interactive_browser_login(
        cookies_out=default_cookies,
        state_dir=state_dir,
        preferred_browser=args.browser,
    )


def _handle_migrate(args: argparse.Namespace) -> int:
    takeout_path = Path(args.takeout)
    state_dir = Path(args.state_dir)
    cookies_path = _resolve_migration_cookies(args, state_dir)
    options = _build_filter_options(args)

    node_path = detect_node_path()
    if node_path is None and not args.dry_run:
        print(
            "Warning: Node.js was not found on PATH; yt-dlp will fall back to its default JS runtime.",
            file=sys.stderr,
        )

    report = _load_filtered_report(takeout_path, options)
    store = ProgressStore(state_dir / "progress.jsonl")
    store.load()

    preset = SPEED_PRESETS[args.speed]
    min_delay = preset.min_delay if args.min_delay is None else args.min_delay
    max_delay = preset.max_delay if args.max_delay is None else args.max_delay
    cooldown_every = preset.cooldown_every if args.cooldown_every is None else args.cooldown_every
    cooldown_seconds = preset.cooldown_seconds if args.cooldown_seconds is None else args.cooldown_seconds

    summary = run_migration(
        report.entries,
        store,
        state_dir,
        cookies_path=cookies_path,
        cookies_from_browser=args.cookies_from_browser,
        limit=args.limit,
        dry_run=bool(args.dry_run),
        min_delay=min_delay,
        max_delay=max_delay,
        cooldown_every=cooldown_every,
        cooldown_seconds=cooldown_seconds,
        prefetch_workers=preset.prefetch_workers,
        retry_unavailable=bool(args.retry_unavailable),
    )
    _print_migration_summary(summary, store.path)

    if summary.aborted_on_auth_circuit_breaker:
        return 1
    if summary.interrupted:
        return 130
    return 0


def _handle_status(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir)
    store = ProgressStore(state_dir / "progress.jsonl")
    store.load()
    counts = store.summary_counts()

    print(f"Checkpoint file : {store.path}")
    print(f"Tracked videos  : {store.total_recorded()}")
    for status in MigrationStatus:
        print(f"  - {status.value:<12}: {counts[status]}")

    if not args.takeout:
        return 0

    report = _load_filtered_report(Path(args.takeout), _build_filter_options(args))
    pending_count = sum(1 for entry in report.entries if not store.should_skip(entry.video_id))
    print(f"Eligible total  : {len(report.entries)}")
    print(f"Remaining       : {pending_count}")
    return 0


def _is_existing_ui_running(host: str, port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/bootstrap", timeout=1.5) as resp:
            if resp.status != 200:
                return False
            decoded: object = json.loads(resp.read().decode("utf-8"))
            return isinstance(decoded, dict) and "csrfToken" in decoded
    except Exception:
        return False


def _bind_ui_server(
    project_dir: Path,
    host: str,
    port: int,
) -> tuple[ThreadingHTTPServer | None, WebAppController | None, str]:
    try:
        server, controller = create_ui_server(project_dir, host=host, port=port)
        bound_port = server.server_address[1]
        return (server, controller, f"http://{host}:{bound_port}")
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
    if _is_existing_ui_running(host, port):
        return (None, None, f"http://{host}:{port}")
    server, controller = create_ui_server(project_dir, host=host, port=0)
    bound_port = server.server_address[1]
    return (server, controller, f"http://{host}:{bound_port}")


def _handle_ui(args: argparse.Namespace) -> int:
    project_dir = Path(args.project_dir).resolve()
    server, controller, url = _bind_ui_server(project_dir, args.host, args.port)
    if server is None:
        print(
            f"YouTube History Migrator UI is already running at {url} "
            "(run './.venv/bin/yt-migrate stop' to shut it down)",
            flush=True,
        )
        if not args.no_open:
            webbrowser.open(url)
        return 0

    print(f"YouTube History Migrator UI running at {url} (Press Ctrl+C to stop)", flush=True)
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping YouTube History Migrator UI...")
        if controller is not None:
            controller.shutdown_gracefully(timeout=2.0)
    finally:
        server.server_close()
    return 0


def _find_listening_pids(port: int) -> list[int]:
    result = subprocess.run(
        ["lsof", f"-tiTCP:{port}", "-sTCP:LISTEN"],
        capture_output=True,
        text=True,
        check=False,
    )
    pids: list[int] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.isdigit():
            pids.append(int(stripped))
    return pids


def _handle_stop(args: argparse.Namespace) -> int:
    pids = _find_listening_pids(args.port)
    if not pids:
        print(f"No UI server is running on port {args.port}.")
        return 0

    if not args.force and not _is_existing_ui_running(args.host, args.port):
        joined = ", ".join(str(pid) for pid in pids)
        raise ValueError(
            f"Port {args.port} is used by another process (PIDs: {joined}). "
            "Pass --force to terminate anyway."
        )

    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and _find_listening_pids(args.port):
        time.sleep(0.1)

    joined = ", ".join(str(pid) for pid in pids)
    print(
        f"Stopped YouTube History Migrator UI on port {args.port} (PID {joined}). "
        "Saved progress remains in state/progress.jsonl."
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Create the top-level CLI parser with inspect, export-urls, migrate, and status."""
    parser = argparse.ArgumentParser(
        prog="yt-migrate",
        description="Filter, order (oldest -> newest), and migrate Google Takeout YouTube watch history.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser(
        "inspect",
        help="Inspect a Google Takeout watch-history file and show filter stats",
    )
    inspect_parser.add_argument("--takeout", required=True, help="Path to watch-history.json, .html, or Takeout .zip")
    _add_filter_arguments(inspect_parser)

    export_parser = subparsers.add_parser(
        "export-urls",
        help="Export cleaned oldest -> newest URL list and JSON metadata",
    )
    export_parser.add_argument("--takeout", required=True, help="Path to watch-history.json, .html, or Takeout .zip")
    export_parser.add_argument(
        "--out-urls",
        default="output/watch-urls-oldest-first.txt",
        help="Destination text file for canonical URLs (default: output/watch-urls-oldest-first.txt)",
    )
    export_parser.add_argument(
        "--out-json",
        default="output/cleaned-history.json",
        help="Destination JSON file for cleaned history (default: output/cleaned-history.json)",
    )
    _add_filter_arguments(export_parser)

    login_parser = subparsers.add_parser(
        "login",
        help="Open an isolated browser window to sign into your NEW YouTube account and save cookies",
    )
    login_parser.add_argument(
        "--out",
        default="data/cookies.txt",
        help="Destination path for saved Netscape cookies (default: data/cookies.txt)",
    )
    login_parser.add_argument(
        "--browser",
        choices=SUPPORTED_BROWSERS,
        default=None,
        help="Browser to launch for sign-in (default: auto-detect Firefox, Chrome, or Brave)",
    )
    login_parser.add_argument(
        "--state-dir",
        default="state",
        help="Directory for isolated browser sign-in profile (default: state)",
    )
    login_parser.add_argument(
        "--fresh",
        action="store_true",
        help="Clear any cached login profile before opening the browser",
    )

    migrate_parser = subparsers.add_parser(
        "migrate",
        help="Replay filtered history onto your destination YouTube account with checkpointing",
    )
    migrate_parser.add_argument("--takeout", required=True, help="Path to watch-history.json, .html, or Takeout .zip")
    auth_group = migrate_parser.add_mutually_exclusive_group(required=False)
    auth_group.add_argument("--cookies", help="Path to Netscape cookies.txt for the destination account")
    auth_group.add_argument(
        "--cookies-from-browser",
        help="Browser[:profile] to extract cookies from (e.g. chrome or firefox:default)",
    )
    auth_group.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview pending oldest -> newest videos without contacting YouTube",
    )
    migrate_parser.add_argument(
        "--browser",
        choices=SUPPORTED_BROWSERS,
        default=None,
        help="Browser to open if automatic sign-in is triggered (default: auto-detect Firefox/Chrome/Brave)",
    )
    migrate_parser.add_argument("--limit", type=int, default=None, help="Process at most N pending videos")
    migrate_parser.add_argument(
        "--speed",
        choices=tuple(SPEED_PRESETS.keys()),
        default="fast",
        help="Throughput preset: fast (~0.4s/video), turbo (~0.2s/video), or safe (~1.3s/video)",
    )
    migrate_parser.add_argument(
        "--min-delay",
        type=float,
        default=None,
        help="Override minimum jittered delay in seconds between videos",
    )
    migrate_parser.add_argument(
        "--max-delay",
        type=float,
        default=None,
        help="Override maximum jittered delay in seconds between videos",
    )
    migrate_parser.add_argument(
        "--cooldown-every",
        type=int,
        default=None,
        help="Override cooldown interval in videos (0 disables)",
    )
    migrate_parser.add_argument(
        "--cooldown-seconds",
        type=float,
        default=None,
        help="Override cooldown pause duration in seconds",
    )
    migrate_parser.add_argument(
        "--state-dir",
        default="state",
        help="Directory for progress.jsonl and session-cookies.txt (default: state)",
    )
    migrate_parser.add_argument(
        "--retry-unavailable",
        action="store_true",
        help="Retry videos previously marked unavailable/private",
    )
    _add_filter_arguments(migrate_parser)

    status_parser = subparsers.add_parser(
        "status",
        help="Show progress counts from state/progress.jsonl",
    )
    status_parser.add_argument("--takeout", default=None, help="Optional Takeout path to calculate remaining count")
    status_parser.add_argument("--state-dir", default="state", help="State directory (default: state)")
    _add_filter_arguments(status_parser)

    ui_parser = subparsers.add_parser(
        "ui",
        help="Launch the 3-step local Web UI wizard in your browser",
    )
    ui_parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    ui_parser.add_argument("--port", type=int, default=8765, help="Port number (default: 8765)")
    ui_parser.add_argument("--project-dir", default=".", help="Workspace directory containing data/ and state/")
    ui_parser.add_argument("--no-open", action="store_true", help="Do not open a browser tab automatically")

    stop_parser = subparsers.add_parser(
        "stop",
        help="Stop a running background Web UI server on the given port",
    )
    stop_parser.add_argument("--host", default="127.0.0.1", help="UI host (default: 127.0.0.1)")
    stop_parser.add_argument("--port", type=int, default=8765, help="UI port (default: 8765)")
    stop_parser.add_argument(
        "--force",
        action="store_true",
        help="Terminate whatever process listens on that port even if it is not yt-migrate ui",
    )

    return parser


_COMMAND_HANDLERS: dict[str, Callable[[argparse.Namespace], int]] = {
    "inspect": _handle_inspect,
    "export-urls": _handle_export_urls,
    "login": _handle_login,
    "migrate": _handle_migrate,
    "status": _handle_status,
    "ui": _handle_ui,
    "stop": _handle_stop,
}


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point returning process exit code."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    handler = _COMMAND_HANDLERS.get(args.command)
    if handler is None:
        parser.error(f"Unknown command: {args.command}")
    try:
        return handler(args)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
