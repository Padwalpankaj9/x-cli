"""Media type detection, size limits, and attachment rules for X posts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Soft limits X documents for tweet media (bytes)
IMAGE_MAX_BYTES = 5 * 1024 * 1024
GIF_MAX_BYTES = 15 * 1024 * 1024
VIDEO_MAX_BYTES = 512 * 1024 * 1024

# APPEND chunk size for v1.1 chunked upload
CHUNK_SIZE = 1024 * 1024

MIME_BY_SUFFIX: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
}

CATEGORY_BY_MIME_PREFIX: dict[str, str] = {
    "image/gif": "tweet_gif",
    "video/": "tweet_video",
    "image/": "tweet_image",
}

SIZE_LIMIT_BY_CATEGORY = {
    "tweet_image": IMAGE_MAX_BYTES,
    "tweet_gif": GIF_MAX_BYTES,
    "tweet_video": VIDEO_MAX_BYTES,
}


@dataclass(frozen=True)
class MediaSpec:
    path: Path
    mime_type: str
    category: str
    size: int
    alt_text: str | None = None


def guess_mime(path: Path) -> str:
    mime = MIME_BY_SUFFIX.get(path.suffix.lower())
    if not mime:
        raise ValueError(
            f"Unsupported media type for {path.name}. "
            "Use jpg/png/webp/gif/bmp/tiff or mp4/mov/webm."
        )
    return mime


def guess_category(mime_type: str) -> str:
    if mime_type == "image/gif":
        return "tweet_gif"
    if mime_type.startswith("video/"):
        return "tweet_video"
    if mime_type.startswith("image/"):
        return "tweet_image"
    raise ValueError(f"Cannot map mime type to media category: {mime_type}")


def build_media_spec(
    path: str | Path,
    *,
    alt_text: str | None = None,
    category: str | None = None,
    mime_type: str | None = None,
) -> MediaSpec:
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise ValueError(f"Media file not found: {path}")
    size = p.stat().st_size
    mime = mime_type or guess_mime(p)
    cat = category or guess_category(mime)
    if cat not in SIZE_LIMIT_BY_CATEGORY:
        raise ValueError(
            f"Invalid media category {cat!r}. Use tweet_image, tweet_gif, or tweet_video."
        )
    limit = SIZE_LIMIT_BY_CATEGORY[cat]
    if size > limit:
        raise ValueError(
            f"{p.name} is {size} bytes; {cat} limit is {limit} bytes."
        )
    if alt_text is not None and len(alt_text) > 1000:
        raise ValueError("Alt text must be at most 1000 characters.")
    return MediaSpec(path=p, mime_type=mime, category=cat, size=size, alt_text=alt_text)


def validate_media_set(specs: list[MediaSpec]) -> None:
    """Enforce X post attachment rules before uploading."""
    if not specs:
        return
    if len(specs) > 4:
        raise ValueError("A post can attach at most 4 images (or 1 GIF / 1 video).")

    cats = {s.category for s in specs}
    if len(cats) > 1:
        raise ValueError(
            "Cannot mix media types on one post. "
            "Use only images, or one GIF, or one video."
        )

    cat = next(iter(cats))
    if cat == "tweet_image" and len(specs) > 4:
        raise ValueError("At most 4 images per post.")
    if cat in ("tweet_gif", "tweet_video") and len(specs) != 1:
        raise ValueError(f"Only one {cat.replace('tweet_', '')} per post.")


def pair_alts(paths: list[str], alts: list[str] | None) -> list[str | None]:
    """Align optional --alt values with --media paths (by order)."""
    alts = alts or []
    if len(alts) > len(paths):
        raise ValueError("More --alt values than --media files.")
    out: list[str | None] = list(alts)
    while len(out) < len(paths):
        out.append(None)
    return out
