# pagetomd

> Convert any webpage URL into clean, LLM-ready Markdown with frontmatter.

[![PyPI version](https://img.shields.io/pypi/v/pagetomd?color=%2334D058&label=pypi%20package)](https://pypi.org/project/pagetomd/)
[![Python versions](https://img.shields.io/pypi/pyversions/pagetomd?color=%2334D058)](https://pypi.org/project/pagetomd/)
[![CI](https://github.com/gs202/pagetomd/actions/workflows/ci.yml/badge.svg)](https://github.com/gs202/pagetomd/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-BUSL--1.1-blue.svg)](LICENSE)
[![Total Downloads](https://img.shields.io/pepy/dt/pagetomd?color=%2334D058)](https://pepy.tech/project/pagetomd)

## Why

- **AI-ready by default.** Output is NFC-normalised UTF-8, single H1, monotonic heading hierarchy, no zero-width junk, no tracking chrome — drops straight into a vector store or LLM prompt.
- **Full-fidelity metadata.** Every file ships with a YAML frontmatter block containing canonical URL, final URL after redirects, title, author, date, description, site name, language, and tool identity. No more "where did this Markdown come from?".
- **Static fast, JS-capable when needed.** Default `httpx` fetcher is sub-second; opt-in `playwright` extra (or `--fetcher auto`) handles SPA shells without bloating the install for everyone else.
- **Stable, scriptable CLI.** Typer-built, full env-var precedence (`PAGETOMD_*`), stable exit codes (`0`/`2`/`3`/`4`/`5`/`64`/`130`), structured logs (`--log-json`), and a `--no-fetched-at` switch for byte-deterministic output.
- **Not `pandoc` or `curl + sed`.** `pandoc` doesn't fetch, doesn't strip boilerplate, and doesn't emit frontmatter. Hand-rolled `curl | html2md` pipelines re-invent extraction, mojibake handling, robots.txt, redirect caps, and atomic writes. `pagetomd` is one command for the whole pipeline.

## Install

### With pipx (recommended for CLI use)

```bash
pipx install pagetomd
# optional: enable JS rendering for SPAs
pipx inject pagetomd playwright && playwright install chromium
```

### With uv

```bash
uv tool install pagetomd
```

### With pip

```bash
pip install pagetomd                 # core
pip install 'pagetomd[playwright]'   # + SPA support
```

## Quick start

```bash
# Default: derives output filename from the page title
pagetomd https://example.com/blog/post

# Stream to stdout (pipe into LLMs, etc.)
pagetomd https://example.com/blog/post -o -

# Deterministic output (omits fetched_at — good for snapshot tests / RAG ingestion)
pagetomd https://example.com/blog/post --no-fetched-at -o post.md

# Auto-detect SPA pages and fall back to headless Chromium
pagetomd https://my-spa.example.com -o - --fetcher auto
```

## Cookbook

### Convert a blog post and save to a slugged file

When you omit `--output / -o`, `pagetomd` derives a filename from the page
title (slugified, lowercase, hyphen-separated):

```bash
pagetomd https://example.com/blog/post
# → ./why-we-rewrote-our-build-system-in-rust.md
```

### Stream to stdout and pipe into an LLM

`-o -` writes the rendered Markdown body (with frontmatter) to stdout. All
logs go to stderr, so the stream is safe to pipe:

```bash
pagetomd https://example.com/blog/post -o - | llm "summarise this article in five bullet points"
```

### Convert many URLs from a text file

```bash
while read -r url; do
  pagetomd "$url"
done < urls.txt
```

Each successful conversion exits `0`; any non-zero exit leaves the loop
running but is visible in stderr (see [Exit codes](#exit-codes) below).

### Force deterministic output for snapshot testing

`--no-fetched-at` omits the `fetched_at` line from the frontmatter, making
the output byte-identical across runs:

```bash
pagetomd https://example.com/blog/post --no-fetched-at -o post.md
```

### Strip images for token-budget reasons

```bash
pagetomd https://example.com/blog/post --no-include-images -o post.md
```

Pair with `--no-include-links` to strip URLs too:

```bash
pagetomd https://example.com/blog/post --no-include-images --no-include-links -o post.md
```

### Convert an SPA with Playwright auto-fallback

The `auto` fetcher tries `httpx` first and falls back to headless Chromium
only when the response looks like a JavaScript shell:

```bash
pip install 'pagetomd[playwright]'
playwright install chromium
pagetomd https://my-spa.example.com -o - --fetcher auto
```

### Use a custom User-Agent

Some CDNs reject the default `pagetomd/<ver>` UA:

```bash
pagetomd https://example.com/blog/post \
  --user-agent "Mozilla/5.0 (compatible; MyCrawler/1.0; +https://example.org/bot)" \
  -o post.md
```

### Bypass robots.txt (use responsibly)

```bash
pagetomd https://example.com/private-but-mine --no-respect-robots -o out.md
```

### Skip TLS verification (corporate proxies)

If your network intercepts HTTPS traffic with an internal CA (common on corporate VPNs), `pagetomd` will fail with an `SSL: CERTIFICATE_VERIFY_FAILED` error. Pass `--no-verify-ssl` to disable certificate checks:

```bash
pagetomd https://example.com/blog/post --no-verify-ssl -o post.md
```

SSL errors are detected and fail immediately without retrying, so the error message will also suggest this flag.

### Override config via env var

```bash
PAGETOMD_TIMEOUT=60 pagetomd https://slow-but-real.example.com -o out.md
```

### JSON logs for observability tooling

```bash
pagetomd https://example.com/blog/post --log-json -o post.md 2> events.jsonl
jq -r 'select(.level == "warning") | .event' events.jsonl
```

## Output shape

Running `pagetomd http://127.0.0.1:8765/blog.html --no-fetched-at -o -` against the `blog.html` fixture prints (first ~15 lines shown):

```markdown
---
url: http://127.0.0.1:8765/blog.html
final_url: http://127.0.0.1:8765/blog.html
title: Why We Rewrote Our Build System in Rust
author: Jane Doe
date: '2024-08-14'
description: A retrospective on migrating our monorepo build pipeline from Python to Rust, and what we learned along the way.
site_name: Example Engineering Blog
language: en
tool: pagetomd
tool_version: 0.1.0
---

# Why We Rewrote Our Build System in Rust

Three years ago, our monorepo build pipeline was a sprawling Python application held together with shell scripts and prayer. ...
```

When `fetched_at` is enabled (the default), an extra `fetched_at: '2026-06-15T12:34:56Z'` line is included in the frontmatter. Fields whose value cannot be detected (e.g. `language`, `author`) are omitted from the YAML.

## Common options

A compact overview — see `pagetomd --help` for the full list.

| Flag | Default | Description |
| ---- | ------- | ----------- |
| `--output / -o` | _derived from title_ | Output path, or `-` for stdout. |
| `--overwrite` | `false` | Replace an existing destination file. |
| `--follow-symlinks / --no-follow-symlinks` | `false` | Allow writes to a symlinked destination. Off by default so `--overwrite` cannot be tricked into clobbering a file outside the intended directory via a symlink. |
| `--fetcher` | `httpx` | `httpx`, `playwright`, or `auto`. |
| `--timeout` | `30.0` | Per-request HTTP timeout (seconds). |
| `--retries` | `3` | Retry attempts on transient failures. |
| `--user-agent` | `pagetomd/<ver>` | Override the outbound `User-Agent`. |
| `--no-verify-ssl` | `false` | Disable TLS certificate verification (for corporate proxies that re-sign HTTPS). |
| `--respect-robots / --no-respect-robots` | `true` | Honour `robots.txt` (relaxed for loopback/RFC 1918). |
| `--max-redirects` | `10` | Cap on the redirect chain length. |
| `--include-comments / --no-include-comments` | `false` | Preserve HTML comments in the extracted document. |
| `--include-images / --no-include-images` | `true` | Keep image syntax in output. |
| `--include-links / --no-include-links` | `true` | Keep link URLs in output. |
| `--heading-style` | `atx` | `atx` (`#`) or `setext` (`===`). |
| `--code-fences / --no-code-fences` | `true` | Use fenced code blocks instead of indented ones. |
| `--wide-tables` | `kv` | Wide-table strategy: `kv`, `html`, or `drop`. |
| `--no-fetched-at` | `false` | Omit `fetched_at` for byte-deterministic output. |
| `--log-level` | `info` | `debug`, `info`, `warning`, `error`. |
| `--log-json` | `false` | Emit logs as JSON lines on stderr. |
| `--debug` | `false` | Shortcut for `--log-level=debug` + tracebacks on error. |
| `--playwright-idle-ms` | `500` | Extra wait (ms) after networkidle for late-firing scripts (Playwright fetcher only). |
| `--version` | — | Print the installed version and exit. |

## Environment variables

Every flag has a `PAGETOMD_<UPPER_NAME>` equivalent. For example:

```bash
PAGETOMD_TIMEOUT=60 PAGETOMD_FETCHER=auto pagetomd https://example.com
```

CLI flags always override env vars; env vars override the built-in defaults.

## Exit codes

| Code | Meaning |
| ---- | ------- |
| `0` | Success. |
| `1` | Unexpected internal error. |
| `2` | Fetch failure (DNS, HTTP, robots.txt, redirect cap). |
| `3` | Extraction or conversion failure (empty body, malformed HTML). |
| `4` | Output write failure (permissions, disk, atomic-rename clash). |
| `5` | Missing optional dependency (e.g. `playwright` not installed). |
| `64` | Usage or configuration error (bad flag, invalid value). |
| `130` | Interrupted by user (Ctrl-C). |

## How it works

One paragraph plus a diagram of the pipeline:

```text
URL ──► Fetcher ──► Extractor ──► Converter ──► Postprocess ──► Writer
       (httpx /     (BS4 clean    (markdownify    (NFC, heading   (atomic
        playwright)  + trafilatura) + GFM tables)  hierarchy,      file +
                                                  URL absolutise)  YAML)
```

The fetcher (`httpx` by default, `playwright` for SPAs) downloads the page with retries and robots.txt enforcement. The extractor runs a BeautifulSoup pre-clean pass (strip scripts/styles/nav/ads) then hands the cleaned tree to `trafilatura` to identify main content and harvest metadata. The converter renders the surviving HTML to Markdown via a customised `markdownify` subclass (ATX headings, fenced code blocks with language hints, GFM tables with wide-table fallbacks). The postprocessor enforces the AI-readiness contract (NFC, zero-width strip, monotonic heading hierarchy, absolute URLs). The writer prepends a YAML frontmatter block and writes atomically (or streams to stdout).

## Security

`pagetomd` is a **public-URL-only** tool. It refuses to fetch private, loopback, link-local, multicast, reserved, or cloud-metadata addresses by default — and there is no flag to override that. Treat output files as having the same sensitivity as the URL they were fetched from.

## Quality gates

CI enforces both a project-wide test coverage floor of **85%** and a per-module floor of **90% (line + branch combined)** on the four critical modules — [`extractor`](src/pagetomd/extractor.py), [`converter`](src/pagetomd/converter.py), [`writer`](src/pagetomd/writer.py), and [`postprocess`](src/pagetomd/postprocess.py). These four carry the AI-readiness contract, so they get the strictest coverage bar.

## Contributing

```bash
git clone https://github.com/gs202/pagetomd.git
cd pagetomd
uv sync --extra dev --extra playwright
pre-commit install
uv run pytest
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full contributor workflow.

## License

Business Source License 1.1 — free for non-commercial use. Converts to MIT on 2030-06-16. See [LICENSE](LICENSE) for full terms.
