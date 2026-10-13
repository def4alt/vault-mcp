"""Typed tool results; FastMCP publishes them as output schemas."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class PageInfo(BaseModel):
    path: str
    title: str
    kind: str | None = None
    status: str | None = None
    as_of: str | None = None
    reviewed: str | None = None
    categories: list[str] = []
    summary: str | None = None


class WriteResult(BaseModel):
    paths: list[str]
    action: str
    commit: str


class CommitInfo(BaseModel):
    sha: str
    date: str
    author: str
    message: str


class Change(BaseModel):
    path: str = Field(description="Vault path, e.g. notes/my-note")
    content: str | None = Field(None, description="Whole new file (frontmatter + body); omit with delete=true")
    delete: bool = False


class FileEntry(BaseModel):
    path: str
    kind: Literal["dir", "document", "video", "audio", "image", "data"]
    size: int = 0
    modified: str | None = None
    has_text: bool = False


class FileHit(BaseModel):
    path: str
    kind: str
    snippet: str = ""


class FileText(BaseModel):
    path: str
    kind: str
    size: int
    text: str
    offset: int
    next_offset: int | None = Field(None, description="Pass as offset to read on; null at the end")
    total_chars: int


class Link(BaseModel):
    url: str
    expires_at: str


class Upload(BaseModel):
    id: str
    url: str
    expires_at: str
    delivered: str = Field(description="'elicitation:<action>' if the client showed the page to the user, else 'link'")


class UploadStatus(BaseModel):
    id: str
    ingested: list[dict[str, Any]]
    waiting: list[str]


class FileRef(BaseModel):
    """A file attached in ChatGPT; ChatGPT fills these fields."""
    download_url: str
    file_id: str
    file_name: str | None = None
    mime_type: str | None = None
