"""Workflows as MCP prompts, and the vault's entry pages as resources."""
from __future__ import annotations

from fastmcp import FastMCP

from .. import config

guidance = FastMCP("vault-guidance")


@guidance.prompt
def ingest(material: str) -> str:
    """Capture new material into the vault."""
    return ("Ingest the material below into my vault, following AGENTS (read it first). Save the raw material "
            "as a new source, distil one fact per claim, reuse or create entities, resolve conflicts with "
            "`supersedes` or `needs-update`, all in one `apply_changes` commit. Then `append_log`.\n\n---\n"
            + material)


@guidance.prompt
def ask(question: str) -> str:
    """Answer a question from the vault and the documents."""
    return ("Answer from my vault: start at the relevant entities (`search`, `backlinks`), follow links one hop, "
            "check documents with `search_files` when useful, cite note paths, and say which facts are "
            "unconfirmed or outdated. Offer to save a multi-note answer as a synthesis.\n\nQuestion: " + question)


@guidance.prompt
def review() -> str:
    """Go through unreviewed facts together and confirm, fix or retire them."""
    return ("Run `query` for unreviewed facts, oldest first, and go through them with me a few at a time. "
            "When I confirm one, set status confirmed and reviewed to today; when I correct one, write the "
            "corrected fact and mark the old one outdated with supersedes. Log the session.")


@guidance.prompt
def tidy() -> str:
    """Lint the vault and fix what can be fixed."""
    return ("Run `lint`. Fix what is clearly fixable (missing links and entities, outdated markers, duplicate "
            "notes via `move_note` or `apply_changes`), propose the rest, save the report as "
            "notes/lint-<date> and log it.")


@guidance.prompt
def share(what: str) -> str:
    """Put files into the shared space for the other agents."""
    return ("Get this into the shared space so every agent can use it: " + what + ". Use `fetch_to_shared` "
            "for URLs, `share_file` for files attached in this chat, or `request_upload` for files only "
            "Andrii has. Then sharpen the new source note's summary and link it from relevant notes.")


@guidance.resource("vault://AGENTS", mime_type="text/markdown")
def agents() -> str:
    """Conventions for agents working in the vault."""
    return (config.VAULT_ROOT / "AGENTS.md").read_text("utf-8")


@guidance.resource("vault://index", mime_type="text/markdown")
def index() -> str:
    """Entry page of the vault."""
    return (config.VAULT_ROOT / "index.md").read_text("utf-8")


@guidance.resource("vault://notes/{name}", mime_type="text/markdown")
def note(name: str) -> str:
    """A note by name, e.g. vault://notes/dashpoint."""
    return (config.VAULT_ROOT / "notes" / f"{name}.md").read_text("utf-8")
