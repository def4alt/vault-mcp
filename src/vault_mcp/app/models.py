"""Typed tool results; FastMCP publishes them as output schemas.

search and fetch follow the shapes ChatGPT's deep research and company knowledge expect.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class FactEvidence(BaseModel):
    valid_from: str
    valid_until: str | None
    confirmed: str | None
    source: str | None
    note: str | None


class SearchResult(BaseModel):
    id: str = Field(description="Pass to fetch(). A page (notes/x), one fact (notes/x#fact-12) or a document "
                                "(doc:<path>)")
    title: str
    text: str = Field(description="The matching passage, or the fact with its validity period")
    url: str = Field(description="Where a person opens it; cite this")
    fact: FactEvidence | None = Field(None, description="For a fact hit: full validity, confirmation, source and "
                                     "uncertainty note, never clipped with the passage preview. Null otherwise.")


class SearchResults(BaseModel):
    results: list[SearchResult]
    next_cursor: str | None = Field(None, description="Pass back as cursor for more results")


class Document(BaseModel):
    id: str
    title: str
    text: str
    url: str
    metadata: dict[str, Any] = Field(default_factory=dict, description=(
        "updated (pass it back in a WHERE to avoid overwriting someone else's edit), private, backlinks, "
        "download_url and versions for files, view_url for HTML pages, next_offset when the text continues"))


class SqlResult(BaseModel):
    columns: list[str]
    rows: list[dict[str, Any]]
    truncated: bool = Field(description="More rows exist; pass next_cursor to continue")
    next_cursor: str | None = None


class Statement(BaseModel):
    sql: str = Field(description="One INSERT, UPDATE or DELETE on pages, facts, questions or queries")
    params: list[Any] | dict[str, Any] | None = Field(None, description="Values for ? or :name placeholders")
    many: bool = Field(False, description="Set when the statement is meant to change more than 20 rows")


class WriteResult(BaseModel):
    commit: str = Field(description="Git commit of the Markdown export of this change")
    changed: list[str] = Field(description="Ids of the pages (and tables) the statements touched")
    rows: int = Field(0, description="Rows inserted, updated or deleted")
    replayed: bool = Field(False, description="True when this idempotency_key was already applied; nothing changed")


class FileRef(BaseModel):
    """A file attached in ChatGPT; ChatGPT fills these fields."""
    download_url: str
    file_id: str
    file_name: str | None = None
    mime_type: str | None = None


class UploadResult(BaseModel):
    note: str | None = Field(None, description="The file page created (or found, if the file was already there)")
    duplicate: bool | None = None
    view_url: str | None = Field(None, description="Stable link to open an HTML file (a page) in a browser")
    put_url: str | None = Field(None, description="HTTP PUT target; replace {filename} (URL-encoded)")
    curl: str | None = Field(None, description="Ready upload command; prints the file page as JSON")
    expires_at: str | None = None
