"""Parsers for Google Takeout YouTube history exports (.json, .html, and .zip)."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import zipfile

from yt_history_migrator.models import RawHistoryItem

_GOOGLE_ADS_MARKER = "from google ads"
_HTML_DATETIME_FORMATS = (
    "%b %d, %Y, %I:%M:%S %p",
    "%b %d, %Y, %H:%M:%S",
    "%B %d, %Y, %I:%M:%S %p",
    "%B %d, %Y, %H:%M:%S",
    "%d %b %Y, %H:%M:%S",
    "%d %b %Y, %I:%M:%S %p",
    "%Y-%m-%d %H:%M:%S",
)
_EXPLICIT_OFFSET_RE = re.compile(r"\s+(?:UTC|GMT)([+-])(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)
_TZ_ABBR_SUFFIX_RE = re.compile(r"\s+[A-Z]{2,5}$")


def parse_takeout_file(path: Path) -> list[RawHistoryItem]:
    """Parse a Google Takeout watch-history file (.json, .html, or .zip) into raw items."""
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Takeout file not found: {path}")
    if path.stat().st_size == 0:
        raise ValueError(f"Takeout file is empty: {path}")

    suffix = path.suffix.lower()
    if suffix == ".json":
        return parse_takeout_json(path)
    if suffix in (".html", ".htm"):
        return parse_takeout_html(path)
    if suffix == ".zip":
        return parse_takeout_zip(path)
    raise ValueError(f"Unsupported Takeout format '{path.suffix}'. Expected .json, .html, or .zip")


def parse_takeout_json(path: Path) -> list[RawHistoryItem]:
    """Parse Google Takeout watch-history.json into RawHistoryItem records."""
    return parse_takeout_json_text(path.read_text(encoding="utf-8"), source_label=str(path))


def parse_takeout_json_text(raw_text: str, *, source_label: str = "<json>") -> list[RawHistoryItem]:
    """Parse raw JSON text from watch-history.json into RawHistoryItem records."""
    decoded: object = json.loads(raw_text)
    if not isinstance(decoded, list):
        raise ValueError(
            f"Expected JSON array at root of {source_label}, got {type(decoded).__name__}"
        )

    items: list[RawHistoryItem] = []
    for index, element in enumerate(decoded):
        item = _json_element_to_item(index, element)
        if item is not None:
            items.append(item)
    return items


def parse_takeout_html(path: Path) -> list[RawHistoryItem]:
    """Parse Google Takeout watch-history.html into RawHistoryItem records."""
    return parse_takeout_html_text(path.read_text(encoding="utf-8"))


def parse_takeout_html_text(raw_html: str) -> list[RawHistoryItem]:
    """Parse raw HTML text from watch-history.html into RawHistoryItem records."""
    html_parser = _TakeoutHtmlParser()
    html_parser.feed(raw_html)
    html_parser.close()
    return html_parser.items


def parse_takeout_zip(path: Path) -> list[RawHistoryItem]:
    """Parse watch-history.json or watch-history.html directly inside a Takeout .zip archive."""
    with zipfile.ZipFile(path, "r") as archive:
        found = _find_watch_history_member(archive.namelist())
        if found is None:
            raise ValueError(
                f"No watch-history.json or watch-history.html found inside {path}"
            )
        member_name, kind = found
        raw_text = archive.read(member_name).decode("utf-8")

    if kind == "json":
        return parse_takeout_json_text(raw_text, source_label=f"{path}!{member_name}")
    return parse_takeout_html_text(raw_text)


def _find_watch_history_member(names: list[str]) -> tuple[str, str] | None:
    json_match = _find_member_by_filename(names, "watch-history.json")
    if json_match is not None:
        return (json_match, "json")
    html_match = _find_member_by_filename(names, "watch-history.html")
    if html_match is not None:
        return (html_match, "html")
    return None


def _find_member_by_filename(names: list[str], filename: str) -> str | None:
    preferred_suffix = f"/history/{filename}"
    for name in names:
        lowered = name.lower()
        if lowered.endswith(preferred_suffix):
            return name
    for name in names:
        lowered = name.lower()
        if lowered == filename or lowered.endswith(f"/{filename}"):
            return name
    return None


def _json_element_to_item(record_index: int, element: object) -> RawHistoryItem | None:
    if not isinstance(element, dict):
        return None

    header = _as_str(element.get("header")) or "YouTube"
    title = _as_str(element.get("title")) or ""
    title_url = _normalize_url(_as_str(element.get("titleUrl")))
    watched_at_raw = _as_str(element.get("time"))
    watched_at_epoch = _parse_iso_timestamp(watched_at_raw)

    subtitles = element.get("subtitles")
    details = element.get("details")
    is_ad = _contains_google_ads(details) or _contains_google_ads(subtitles)
    channel_name = _extract_channel_from_subtitles(subtitles)

    return RawHistoryItem(
        record_index=record_index,
        header=header,
        title=title,
        title_url=title_url,
        channel_name=channel_name,
        watched_at_raw=watched_at_raw,
        watched_at_epoch=watched_at_epoch,
        is_ad=is_ad,
    )


def _as_str(value: object) -> str | None:
    if isinstance(value, str):
        return value
    return None


def _normalize_url(url: str | None) -> str | None:
    if url is None:
        return None
    cleaned = url.strip()
    if not cleaned:
        return None
    # Guard against double-escaped \u003d produced by some Takeout converters.
    return cleaned.replace("\\u003d", "=")


def _contains_google_ads(entries: object) -> bool:
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = _as_str(entry.get("name"))
        if name is not None and _GOOGLE_ADS_MARKER in name.lower():
            return True
    return False


def _extract_channel_from_subtitles(subtitles: object) -> str | None:
    if not isinstance(subtitles, list):
        return None
    for entry in subtitles:
        if not isinstance(entry, dict):
            continue
        name = _as_str(entry.get("name"))
        if name is None:
            continue
        cleaned = name.strip()
        if not cleaned or _GOOGLE_ADS_MARKER in cleaned.lower():
            continue
        return cleaned
    return None


def _parse_iso_timestamp(value: str | None) -> float | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _extract_html_tz(cleaned: str) -> tuple[str, tzinfo]:
    offset_match = _EXPLICIT_OFFSET_RE.search(cleaned)
    if offset_match is None:
        without_tz = _TZ_ABBR_SUFFIX_RE.sub("", cleaned).strip()
        return (without_tz, timezone.utc)

    sign = 1 if offset_match.group(1) == "+" else -1
    hours = int(offset_match.group(2))
    minutes = int(offset_match.group(3) or "0")
    tz = timezone(timedelta(hours=sign * hours, minutes=sign * minutes))
    without_tz = cleaned[: offset_match.start()].strip()
    return (without_tz, tz)


def _parse_html_timestamp(value: str | None) -> float | None:
    if value is None:
        return None
    cleaned = value.replace("\xa0", " ").replace("\u202f", " ").strip()
    if not cleaned:
        return None

    iso_epoch = _parse_iso_timestamp(cleaned)
    if iso_epoch is not None:
        return iso_epoch

    without_tz, tz = _extract_html_tz(cleaned)
    for fmt in _HTML_DATETIME_FORMATS:
        try:
            parsed = datetime.strptime(without_tz, fmt)
            return parsed.replace(tzinfo=tz).timestamp()
        except ValueError:
            continue
    return None


@dataclass
class _HtmlCardBuffer:
    """Mutable accumulator for one outer-cell card during HTML parsing."""

    header_chunks: list[str] = field(default_factory=list)
    body_lines: list[str] = field(default_factory=list)
    current_line_chunks: list[str] = field(default_factory=list)
    caption_chunks: list[str] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)

    def flush_body_line(self) -> None:
        text = "".join(self.current_line_chunks).replace("\xa0", " ").replace("\u202f", " ").strip()
        self.current_line_chunks.clear()
        if text:
            self.body_lines.append(text)


class _TakeoutHtmlParser(HTMLParser):
    """Streaming HTML parser for Google Takeout watch-history.html cards."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[RawHistoryItem] = []
        self._card: _HtmlCardBuffer | None = None
        self._card_div_depth = 0
        self._in_header_p = False
        self._body_div_depth = 0
        self._caption_div_depth = 0
        self._active_link_href: str | None = None
        self._active_link_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        classes = set(attr_map.get("class", "").split())

        if tag == "div":
            self._handle_div_start(classes)
            return
        if self._card is None:
            return
        if tag == "p" and "mdl-typography--title" in classes:
            self._in_header_p = True
            return
        if tag == "br" and self._body_div_depth > 0:
            self._card.flush_body_line()
            return
        if tag == "a" and self._body_div_depth > 0:
            self._active_link_href = attr_map.get("href", "").strip() or None
            self._active_link_chunks.clear()

    def handle_endtag(self, tag: str) -> None:
        if self._card is None:
            return
        if tag == "p" and self._in_header_p:
            self._in_header_p = False
            return
        if tag == "a" and self._active_link_href is not None:
            link_text = "".join(self._active_link_chunks).replace("\xa0", " ").strip()
            self._card.links.append((self._active_link_href, link_text))
            self._active_link_href = None
            self._active_link_chunks.clear()
            return
        if tag == "div":
            self._handle_div_end()

    def handle_data(self, data: str) -> None:
        if self._card is None:
            return
        if self._in_header_p:
            self._card.header_chunks.append(data)
            return
        if self._caption_div_depth > 0:
            self._card.caption_chunks.append(data)
            return
        if self._body_div_depth <= 0:
            return
        self._card.current_line_chunks.append(data)
        if self._active_link_href is not None:
            self._active_link_chunks.append(data)

    def _handle_div_start(self, classes: set[str]) -> None:
        if self._card is None:
            self._maybe_open_card(classes)
            return

        self._card_div_depth += 1
        if self._body_div_depth > 0:
            self._body_div_depth += 1
            return
        if self._caption_div_depth > 0:
            self._caption_div_depth += 1
            return
        if "mdl-typography--caption" in classes:
            self._caption_div_depth = 1
            return
        if "content-cell" in classes and "mdl-typography--text-right" not in classes:
            self._body_div_depth = 1

    def _maybe_open_card(self, classes: set[str]) -> None:
        if "outer-cell" not in classes:
            return
        self._card = _HtmlCardBuffer()
        self._card_div_depth = 1

    def _handle_div_end(self) -> None:
        if self._card is None:
            return
        self._decrement_section_depth()
        self._card_div_depth -= 1
        if self._card_div_depth == 0:
            self._finalize_card()

    def _decrement_section_depth(self) -> None:
        if self._card is None:
            return
        if self._body_div_depth > 0:
            self._body_div_depth -= 1
            self._flush_body_if_closed()
            return
        if self._caption_div_depth > 0:
            self._caption_div_depth -= 1

    def _flush_body_if_closed(self) -> None:
        if self._card is None or self._body_div_depth != 0:
            return
        self._card.flush_body_line()

    def _finalize_card(self) -> None:
        if self._card is None:
            return
        card = self._card
        self._card = None
        self._in_header_p = False
        self._body_div_depth = 0
        self._caption_div_depth = 0

        header = "".join(card.header_chunks).strip() or "YouTube"
        caption_text = " ".join(card.caption_chunks).lower()
        body_text = " ".join(card.body_lines).lower()
        is_ad = (_GOOGLE_ADS_MARKER in caption_text) or (_GOOGLE_ADS_MARKER in body_text)

        title = card.body_lines[0] if card.body_lines else ""
        title_url = _normalize_url(card.links[0][0]) if card.links else None
        channel_name = card.links[1][1] if len(card.links) > 1 and card.links[1][1] else None

        watched_at_raw = _extract_html_date_line(card.body_lines, channel_name)
        watched_at_epoch = _parse_html_timestamp(watched_at_raw)

        self.items.append(
            RawHistoryItem(
                record_index=len(self.items),
                header=header,
                title=title,
                title_url=title_url,
                channel_name=channel_name,
                watched_at_raw=watched_at_raw,
                watched_at_epoch=watched_at_epoch,
                is_ad=is_ad,
            )
        )


def _extract_html_date_line(body_lines: list[str], channel_name: str | None) -> str | None:
    if len(body_lines) <= 1:
        return None
    for candidate in reversed(body_lines[1:]):
        if channel_name is not None and candidate == channel_name:
            continue
        if _GOOGLE_ADS_MARKER in candidate.lower():
            continue
        return candidate
    return None
