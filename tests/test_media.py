"""Tests for media helpers and post body media_ids."""

from pathlib import Path

import pytest

from x_cli.api import XApiClient
from x_cli.auth import Credentials
from x_cli.media import (
    build_media_spec,
    guess_category,
    guess_mime,
    pair_alts,
    validate_media_set,
)


def test_guess_mime_and_category(tmp_path: Path):
    png = tmp_path / "a.png"
    png.write_bytes(b"x")
    assert guess_mime(png) == "image/png"
    assert guess_category("image/png") == "tweet_image"
    assert guess_category("image/gif") == "tweet_gif"
    assert guess_category("video/mp4") == "tweet_video"


def test_pair_alts_padding():
    assert pair_alts(["a.png", "b.png"], ["one"]) == ["one", None]
    with pytest.raises(ValueError):
        pair_alts(["a.png"], ["one", "two"])


def test_validate_media_set_rejects_mix(tmp_path: Path):
    img = tmp_path / "a.png"
    gif = tmp_path / "b.gif"
    img.write_bytes(b"x" * 10)
    gif.write_bytes(b"x" * 10)
    specs = [build_media_spec(img), build_media_spec(gif)]
    with pytest.raises(ValueError, match="Cannot mix"):
        validate_media_set(specs)


def test_validate_media_set_rejects_two_videos(tmp_path: Path):
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(b"x" * 10)
    b.write_bytes(b"x" * 10)
    specs = [build_media_spec(a), build_media_spec(b)]
    with pytest.raises(ValueError, match="Only one"):
        validate_media_set(specs)


def test_post_tweet_includes_media_ids(monkeypatch):
    creds = Credentials(
        api_key="k",
        api_secret="s",
        access_token="t",
        access_token_secret="ts",
        bearer_token="b",
    )
    client = XApiClient(creds)
    captured: dict = {}

    def fake_oauth(method, url, json_body=None):
        captured["method"] = method
        captured["url"] = url
        captured["body"] = json_body
        return {"data": {"id": "1", "text": "hi"}}

    monkeypatch.setattr(client, "_oauth_request", fake_oauth)
    client.post_tweet("hi", media_ids=["111", "222"])
    assert captured["body"]["media"]["media_ids"] == ["111", "222"]


def test_post_tweet_rejects_poll_with_media():
    creds = Credentials(
        api_key="k",
        api_secret="s",
        access_token="t",
        access_token_secret="ts",
        bearer_token="b",
    )
    client = XApiClient(creds)
    with pytest.raises(ValueError, match="poll and media"):
        client.post_tweet("hi", poll_options=["A", "B"], media_ids=["1"])
