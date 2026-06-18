"""Command-line interface for :mod:`pagetomd`.

This module exposes a single Typer command — ``pagetomd <url> [options]`` —
that builds a :class:`~pagetomd.config.Config`, configures structured logging,
runs the conversion pipeline, and translates any
:class:`~pagetomd.exceptions.PageToMdError` into a stable process exit code
with stable exit codes.
"""

from __future__ import annotations

import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Final

import typer

from pagetomd import __version__
from pagetomd.config import Config
from pagetomd.crawler import CrawlResult, crawl
from pagetomd.exceptions import PageToMdError, UsageError
from pagetomd.logging import configure_logging, get_logger
from pagetomd.pipeline import PipelineResult, run

__all__ = ["app", "main"]

# Standard Unix convention: 128 + signal number. SIGINT = 2 → 130.
EXIT_INTERRUPTED: Final[int] = 130

app = typer.Typer(
    name="pagetomd",
    add_completion=False,
    rich_markup_mode="rich",
)


def _version_callback(value: bool) -> None:
    """Print the version and exit when ``--version`` is supplied.

    Args:
        value: ``True`` when the flag was passed on the command line.

    Raises:
        typer.Exit: Always when ``value`` is true — with exit code ``0``.
    """
    if value:
        typer.echo(f"pagetomd {__version__}")
        raise typer.Exit(0)


def _coerce_output(output: str | None) -> Path | None:
    """Translate the raw ``--output`` string into a :class:`Path` (or ``None``)."""
    if output is None:
        return None
    return Path(output)


# Maps each Config field name to a ``(cli_param_name, transform)`` pair.
#
# ``cli_param_name`` is the Python parameter name Typer uses in ``ctx.params``
# (snake_case, matching the ``main()`` signature).  ``transform`` converts the
# raw CLI value to the value expected by :class:`~pagetomd.config.Config`.
#
# **Adding a new CLI flag only requires two edits:**
#   1. Add the parameter to ``main()`` with its ``typer.Option(...)`` annotation.
#   2. Add an entry here.
#
# The dict replaces the old ``_CLI_OVERRIDE_NAMES`` tuple (which tracked *what*
# to check) and the ``values`` dict inside ``_build_config()`` (which tracked
# *how* to transform each value) — eliminating two of the original four
# parallel structures.
_OPTION_TRANSFORMS: dict[str, tuple[str, Callable[[object], object]]] = {
    # config field      cli param name    transform
    "output": ("output", lambda v: _coerce_output(v)),  # type: ignore[arg-type]
    "overwrite": ("overwrite", lambda v: v),
    "follow_symlinks": ("follow_symlinks", lambda v: v),
    "fetcher": ("fetcher", lambda v: v),
    "timeout": ("timeout", lambda v: v),
    "retries": ("retries", lambda v: v),
    "user_agent": ("user_agent", lambda v: v),
    "respect_robots": ("respect_robots", lambda v: v),
    "max_redirects": ("max_redirects", lambda v: v),
    "include_comments": ("include_comments", lambda v: v),
    "include_images": ("include_images", lambda v: v),
    "include_links": ("include_links", lambda v: v),
    "heading_style": ("heading_style", lambda v: v),
    "code_fences": ("code_fences", lambda v: v),
    "wide_tables": ("wide_tables", lambda v: v),
    "no_fetched_at": ("no_fetched_at", lambda v: v),
    "log_level": ("log_level", lambda v: v),
    "log_json": ("log_json", lambda v: v),
    "playwright_idle_ms": ("playwright_idle_ms", lambda v: v),
    # Inverted flag: --no-verify-ssl on the CLI → verify_ssl=False in Config.
    "verify_ssl": ("no_verify_ssl", lambda v: not v),
}


