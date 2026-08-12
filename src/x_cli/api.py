"""Twitter API v2 client with OAuth 1.0a and Bearer token auth."""

from __future__ import annotations

from typing import Any
import base64
import time
from pathlib import Path

import httpx

from .auth import Credentials, generate_oauth_header
from . import oauth2
from .media import CHUNK_SIZE, MediaSpec, build_media_spec, validate_media_set

API_BASE = "https://api.x.com/2"
UPLOAD_BASE = "https://upload.twitter.com/1.1"
FULL_ARCHIVE_START_TIME = "2006-03-21T00:00:00Z"


def _merge_paginated_responses(pages: list[dict[str, Any]]) -> dict[str, Any]:
    if not pages:
        return {"data": [], "meta": {"result_count": 0}}

    merged: dict[str, Any] = {"data": [], "includes": {}, "meta": {}}
    seen_tweets: set[str] = set()

    for page in pages:
        for tweet in page.get("data", []):
            tweet_id = tweet.get("id")
            if tweet_id and tweet_id in seen_tweets:
                continue
            if tweet_id:
                seen_tweets.add(tweet_id)
            merged["data"].append(tweet)

        for include_key, include_items in page.get("includes", {}).items():
            target = merged["includes"].setdefault(include_key, [])
            seen_include_ids = {
                item.get("id") or item.get("media_key")
                for item in target
                if isinstance(item, dict)
            }
            for item in include_items:
                item_id = item.get("id") or item.get("media_key")
                if item_id and item_id in seen_include_ids:
                    continue
                target.append(item)
                if item_id:
                    seen_include_ids.add(item_id)

    last_meta = pages[-1].get("meta", {})
    merged["meta"] = {
        **last_meta,
        "result_count": len(merged["data"]),
        "pages": len(pages),
    }
    return merged


