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


# Names of options whose presence on the command line should override an env
# var. Kept as a module-level tuple so the implementation cannot drift from
# the option declarations below.
_CLI_OVERRIDE_NAMES: tuple[str, ...] = (
    "output",
    "overwrite",
    "follow_symlinks",
    "fetcher",
    "timeout",
    "retries",
    "user_agent",
    "respect_robots",
    "max_redirects",
    "include_comments",
    "include_images",
    "include_links",
    "heading_style",
    "code_fences",
    "wide_tables",
    "no_fetched_at",
    "log_level",
    "log_json",
    "playwright_idle_ms",
)


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
        cfg, cli_overrides = _build_config(
            ctx,
            url=url,
            output=output,
            overwrite=overwrite,
            follow_symlinks=follow_symlinks,
            fetcher=fetcher,
            timeout=timeout,
            retries=retries,
            user_agent=user_agent,
            no_verify_ssl=no_verify_ssl,
            respect_robots=respect_robots,
            max_redirects=max_redirects,
            include_comments=include_comments,
            include_images=include_images,
            include_links=include_links,
            heading_style=heading_style,
            code_fences=code_fences,
            wide_tables=wide_tables,
            no_fetched_at=no_fetched_at,
            log_level=log_level,
            log_json=log_json,
            playwright_idle_ms=playwright_idle_ms,
            debug=debug,
        )
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
    *,
    url: str,
    output: str | None,
    overwrite: bool,
    follow_symlinks: bool,
    fetcher: str,
    timeout: float,
    retries: int,
    user_agent: str | None,
    no_verify_ssl: bool,
    respect_robots: bool,
    max_redirects: int,
    include_comments: bool,
    include_images: bool,
    include_links: bool,
    heading_style: str,
    code_fences: bool,
    wide_tables: str,
    no_fetched_at: bool,
    log_level: str,
    log_json: bool,
    playwright_idle_ms: int,
    debug: bool,
) -> tuple[Config, dict[str, object]]:
    """Collect CLI flags into a :class:`Config`, honouring env-var precedence.

    Only options the user actually passed on the command line are forwarded
    to :meth:`Config.from_overrides`. Anything left at its Typer default is
    omitted so ``pydantic-settings`` can still pull ``PAGETOMD_*`` env vars
    (or fall back to the Pydantic-side default) for that field.

    Returns:
        A ``(Config, cli_overrides)`` tuple. ``cli_overrides`` is the
        mapping of field names actually supplied on the command line
        (including ``"url"``).
    """
    values: dict[str, object] = {
        "output": _coerce_output(output),
        "overwrite": overwrite,
        "follow_symlinks": follow_symlinks,
        "fetcher": fetcher,
        "timeout": timeout,
        "retries": retries,
        "user_agent": user_agent,
        "verify_ssl": not no_verify_ssl,
        "respect_robots": respect_robots,
        "max_redirects": max_redirects,
        "include_comments": include_comments,
        "include_images": include_images,
        "include_links": include_links,
        "heading_style": heading_style,
        "code_fences": code_fences,
        "wide_tables": wide_tables,
        "no_fetched_at": no_fetched_at,
        "log_level": log_level,
        "log_json": log_json,
        "playwright_idle_ms": playwright_idle_ms,
    }

    overrides: dict[str, object] = {"url": url}
    for name in _CLI_OVERRIDE_NAMES:
        source = ctx.get_parameter_source(name)
        if source is not None and source.name != "DEFAULT":
            overrides[name] = values[name]

    if debug:
        overrides["log_level"] = "debug"

    if no_verify_ssl:
        overrides["verify_ssl"] = False

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


def _coerce_output(output: str | None) -> Path | None:
    """Translate the raw ``--output`` string into a :class:`Path` (or ``None``)."""
    if output is None:
        return None
    return Path(output)


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
        f"{result.pages_skipped} skipped, {result.pages_failed} failed "
        f"(total {result.total})",
        err=True,
    )
    if result.skipped_urls:
        typer.echo("", err=True)
        typer.echo("Skipped (file already exists — re-run with --overwrite):", err=True)
        for url in result.skipped_urls:
            typer.echo(f"  {url}", err=True)
    if result.failed_urls:
        typer.echo("", err=True)
        typer.echo("Failed (fetch/conversion error — retry individually):", err=True)
        for url in result.failed_urls:
            typer.echo(f"  {url}", err=True)
