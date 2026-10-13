"""Typed tool results; FastMCP publishes them as output schemas.

search and fetch follow the shapes ChatGPT's deep research and company knowledge expect.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SearchResult(BaseModel):
    id: str = Field(description="Pass to fetch(). Pages: notes/x, files/x...; documents: doc:<path>")
    title: str
    text: str = Field(description="Matching passage")
    url: str = Field(description="Where a person opens it; cite this")


class SearchResults(BaseModel):
    results: list[SearchResult]
    next_cursor: str | None = Field(None, description="Pass back as cursor for more results")


class Document(BaseModel):
    id: str
    title: str
    text: str
    url: str
    metadata: dict[str, Any] = Field(default_factory=dict, description=(
        "Frontmatter, rev (pass as base_rev to write), backlinks, download_url for files, "
        "next_offset when the text continues"))


class SqlResult(BaseModel):
    columns: list[str]
    rows: list[dict[str, Any]]
    truncated: bool = Field(description="More rows exist; pass next_cursor to continue")
    next_cursor: str | None = None


class FrontmatterPatch(BaseModel):
    set: dict[str, Any] = {}
    unset: list[str] = []


class Change(BaseModel):
    path: str = Field(description="Vault path, e.g. notes/my-note or files/slides (.md is implied)")
    content: str | None = Field(None, description="The whole new file: frontmatter + body")
    frontmatter: FrontmatterPatch | None = Field(None, description="Change only these frontmatter fields")
    move_to: str | None = Field(None, description="Rename/move the page; links to it are rewritten everywhere")
    base_rev: str | None = Field(None, description=(
        "rev you read (fetch metadata.rev or notes.rev); refused if the page changed since. 'new' = must not exist"))


class WriteResult(BaseModel):
    commit: str
    revs: dict[str, str | None] = Field(description="New rev of every path touched (null = deleted)")
    replayed: bool = Field(False, description="True when this idempotency_key was already applied; nothing changed")


class FileRef(BaseModel):
    """A file attached in ChatGPT; ChatGPT fills these fields."""
    download_url: str
    file_id: str
    file_name: str | None = None
    mime_type: str | None = None


class UploadResult(BaseModel):
    note: str | None = Field(None, description="The file note created (or found, if the file was already there)")
    duplicate: bool | None = None
    put_url: str | None = Field(None, description="HTTP PUT target; replace {filename} (URL-encoded)")
    curl: str | None = Field(None, description="Ready upload command; prints the file note as JSON")
    expires_at: str | None = None