class XApiClient:
    def __init__(self, creds: Credentials) -> None:
        self.creds = creds
        self._user_id: str | None = None
        # Media APPEND chunks and video STATUS need a longer budget than text posts
        self._http = httpx.Client(timeout=httpx.Timeout(120.0, connect=30.0))

    def close(self) -> None:
        self._http.close()

    # ---- internal ----

    def _bearer_get(self, url: str) -> dict[str, Any]:
        resp = self._http.get(url, headers={"Authorization": f"Bearer {self.creds.bearer_token}"})
        return self._handle(resp)

    def _oauth2_request(self, method: str, url: str, json_body: dict | None = None) -> dict[str, Any]:
        if not (self.creds.oauth2_client_id and self.creds.oauth2_client_secret):
            raise RuntimeError(
                "OAuth 2.0 client creds missing. Set X_OAUTH2_CLIENT_ID and X_OAUTH2_CLIENT_SECRET."
            )
        token = oauth2.get_valid_access_token(
            self.creds.oauth2_client_id, self.creds.oauth2_client_secret
        )
        headers = {"Authorization": f"Bearer {token}"}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        resp = self._http.request(method, url, headers=headers, json=json_body if json_body else None)
        return self._handle(resp)

    def _oauth_request(self, method: str, url: str, json_body: dict | None = None) -> dict[str, Any]:
        auth_header = generate_oauth_header(method, url, self.creds)
        headers: dict[str, str] = {"Authorization": auth_header}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        resp = self._http.request(method, url, headers=headers, json=json_body if json_body else None)
        return self._handle(resp)

    def _handle(self, resp: httpx.Response) -> dict[str, Any]:
        if resp.status_code == 429:
            reset = resp.headers.get("x-rate-limit-reset", "unknown")
            raise RuntimeError(f"Rate limited. Resets at {reset}.")
        # APPEND returns 204 with an empty body (content may be missing on test fakes)
        content = getattr(resp, "content", None)
        if content is not None and not content:
            if resp.is_success:
                return {}
            raise RuntimeError(f"API error (HTTP {resp.status_code}): empty body")
        try:
            data = resp.json()
        except Exception:
            if resp.is_success:
                return {}
            raise RuntimeError(f"API error (HTTP {resp.status_code}): {resp.text[:500]}")
        if not resp.is_success:
            errors = data.get("errors", []) if isinstance(data, dict) else []
            msg = "; ".join(e.get("detail") or e.get("message", "") for e in errors) or resp.text[:500]
            raise RuntimeError(f"API error (HTTP {resp.status_code}): {msg}")
        return data if isinstance(data, dict) else {"data": data}

    def _oauth_form(self, method: str, url: str, form: dict[str, str]) -> dict[str, Any]:
        """OAuth 1.0a form POST; form fields are part of the signature."""
        auth_header = generate_oauth_header(method, url, self.creds, form)
        headers = {
            "Authorization": auth_header,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        resp = self._http.request(method, url, headers=headers, data=form)
        return self._handle(resp)

    def get_authenticated_user_id(self) -> str:
        if self._user_id:
            return self._user_id
        data = self._oauth_request("GET", f"{API_BASE}/users/me")
        self._user_id = data["data"]["id"]
        return self._user_id

    # ---- tweets ----

    def post_tweet(
        self,
        text: str,
        reply_to: str | None = None,
        quote_tweet_id: str | None = None,
        poll_options: list[str] | None = None,
        poll_duration_minutes: int = 1440,
        media_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        if poll_options and media_ids:
            raise ValueError("Cannot attach both a poll and media on the same post.")
        body: dict[str, Any] = {"text": text}
        if reply_to:
            # NOTE: X API restricts programmatic replies (Feb 2024). Replies only
            # succeed if the original author @mentioned you or quoted your post.
            body["reply"] = {"in_reply_to_tweet_id": reply_to}
        if quote_tweet_id:
            body["quote_tweet_id"] = quote_tweet_id
        if poll_options:
            body["poll"] = {"options": poll_options, "duration_minutes": poll_duration_minutes}
        if media_ids:
            body["media"] = {"media_ids": [str(m) for m in media_ids]}
        return self._oauth_request("POST", f"{API_BASE}/tweets", body)

    def delete_tweet(self, tweet_id: str) -> dict[str, Any]:
        return self._oauth_request("DELETE", f"{API_BASE}/tweets/{tweet_id}")

    def get_tweet(self, tweet_id: str) -> dict[str, Any]:
        params = {
            "tweet.fields": "created_at,public_metrics,author_id,conversation_id,in_reply_to_user_id,referenced_tweets,attachments,entities,lang,note_tweet",
            "expansions": "author_id,referenced_tweets.id,attachments.media_keys",
            "user.fields": "name,username,verified,profile_image_url,public_metrics",
            "media.fields": "url,preview_image_url,type,width,height,alt_text",
        }
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        return self._bearer_get(f"{API_BASE}/tweets/{tweet_id}?{qs}")

    def search_tweets(
        self,
        query: str,
        max_results: int = 10,
        *,
        next_token: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> dict[str, Any]:
        max_results = max(10, min(max_results, 100))
        params = {
            "query": query,
            "max_results": str(max_results),
            "tweet.fields": "created_at,public_metrics,author_id,conversation_id,entities,lang,note_tweet",
            "expansions": "author_id,attachments.media_keys",
            "user.fields": "name,username,verified,profile_image_url",
            "media.fields": "url,preview_image_url,type",
        }
        if next_token:
            params["next_token"] = next_token
        if start_time:
            params["start_time"] = start_time
        if end_time:
            params["end_time"] = end_time
        url = f"{API_BASE}/tweets/search/recent"
        resp = self._http.get(url, params=params, headers={"Authorization": f"Bearer {self.creds.bearer_token}"})
        return self._handle(resp)

    def search_all_tweets(
        self,
        query: str,
        max_results: int = 10,
        *,
        next_token: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> dict[str, Any]:
        max_results = max(10, min(max_results, 500))
        start_time = start_time or FULL_ARCHIVE_START_TIME
        params = {
            "query": query,
            "max_results": str(max_results),
            "tweet.fields": "created_at,public_metrics,author_id,conversation_id,entities,lang,note_tweet",
            "expansions": "author_id,attachments.media_keys",
            "user.fields": "name,username,verified,profile_image_url",
            "media.fields": "url,preview_image_url,type",
        }
        if next_token:
            params["next_token"] = next_token
        if start_time:
            params["start_time"] = start_time
        if end_time:
            params["end_time"] = end_time
        resp = self._http.get(
            f"{API_BASE}/tweets/search/all",
            params=params,
            headers={"Authorization": f"Bearer {self.creds.bearer_token}"},
        )
        return self._handle(resp)

    def search_tweets_paginated(
        self,
        query: str,
        max_results: int,
        *,
        archive: bool = False,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> dict[str, Any]:
        page_size = 500 if archive else 100
        if archive:
            start_time = start_time or FULL_ARCHIVE_START_TIME
        remaining = max(10, max_results)
        next_token: str | None = None
        pages: list[dict[str, Any]] = []

        while remaining > 0:
            fetch_size = min(page_size, remaining)
            page = (
                self.search_all_tweets(
                    query,
                    fetch_size,
                    next_token=next_token,
                    start_time=start_time,
                    end_time=end_time,
                )
                if archive
                else self.search_tweets(
                    query,
                    fetch_size,
                    next_token=next_token,
                    start_time=start_time,
                    end_time=end_time,
                )
            )
            pages.append(page)
            remaining -= len(page.get("data", []))
            next_token = page.get("meta", {}).get("next_token")
            if not next_token or not page.get("data"):
                break

        return _merge_paginated_responses(pages)

    def get_tweet_metrics(self, tweet_id: str) -> dict[str, Any]:
        params = "tweet.fields=public_metrics,non_public_metrics,organic_metrics"
        return self._oauth_request("GET", f"{API_BASE}/tweets/{tweet_id}?{params}")


    # ---- media upload (v1.1 chunked, OAuth 1.0a) ----

    def upload_media_file(
        self,
        path: str | Path,
        *,
        alt_text: str | None = None,
        category: str | None = None,
        mime_type: str | None = None,
        wait: bool = True,
    ) -> dict[str, Any]:
        """Upload one local file. Returns dict with media_id and finalize payload."""
        spec = build_media_spec(path, alt_text=alt_text, category=category, mime_type=mime_type)
        return self.upload_media_spec(spec, wait=wait)

    def upload_media_spec(self, spec: MediaSpec, *, wait: bool = True) -> dict[str, Any]:
        media_id = self._chunked_upload(spec)
        if spec.alt_text:
            self.set_media_alt_text(media_id, spec.alt_text)
        result: dict[str, Any] = {"media_id": media_id, "path": str(spec.path), "category": spec.category}
        if wait and spec.category in ("tweet_video", "tweet_gif"):
            result["processing"] = self.wait_for_media(media_id)
        return result

    def upload_media_paths(
        self,
        paths: list[str],
        *,
        alts: list[str | None] | None = None,
        wait: bool = True,
    ) -> list[str]:
        """Upload several files, validate mix rules, return media_ids in order."""
        alts = alts or [None] * len(paths)
        if len(alts) != len(paths):
            raise ValueError("alts length must match paths length")
        specs = [
            build_media_spec(p, alt_text=a)
            for p, a in zip(paths, alts, strict=True)
        ]
        validate_media_set(specs)
        return [self.upload_media_spec(s, wait=wait)["media_id"] for s in specs]

    def _chunked_upload(self, spec: MediaSpec) -> str:
        upload_url = f"{UPLOAD_BASE}/media/upload.json"
        data = spec.path.read_bytes()
        total = len(data)

        # INIT
        init = self._oauth_form(
            "POST",
            upload_url,
            {
                "command": "INIT",
                "total_bytes": str(total),
                "media_type": spec.mime_type,
                "media_category": spec.category,
            },
        )
        media_id = str(init.get("media_id_string") or init.get("media_id") or "")
        if not media_id:
            raise RuntimeError(f"Media INIT returned no media_id: {init}")

        # APPEND in chunks (base64 media_data; fields signed)
        for i in range(0, total, CHUNK_SIZE):
            chunk = data[i : i + CHUNK_SIZE]
            segment_index = str(i // CHUNK_SIZE)
            self._oauth_form(
                "POST",
                upload_url,
                {
                    "command": "APPEND",
                    "media_id": media_id,
                    "segment_index": segment_index,
                    "media_data": base64.b64encode(chunk).decode("ascii"),
                },
            )

        # FINALIZE
        finalized = self._oauth_form(
            "POST",
            upload_url,
            {"command": "FINALIZE", "media_id": media_id},
        )
        # Video/GIF may need STATUS polling; caller can wait_for_media
        processing = finalized.get("processing_info")
        if processing and processing.get("state") in ("pending", "in_progress"):
            # store for wait helper via return only media_id; wait is separate
            pass
        return media_id

    def get_media_status(self, media_id: str) -> dict[str, Any]:
        url = f"{UPLOAD_BASE}/media/upload.json"
        params = {"command": "STATUS", "media_id": str(media_id)}
        # STATUS is GET; query params go into OAuth signature via URL parse
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        full = f"{url}?{qs}"
        auth_header = generate_oauth_header("GET", full, self.creds)
        resp = self._http.get(full, headers={"Authorization": auth_header})
        return self._handle(resp)

    def wait_for_media(self, media_id: str, timeout_sec: int = 180) -> dict[str, Any]:
        """Poll STATUS until video/GIF processing succeeds or fails."""
        deadline = time.time() + timeout_sec
        last: dict[str, Any] = {}
        while time.time() < deadline:
            last = self.get_media_status(media_id)
            info = last.get("processing_info") or {}
            state = info.get("state")
            if not state or state == "succeeded":
                return last
            if state == "failed":
                err = info.get("error") or last
                raise RuntimeError(f"Media processing failed for {media_id}: {err}")
            wait = int(info.get("check_after_secs") or 2)
            time.sleep(max(1, wait))
        raise RuntimeError(f"Media processing timed out for {media_id} after {timeout_sec}s: {last}")

    def set_media_alt_text(self, media_id: str, alt_text: str) -> dict[str, Any]:
        """Attach alt text after FINALIZE and before creating the post."""
        if len(alt_text) > 1000:
            raise ValueError("Alt text must be at most 1000 characters.")
        url = f"{UPLOAD_BASE}/media/metadata/create.json"
        body = {"media_id": str(media_id), "alt_text": {"text": alt_text}}
        # JSON body is not included in OAuth 1.0a signature for this endpoint
        return self._oauth_request("POST", url, body)

    # ---- users ----

    def get_user(self, username: str) -> dict[str, Any]:
        fields = "user.fields=created_at,description,public_metrics,verified,profile_image_url,url,location,pinned_tweet_id"
        return self._bearer_get(f"{API_BASE}/users/by/username/{username}?{fields}")

    def get_timeline(self, user_id: str, max_results: int = 10) -> dict[str, Any]:
        max_results = max(5, min(max_results, 100))
        params = {
            "max_results": str(max_results),
            "tweet.fields": "created_at,public_metrics,author_id,conversation_id,entities,lang,note_tweet",
            "expansions": "author_id,attachments.media_keys,referenced_tweets.id",
            "user.fields": "name,username,verified",
            "media.fields": "url,preview_image_url,type",
        }
        resp = self._http.get(
            f"{API_BASE}/users/{user_id}/tweets",
            params=params,
            headers={"Authorization": f"Bearer {self.creds.bearer_token}"},
        )
        return self._handle(resp)

    def get_followers(self, user_id: str, max_results: int = 100) -> dict[str, Any]:
        max_results = max(1, min(max_results, 1000))
        params = {
            "max_results": str(max_results),
            "user.fields": "created_at,description,public_metrics,verified,profile_image_url",
        }
        resp = self._http.get(
            f"{API_BASE}/users/{user_id}/followers",
            params=params,
            headers={"Authorization": f"Bearer {self.creds.bearer_token}"},
        )
        return self._handle(resp)

    def get_following(self, user_id: str, max_results: int = 100) -> dict[str, Any]:
        max_results = max(1, min(max_results, 1000))
        params = {
            "max_results": str(max_results),
            "user.fields": "created_at,description,public_metrics,verified,profile_image_url",
        }
        resp = self._http.get(
            f"{API_BASE}/users/{user_id}/following",
            params=params,
            headers={"Authorization": f"Bearer {self.creds.bearer_token}"},
        )
        return self._handle(resp)

    def get_mentions(self, max_results: int = 10) -> dict[str, Any]:
        user_id = self.get_authenticated_user_id()
        max_results = max(5, min(max_results, 100))
        params = {
            "max_results": str(max_results),
            "tweet.fields": "created_at,public_metrics,author_id,conversation_id,entities,note_tweet",
            "expansions": "author_id",
            "user.fields": "name,username,verified",
        }
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{API_BASE}/users/{user_id}/mentions?{qs}"
        return self._oauth_request("GET", url)

    # ---- engagement ----

    def like_tweet(self, tweet_id: str) -> dict[str, Any]:
        user_id = self.get_authenticated_user_id()
        return self._oauth_request("POST", f"{API_BASE}/users/{user_id}/likes", {"tweet_id": tweet_id})

    def retweet(self, tweet_id: str) -> dict[str, Any]:
        user_id = self.get_authenticated_user_id()
        return self._oauth_request("POST", f"{API_BASE}/users/{user_id}/retweets", {"tweet_id": tweet_id})

    # ---- bookmarks (require OAuth 2.0 User Context) ----

    def get_bookmarks(self, max_results: int = 10) -> dict[str, Any]:
        user_id = self.get_authenticated_user_id()
        max_results = max(1, min(max_results, 100))
        params = {
            "max_results": str(max_results),
            "tweet.fields": "created_at,public_metrics,author_id,conversation_id,entities,lang,note_tweet",
            "expansions": "author_id,attachments.media_keys",
            "user.fields": "name,username,verified,profile_image_url",
            "media.fields": "url,preview_image_url,type",
        }
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{API_BASE}/users/{user_id}/bookmarks?{qs}"
        return self._oauth2_request("GET", url)

    def bookmark_tweet(self, tweet_id: str) -> dict[str, Any]:
        user_id = self.get_authenticated_user_id()
        return self._oauth2_request("POST", f"{API_BASE}/users/{user_id}/bookmarks", {"tweet_id": tweet_id})

    def unbookmark_tweet(self, tweet_id: str) -> dict[str, Any]:
        user_id = self.get_authenticated_user_id()
        return self._oauth2_request("DELETE", f"{API_BASE}/users/{user_id}/bookmarks/{tweet_id}")

    # ---- likes lookup (requires OAuth 2.0 User Context) ----

    def get_liked_tweets(self, max_results: int = 10) -> dict[str, Any]:
        tokens = oauth2.load_tokens()
        scopes = set((tokens or {}).get("scope", "").split())
        if "like.read" not in scopes:
            raise RuntimeError(
                "OAuth 2.0 token is missing like.read. Run: "
                "x-cli auth login --scopes tweet.read,users.read,bookmark.read,bookmark.write,like.read,offline.access"
            )
        user_id = self.get_authenticated_user_id()
        max_results = max(5, min(max_results, 100))
        params = {
            "max_results": str(max_results),
            "tweet.fields": "created_at,public_metrics,author_id,conversation_id,entities,lang,note_tweet",
            "expansions": "author_id,attachments.media_keys",
            "user.fields": "name,username,verified,profile_image_url",
            "media.fields": "url,preview_image_url,type",
        }
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{API_BASE}/users/{user_id}/liked_tweets?{qs}"
        return self._oauth2_request("GET", url)