@app.command(no_args_is_help=True)
def main(
    ctx: typer.Context,
    url: Annotated[
        str,
        typer.Argument(help="URL of the page to convert."),
    ],
    output: Annotated[
        str | None,
        typer.Option(
            "--output",
            "-o",
            help='Output file path, or "-" for stdout. Defaults to a slug from the title.',
        ),
    ] = None,
    overwrite: Annotated[
        bool,
        typer.Option("--overwrite", help="Replace an existing destination file."),
    ] = False,
    follow_symlinks: Annotated[
        bool,
        typer.Option(
            "--follow-symlinks/--no-follow-symlinks",
            help=(
                "Allow writes whose destination is a symlink. Off by default — "
                "the writer refuses symlinked targets so --overwrite cannot be "
                "tricked into clobbering a file outside the intended directory."
            ),
        ),
    ] = False,
    fetcher: Annotated[
        str,
        typer.Option(
            "--fetcher",
            help=(
                'Fetcher backend: "httpx" (default, static fetch), '
                '"playwright" (always render in headless Chromium), '
                'or "auto" (try httpx first, fall back to playwright on SPA shells). '
                "Browser backends launch headless Chromium with hardening flags "
                "(sandbox + 512 MB heap cap) but cannot fully bound resource use "
                "against adversarial pages."
            ),
            case_sensitive=False,
        ),
    ] = "httpx",
    timeout: Annotated[
        float,
        typer.Option("--timeout", help="HTTP timeout in seconds."),
    ] = 30.0,
    retries: Annotated[
        int,
        typer.Option(
            "--retries",
            help=(
                "Retry attempts on transient failures (per page). "
                "Default 4 yields up to 5 total attempts. "
                "Honours the server's Retry-After header on 429/503."
            ),
        ),
    ] = 4,
    user_agent: Annotated[
        str | None,
        typer.Option(
            "--user-agent",
            help="Override the outbound User-Agent header.",
        ),
    ] = None,
    no_verify_ssl: Annotated[
        bool,
        typer.Option(
            "--no-verify-ssl",
            help=(
                "Disable TLS certificate verification. Useful behind corporate "
                "proxies that re-sign HTTPS traffic with an internal CA."
            ),
        ),
    ] = False,
    respect_robots: Annotated[
        bool,
        typer.Option(
            "--respect-robots/--no-respect-robots",
            help="Honour robots.txt for public hosts.",
        ),
    ] = True,
    max_redirects: Annotated[
        int,
        typer.Option("--max-redirects", help="Cap on the redirect chain length."),
    ] = 10,
    include_comments: Annotated[
        bool,
        typer.Option(
            "--include-comments/--no-include-comments",
            help="Preserve HTML comments in the extracted document.",
        ),
    ] = False,
    include_images: Annotated[
        bool,
        typer.Option(
            "--include-images/--no-include-images",
            help="Keep image syntax in the rendered Markdown.",
        ),
    ] = True,
    include_links: Annotated[
        bool,
        typer.Option(
            "--include-links/--no-include-links",
            help="Keep link URLs in the rendered Markdown.",
        ),
    ] = True,
    heading_style: Annotated[
        str,
        typer.Option(
            "--heading-style",
            help='Markdown heading style ("atx" or "setext").',
            case_sensitive=False,
        ),
    ] = "atx",
    code_fences: Annotated[
        bool,
        typer.Option(
            "--code-fences/--no-code-fences",
            help="Use fenced code blocks instead of indented ones.",
        ),
    ] = True,
    wide_tables: Annotated[
        str,
        typer.Option(
            "--wide-tables",
            help=(
                'Wide-table rendering strategy: "kv", "html", or "drop". '
                "In 'html' mode the embedded HTML is scrubbed of inline JS "
                "(event handlers, javascript: URLs); avoid feeding into "
                "non-sanitising renderers."
            ),
            case_sensitive=False,
        ),
    ] = "kv",
    no_fetched_at: Annotated[
        bool,
        typer.Option(
            "--no-fetched-at",
            help="Omit fetched_at from frontmatter (deterministic output).",
        ),
    ] = False,
    log_level: Annotated[
        str,
        typer.Option(
            "--log-level",
            help='Log verbosity: "debug", "info", "warning", or "error".',
            case_sensitive=False,
        ),
    ] = "info",
    log_json: Annotated[
        bool,
        typer.Option("--log-json", help="Emit logs as JSON lines."),
    ] = False,
    playwright_idle_ms: Annotated[
        int,
        typer.Option(
            "--playwright-idle-ms",
            help=(
                "Extra milliseconds the Playwright fetcher waits after "
                "networkidle for late-firing scripts to settle."
            ),
        ),
    ] = 500,
    debug: Annotated[
        bool,
        typer.Option(
            "--debug",
            help=(
                "Shortcut for --log-level=debug; also print tracebacks on error. "
                "Tracebacks may include local file paths and the original URL."
            ),
        ),
    ] = False,
    crawl_site: Annotated[
        bool,
        typer.Option(
            "--crawl",
            help=(
                "Crawl all linked sub-pages reachable from URL and convert each "
                "to a separate .md file. Requires -o to be a directory "
                "(not a file or stdout)."
            ),
        ),
    ] = False,
    crawl_depth: Annotated[
        int,
        typer.Option(
            "--crawl-depth",
            help=("Maximum BFS depth from the seed URL when --crawl is active. Default: 1."),
            min=0,
        ),
    ] = 1,
    retry_failed: Annotated[
        bool,
        typer.Option(
            "--retry-failed/--no-retry-failed",
            help=(
                "After --crawl finishes, automatically retry pages that failed "
                "in the initial pass (one extra attempt). Default: on."
            ),
        ),
    ] = True,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Print the version and exit.",
        ),
    ] = False,
) -> None:
    """Convert a webpage URL into clean, LLM-ready Markdown.

    Fetches the page at URL, extracts the main content, converts it to
    Markdown, and writes the result. Use '-o -' to stream the rendered
    Markdown to stdout (suitable for Unix pipelines). All human-facing
    messages go to stderr; stdout is reserved for the Markdown body and
    the --version string.
    """
    del version  # consumed by the eager --version callback; nothing to do here.

    try:
        cfg, cli_overrides = _build_config(ctx, url, debug)
        configure_logging(level=cfg.log_level, json_mode=cfg.log_json)
        _emit_env_override_log(cfg, cli_overrides)
        if crawl_site:
            _validate_crawl_output(output)
            crawl_result = crawl(cfg, max_depth=crawl_depth, retry_failed=retry_failed)
            _print_crawl_summary(crawl_result)
            return
        result = run(cfg)
    except PageToMdError as exc:
        _report_error(exc, debug=debug)
        raise typer.Exit(exc.exit_code) from exc
    except KeyboardInterrupt as exc:
        typer.echo("interrupted", err=True)
        raise typer.Exit(EXIT_INTERRUPTED) from exc

    _print_success_summary(result)


