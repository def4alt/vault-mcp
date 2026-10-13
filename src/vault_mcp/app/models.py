"""Typed tool results; FastMCP publishes them as output schemas."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class Hit(BaseModel):
    id: str = Field(description="Pass to fetch(). Vault pages: notes/x, files/x...; documents: doc:<path>")
    title: str
    url: str | None = Field(None, description="Where Andrii can open it (cite this)")
    kind: str | None = None
    snippet: str | None = None
    status: str | None = None
    as_of: str | None = None
    summary: str | None = None
    tags: list[str] | None = None
    size: int | None = None
    score: float | None = None


class SearchResults(BaseModel):
    results: list[Hit]
    next_cursor: str | None = Field(None, description="Pass back as cursor for the next page")


class FetchedItem(BaseModel):
    id: str
    title: str
    text: str
    url: str | None = None
    metadata: dict[str, Any] = {}
    next_offset: int | None = Field(None, description="Pass as offset to read on; null at the end")


class FrontmatterPatch(BaseModel):
    set: dict[str, Any] = {}
    unset: list[str] = []


class Change(BaseModel):
    path: str = Field(description="Vault path, e.g. notes/my-note or files/slides")
    content: str | None = Field(None, description="Whole new file (frontmatter + body)")
    frontmatter: FrontmatterPatch | None = Field(None, description="Change only frontmatter fields")


class WriteResult(BaseModel):
    paths: list[str]
    commit: str


class CommitInfo(BaseModel):
    sha: str
    date: str
    author: str
    message: str


class History(BaseModel):
    commits: list[CommitInfo]
    next_cursor: str | None = None


class Link(BaseModel):
    url: str
    expires_at: str
    curl: str = Field(description="Download command for an agent with a shell")


class Upload(BaseModel):
    id: str = Field(description="Pass to upload_status() after uploading")
    page_url: str = Field(description="Upload page for a person (drag and drop)")
    put_url_template: str = Field(description="PUT a file to this URL with {filename} replaced (URL-encoded)")
    curl: str = Field(description="Upload command for an agent with a shell")
    expires_at: str


class Filed(BaseModel):
    note: str = Field(description="The file note, e.g. files/slides")
    blob: str | None = None
    duplicate: bool = False


class UploadStatus(BaseModel):
    id: str
    filed: list[Filed]
    waiting: list[str]


class FileRef(BaseModel):
    """A file attached in ChatGPT; ChatGPT fills these fields."""
    download_url: str
    file_id: str
    file_name: str | None = None
    mime_type: str | None = None


class ReviewItem(BaseModel):
    id: str
    title: str
    summary: str | None = None
    as_of: str | None = None
    status: str | None = None
    url: str | None = None


class ReviewQueue(BaseModel):
    items: list[ReviewItem]
    remaining: int
