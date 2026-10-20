"""Domain models for Pinterest media extraction and downloads."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class SourceKind(str, Enum):
    PIN = "pin"
    BOARD = "board"
    SEARCH = "search"
    PROFILE = "profile"
    UNKNOWN = "unknown"
    DIRECT_IMAGE = "direct_image"
    DIRECT_VIDEO = "direct_video"


class Quality(str, Enum):
    ORIGINALS = "originals"
    X1200 = "1200x"
    X736 = "736x"
    X564 = "564x"
    X474 = "474x"
    X236 = "236x"


QUALITY_RANK: list[str] = [
    "originals",
    "1200x",
    "736x",
    "564x",
    "474x",
    "236x",
]


@dataclass(slots=True)
class MediaItem:
    """A single downloadable Pinterest image and/or video."""

    id: str
    image_url: str
    quality: str
    title: str = ""
    description: str = ""
    pin_url: str | None = None
    source_url: str | None = None
    width: int | None = None
    height: int | None = None
    signature: str | None = None
    extension: str = "jpg"
    alt_urls: list[str] = field(default_factory=list)
    is_video: bool = False
    video_url: str | None = None
    video_urls: list[str] = field(default_factory=list)
    video_extension: str = "mp4"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ScrapeResult:
    source_url: str
    kind: SourceKind
    title: str
    items: list[MediaItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_url": self.source_url,
            "kind": self.kind.value,
            "title": self.title,
            "count": len(self.items),
            "items": [i.to_dict() for i in self.items],
            "warnings": list(self.warnings),
            "video_count": sum(1 for i in self.items if i.is_video or i.video_url),
        }


@dataclass(slots=True)
class DownloadResult:
    item: MediaItem
    path: Path | None
    ok: bool
    bytes_written: int = 0
    error: str | None = None
    skipped: bool = False
    paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.item.id,
            "image_url": self.item.image_url,
            "video_url": self.item.video_url,
            "is_video": self.item.is_video or bool(self.item.video_url),
            "path": str(self.path) if self.path else None,
            "paths": list(self.paths),
            "ok": self.ok,
            "bytes_written": self.bytes_written,
            "error": self.error,
            "skipped": self.skipped,
            "quality": self.item.quality,
            "title": self.item.title,
        }
