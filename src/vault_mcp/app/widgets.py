"""MCP Apps: HTML widgets served as ui:// resources, with the shared bridge inlined."""
from __future__ import annotations

from importlib import resources

from fastmcp import FastMCP
from fastmcp.apps import UI_MIME_TYPE, AppConfig, ResourceCSP

from .. import config

UPLOAD_URI = "ui://vault/upload.html"
REVIEW_URI = "ui://vault/review.html"


def page(name: str) -> str:
    ui = resources.files(__package__) / "ui"
    return (ui / name).read_text("utf-8").replace("/*BRIDGE*/", (ui / "bridge.js").read_text("utf-8"))


widgets = FastMCP("vault-widgets")


@widgets.resource(UPLOAD_URI, mime_type=UI_MIME_TYPE, name="upload-widget",
                  app=AppConfig(csp=ResourceCSP(connect_domains=[config.FILES_BASE_URL]), prefers_border=True))
def upload_widget() -> str:
    """Drag-and-drop upload into the vault."""
    return page("upload.html")


@widgets.resource(REVIEW_URI, mime_type=UI_MIME_TYPE, name="review-widget", app=AppConfig(prefers_border=True))
def review_widget() -> str:
    """Confirm or retire unreviewed facts."""
    return page("review.html")
