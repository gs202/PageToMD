"""Allow ``python -m pagetomd`` to invoke the Typer CLI."""

from __future__ import annotations

from pagetomd.cli import app

if __name__ == "__main__":  # pragma: no cover
    app()
