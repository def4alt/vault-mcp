"""Workflows as MCP prompts; conventions, schema and entry page as resources."""
from __future__ import annotations

from fastmcp import FastMCP

from .. import config

guidance = FastMCP("vault-guidance")


@guidance.prompt
def ingest(material: str) -> str:
    """Capture new material into the vault."""
    return ("Ingest the material below into my vault, following AGENTS (fetch it first). Save the raw material as a "
            "new source, distil one fact per claim, reuse or create entities, resolve conflicts with `supersedes` or "
            "`needs-update`, all in one `save` call. Then `log` it.\n\n---\n" + material)


@guidance.prompt
def ask(question: str) -> str:
    """Answer a question from the vault, its files and the documents."""
    return ("Answer from my vault: `search` (start broad, then filter by tags or links_to), `fetch` the best hits, "
            "follow links one hop, cite the urls, and say which facts are unconfirmed or outdated. Offer to save a "
            "multi-source answer as a synthesis note.\n\nQuestion: " + question)


@guidance.prompt
def review() -> str:
    """Go through unreviewed facts together and confirm, fix or retire them."""
    return ("Call `review_queue`. If a widget appears, let me decide there; otherwise go through the facts with me a "
            "few at a time. Record confirmations with save(frontmatter={set: {status: confirmed, reviewed: today}}), "
            "corrections as new facts that `supersedes` the old one (mark it outdated). Log the session.")


@guidance.prompt
def tidy() -> str:
    """Lint the vault and fix what can be fixed."""
    return ("Run `lint`. Fix what is clearly fixable (missing links and entities, outdated markers, duplicates via "
            "`move`/`save`/`delete`, file notes without tags or summary), propose the rest, save the report as "
            "notes/lint-<date> and log it.")


@guidance.prompt
def share(what: str) -> str:
    """Bring files into the vault for every agent."""
    return ("Get this into the vault as file notes: " + what + ". Use `save_from_url` for URLs, `share_file` for "
            "files attached in this chat, or `request_upload` for files only I have. Then give each new file note a "
            "real summary, tags and categories, and link it from the notes it informs.")


@guidance.resource("vault://AGENTS", mime_type="text/markdown")
def agents() -> str:
    """How agents work in this vault."""
    return (config.VAULT_ROOT / "AGENTS.md").read_text("utf-8")


@guidance.resource("vault://schema", mime_type="text/yaml")
def schema() -> str:
    """Frontmatter schema and the category taxonomy."""
    return (config.VAULT_ROOT / ".schema/notes.yaml").read_text("utf-8")


@guidance.resource("vault://index", mime_type="text/markdown")
def entry() -> str:
    """Entry page of the vault."""
    return (config.VAULT_ROOT / "index.md").read_text("utf-8")
