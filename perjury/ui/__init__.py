"""Zero-build same-origin demo UI (#18): static HTML/CSS/vanilla JS served by FastAPI."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

UI_DIR = Path(__file__).parent


def mount_ui(app: FastAPI) -> None:
    """Serve the demo shell at /demo and its assets at /ui, from package-relative paths."""
    app.mount("/ui", StaticFiles(directory=UI_DIR), name="ui")

    @app.get("/demo", include_in_schema=False)
    def demo() -> FileResponse:
        return FileResponse(UI_DIR / "index.html")