def _build_config(
    ctx: typer.Context,
    url: str,
    debug: bool,
) -> tuple[Config, dict[str, object]]:
    """Collect CLI flags into a :class:`Config`, honouring env-var precedence.

    Uses ``ctx.params`` (populated by Typer from the ``main()`` signature) and
    :data:`_OPTION_TRANSFORMS` to determine which options were explicitly
    supplied on the command line and apply any necessary value transforms.

    Only options the user actually passed on the command line are forwarded
    to :meth:`Config.from_overrides`. Anything left at its Typer default is
    omitted so ``pydantic-settings`` can still pull ``PAGETOMD_*`` env vars
    (or fall back to the Pydantic-side default) for that field.

    Args:
        ctx: The Typer context, used to inspect which params were explicitly
            set vs. left at their defaults.
        url: The positional URL argument — always included in the overrides.
        debug: Shortcut flag that forces ``log_level`` to ``"debug"`` when set.

    Returns:
        A ``(Config, cli_overrides)`` tuple. ``cli_overrides`` is the
        mapping of field names actually supplied on the command line
        (including ``"url"``).
    """
    overrides: dict[str, object] = {"url": url}

    for config_field, (cli_param, transform) in _OPTION_TRANSFORMS.items():
        source = ctx.get_parameter_source(cli_param)
        if source is not None and source.name != "DEFAULT":
            overrides[config_field] = transform(ctx.params[cli_param])

    if debug:
        overrides["log_level"] = "debug"

    return Config.from_overrides(overrides), overrides


def _emit_env_override_log(
    cfg: Config,
    cli_overrides: dict[str, object],
) -> None:
    """Log an INFO event listing Config fields sourced from env vars (not CLI).

    Field values are intentionally omitted — names only — to avoid leaking
    sensitive overrides (e.g. ``user_agent`` contents).
    """
    explicit = set(cfg.model_fields_set)
    cli_keys = set(cli_overrides.keys()) | {"url"}  # url is always supplied
    env_only = sorted(explicit - cli_keys)
    if env_only:
        get_logger("pagetomd.cli").info(
            "config.env_overrides",
            fields=env_only,
        )


def _report_error(exc: PageToMdError, *, debug: bool) -> None:
    """Print a two-line error report (plus optional traceback) to stderr."""
    typer.echo(f"error: {exc.__class__.__name__}: {exc.message}", err=True)
    typer.echo(f"hint:  {exc.hint}", err=True)
    if debug:
        traceback.print_exc(file=sys.stderr)


def _print_success_summary(result: PipelineResult) -> None:
    """Emit the one-line success summary to stderr (never stdout)."""
    target = "<stdout>" if result.output_path is None else str(result.output_path)
    typer.echo(f"✓ wrote to {target}", err=True)


def _validate_crawl_output(output: str | None) -> None:
    """Reject ``--crawl`` invocations whose ``-o`` is stdout or an existing file.

    Crawl mode always writes one ``.md`` file per page, so the destination
    must be a directory we can populate. A ``None`` output is accepted —
    it resolves to the current working directory inside :func:`crawl`.

    Raises:
        UsageError: When ``output`` is the stdout sentinel ``"-"`` or
            points to an existing non-directory path.
    """
    if output == "-":
        raise UsageError(
            "--crawl cannot be combined with -o - (stdout); "
            "crawl writes one file per page and requires a directory."
        )
    if output is not None:
        out_path = Path(output)
        if out_path.exists() and not out_path.is_dir():
            raise UsageError(f"--crawl requires -o to be a directory, but {output!r} is a file.")


def _print_crawl_summary(result: CrawlResult) -> None:
    """Emit the crawl summary to stderr."""
    typer.echo(
        f"✓ crawl complete: {result.pages_written} written, "
        f"{result.pages_skipped} skipped, {result.pages_empty} empty, "
        f"{result.pages_failed} failed "
        f"(total {result.total})",
        err=True,
    )
    if result.skipped_urls:
        typer.echo("", err=True)
        typer.echo("Skipped (file already exists — re-run with --overwrite):", err=True)
        for url in result.skipped_urls:
            typer.echo(f"  {url}", err=True)
    if result.empty_urls:
        typer.echo("", err=True)
        typer.echo(
            "Empty (no extractable content — page may require auth or is a nav stub):", err=True
        )
        for url in result.empty_urls:
            typer.echo(f"  {url}", err=True)
    if result.failed_urls:
        typer.echo("", err=True)
        typer.echo("Failed (fetch/conversion error — retry individually):", err=True)
        for url in result.failed_urls:
            typer.echo(f"  {url}", err=True)
