"""Tests for Google Takeout JSON and HTML watch-history parsers."""

from datetime import datetime, timezone
import json
from pathlib import Path
import zipfile
import pytest

from yt_history_migrator.parser import parse_takeout_file, parse_takeout_html, parse_takeout_json


def test_parse_takeout_json_handles_ads_music_missing_urls_and_escaped_urls(tmp_path: Path) -> None:
    payload = [
        {
            "header": "YouTube",
            "title": "Watched Normal Video",
            "titleUrl": "https://www.youtube.com/watch?v\\u003dabcdefghijk",
            "subtitles": [{"name": "Creator One", "url": "https://www.youtube.com/channel/UC1"}],
            "time": "2026-06-06T12:34:56.789Z",
        },
        {
            "header": "YouTube Music",
            "title": "Watched Song Track",
            "titleUrl": "https://music.youtube.com/watch?v=12345678901",
            "subtitles": [{"name": "Artist - Topic", "url": "https://www.youtube.com/channel/UC2"}],
            "time": "2026-06-05T10:00:00Z",
        },
        {
            "header": "YouTube",
            "title": "Watched Promo Ad",
            "titleUrl": "https://www.youtube.com/watch?v=advideo1234",
            "subtitles": [{"name": "From Google Ads", "url": "https://www.google.com/url?q=x"}],
            "details": [{"name": "From Google Ads"}],
            "time": "2026-06-04T09:00:00Z",
        },
        {
            "header": "YouTube",
            "title": "Watched a video that has been removed",
            "time": "2026-06-03T08:00:00Z",
        },
    ]
    json_path = tmp_path / "watch-history.json"
    json_path.write_text(json.dumps(payload), encoding="utf-8")

    items = parse_takeout_json(json_path)

    assert len(items) == 4
    assert items[0].record_index == 0
    assert items[0].title_url == "https://www.youtube.com/watch?v=abcdefghijk"
    assert items[0].channel_name == "Creator One"
    assert items[0].is_ad is False
    assert items[0].watched_at_epoch is not None

    assert items[1].header == "YouTube Music"
    assert items[1].channel_name == "Artist - Topic"

    assert items[2].is_ad is True
    assert items[2].channel_name is None

    assert items[3].title_url is None
    assert items[3].channel_name is None


def test_parse_takeout_html_parses_cards_ads_and_music(tmp_path: Path) -> None:
    html_content = """<!DOCTYPE html>
<html><body>
<div class="mdl-grid">
  <div class="outer-cell mdl-cell mdl-cell--12-col mdl-shadow--2dp">
    <div class="mdl-grid">
      <div class="header-cell mdl-cell mdl-cell--12-col">
        <p class="mdl-typography--title">YouTube<br></p>
      </div>
      <div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">
        Watched&nbsp;<a href="https://www.youtube.com/watch?v=vid00000001">First Video</a><br>
        <a href="https://www.youtube.com/channel/UC1">Tech Channel</a><br>
        Jun 6, 2026, 12:34:56 PM UTC
      </div>
      <div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1 mdl-typography--text-right"></div>
      <div class="content-cell mdl-cell mdl-cell--12-col mdl-typography--caption">
        <b>Products:</b><br>&emsp;YouTube<br>
      </div>
    </div>
  </div>
  <div class="outer-cell mdl-cell mdl-cell--12-col mdl-shadow--2dp">
    <div class="mdl-grid">
      <div class="header-cell mdl-cell mdl-cell--12-col">
        <p class="mdl-typography--title">YouTube<br></p>
      </div>
      <div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">
        Watched&nbsp;<a href="https://www.youtube.com/watch?v=ad000000002">Ad Video</a><br>
        Jun 5, 2026, 11:00:00 AM UTC
      </div>
      <div class="content-cell mdl-cell mdl-cell--12-col mdl-typography--caption">
        <b>Details:</b><br>&emsp;From Google Ads<br>
      </div>
    </div>
  </div>
  <div class="outer-cell mdl-cell mdl-cell--12-col mdl-shadow--2dp">
    <div class="mdl-grid">
      <div class="header-cell mdl-cell mdl-cell--12-col">
        <p class="mdl-typography--title">YouTube Music<br></p>
      </div>
      <div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">
        Watched&nbsp;<a href="https://music.youtube.com/watch?v=mus00000003">Cool Song</a><br>
        <a href="https://www.youtube.com/channel/UC3">Band Topic</a><br>
        Jun 4, 2026, 9:15:00 AM UTC
      </div>
    </div>
  </div>
</div>
</body></html>
"""
    html_path = tmp_path / "watch-history.html"
    html_path.write_text(html_content, encoding="utf-8")

    items = parse_takeout_html(html_path)

    assert len(items) == 3
    assert items[0].header == "YouTube"
    assert items[0].title_url == "https://www.youtube.com/watch?v=vid00000001"
    assert items[0].channel_name == "Tech Channel"
    assert items[0].watched_at_raw == "Jun 6, 2026, 12:34:56 PM UTC"
    assert items[0].watched_at_epoch is not None
    assert items[0].is_ad is False

    assert items[1].is_ad is True
    assert items[1].title_url == "https://www.youtube.com/watch?v=ad000000002"
    assert items[1].channel_name is None

    assert items[2].header == "YouTube Music"
    assert items[2].channel_name == "Band Topic"


def test_parse_takeout_file_validates_extension_and_nonempty(tmp_path: Path) -> None:
    empty_file = tmp_path / "watch-history.json"
    empty_file.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        parse_takeout_file(empty_file)

    csv_file = tmp_path / "watch-history.csv"
    csv_file.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Unsupported Takeout format"):
        parse_takeout_file(csv_file)


def test_parse_takeout_zip_and_explicit_gmt_offset(tmp_path: Path) -> None:
    html_content = """<!DOCTYPE html>
<html><body>
<div class="mdl-grid">
  <div class="outer-cell mdl-cell mdl-cell--12-col mdl-shadow--2dp">
    <div class="mdl-grid">
      <div class="header-cell mdl-cell mdl-cell--12-col">
        <p class="mdl-typography--title">YouTube<br></p>
      </div>
      <div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">
        Watched&nbsp;<a href="https://www.youtube.com/watch?v=YMGrwPqnq3g">History Video</a><br>
        <a href="https://www.youtube.com/channel/UC1">History Channel</a><br>
        Nov 20, 2026, 1:43:53\u202fAM GMT+07:00<br>
      </div>
    </div>
  </div>
</div>
</body></html>
"""
    zip_path = tmp_path / "takeout.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr(
            "Takeout/YouTube and YouTube Music/history/watch-history.html",
            html_content,
        )

    items = parse_takeout_file(zip_path)

    assert len(items) == 1
    assert items[0].channel_name == "History Channel"
    expected_utc = datetime(2026, 11, 19, 18, 43, 53, tzinfo=timezone.utc).timestamp()
    assert items[0].watched_at_epoch == expected_utc
