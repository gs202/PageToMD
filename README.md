# pagetomd

> Convert any webpage URL into clean, LLM-ready Markdown with frontmatter.

[![PyPI version](https://img.shields.io/pypi/v/pagetomd?color=%2334D058&label=pypi%20package)](https://pypi.org/project/pagetomd/)
[![Python versions](https://img.shields.io/pypi/pyversions/pagetomd?color=%2334D058)](https://pypi.org/project/pagetomd/)
[![CI](https://github.com/gs202/PageToMD/actions/workflows/ci.yml/badge.svg)](https://github.com/gs202/PageToMD/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-BUSL--1.1-blue.svg)](LICENSE)
[![Total Downloads](https://static.pepy.tech/badge/pagetomd)](https://pepy.tech/project/pagetomd)

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
# optional: enable JS rendering for SPAs
uv tool install 'pagetomd[playwright]' && playwright install chromium
```

### Without installing (uv run)

```bash
# Core — no install required
uv run --with pagetomd pagetomd https://example.com

# With Playwright for SPA / JS-heavy pages (install Chromium once first)
uv run --with playwright playwright install chromium
uv run --with 'pagetomd[playwright]' pagetomd https://example.com --fetcher auto
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

### Pipe into an LLM

`-o -` writes the Markdown to stdout. All logs go to stderr, so the stream is safe to pipe:

```bash
pagetomd https://example.com/blog/post -o - | llm "summarise this article in five bullet points"
```

### Batch-convert from a file

```bash
while read -r url; do
  pagetomd "$url"
done < urls.txt
```

Each successful conversion exits `0`; any non-zero exit leaves the loop
running but is visible in stderr (see [Exit codes](#exit-codes) below).

### Crawl an entire documentation site

Use `--crawl` to discover every linked sub-page under a seed URL and write
one `.md` file per page into an output directory:

```bash
pagetomd "https://docs.example.com/guide/" \
  --crawl --crawl-depth 2 \
  --fetcher auto --no-respect-robots \
  -o ./docs-output/
```

**Scope:** The seed is treated as the root of its own subtree. Only links
whose URL lives *under* the seed are followed; siblings, parents, and
external sites are skipped. For a seed of
`https://docs.example.com/guide/intro` the in-scope prefix is
`https://docs.example.com/guide/intro/` — pass a trailing slash on the
seed (or use a "directory" URL like `/guide/`) to scope the crawl one
level higher.

**Output structure:** The on-disk layout mirrors the URL hierarchy under
the seed, so two pages with the same final URL segment under different
parents do not collide:

| URL                                                  | Output file (relative to `-o`)     |
|------------------------------------------------------|------------------------------------|
| The seed itself                                      | `index.md`                         |
| `…/guide/intro`                                      | `intro.md`                         |
| `…/guide/intro/`                                     | `intro/index.md`                   |
| `…/guide/concepts/alerts`                            | `concepts/alerts.md`               |
| `…/guide/concepts/alerts/`                           | `concepts/alerts/index.md`         |

Each path segment is slugified independently, and Windows-reserved device
names (`CON`, `PRN`, …) are escaped per segment.

**Options:**

- `--crawl-depth N` — BFS hop limit from the seed (default: `1`).
  `--crawl-depth 10` against a site that naturally ends at depth 3 simply
  stops when the queue empties; nothing is wasted.
- `--overwrite` — replace existing `.md` files (default: skip). At the end of a crawl,
  three lists are printed to stderr: pages skipped because the file already exists,
  pages where no content could be extracted (auth walls, thin nav stubs), and pages
  that failed with a fetch or conversion error — so you can handle each category
  appropriately.
- All other flags (`--fetcher`, `--no-verify-ssl`, `--user-agent`,
  `--retries`, …) apply to every page in the crawl. `--retries` honours
  `Retry-After` headers on 429/503 responses (capped at 5 minutes per
  attempt).

A single fetcher context is reused across the whole crawl, so browser
backends do not relaunch Chromium per page.

## Choosing a mode

`pagetomd` has four ways to turn URLs into Markdown. Pick the one that matches your situation:

| I want to… | Use | Why |
|---|---|---|
| Convert a single static page (blog, docs, article) | `pagetomd URL` | Default `httpx` fetcher — fast, no extra deps. |
| Convert a page that needs JavaScript to render (React, Vue, Angular, Next.js) | `pagetomd URL --fetcher playwright` | Launches headless Chromium so the SPA actually renders. |
| Convert a page and I'm not sure if it needs JS | `pagetomd URL --fetcher auto` | Tries `httpx` first; falls back to Playwright if the page looks like an empty SPA shell or extraction comes back empty. |
| Crawl an entire site section into a folder of `.md` files | `pagetomd URL --crawl -o dir/` | BFS-walks every same-subtree link and writes one file per page. Combine with `--fetcher auto` if some pages are JS-rendered. |

### Fetcher details

**`httpx`** (default) — A plain HTTP GET. Sub-second for most pages, handles retries with exponential backoff, honours `Retry-After` on 429/503, enforces `robots.txt`, and follows `<meta http-equiv="refresh">` redirects. No JavaScript execution — if the server sends an empty `<div id="root"></div>` shell, that's all you get.

**`playwright`** — Renders the page in headless Chromium, waits for network idle, then serialises the live DOM (including shadow roots). Use this when you _know_ the page is a SPA. Requires the optional `playwright` extra (`pip install 'pagetomd[playwright]'`) and a one-time `playwright install chromium`. Slower and heavier than `httpx`, but the only way to get content that lives behind a JS framework.

**`auto`** — Fetches with `httpx` first, then inspects the result: if the `<body>` text is under 200 characters _and_ the HTML contains SPA markers (`data-reactroot`, `<div id="__next">`, a "you need to enable javascript" noscript tag, etc.), it re-fetches with Playwright. A second safety net fires if `httpx` returned HTML that _looked_ non-empty but the extractor still couldn't pull any content — Playwright gets a shot then too. If Playwright is unavailable, the page is counted as "empty" in the crawl summary rather than a hard failure. Best choice when you're pointed at an unfamiliar URL.

### Single page vs. crawl

Use the **default single-page mode** when you have a specific URL (or a short list piped through a `while read` loop). Use **`--crawl`** when you want every page under a URL prefix — it discovers links automatically, deduplicates, mirrors the URL hierarchy on disk, and reuses a single fetcher context so Playwright doesn't relaunch Chromium per page. See the [crawl cookbook recipe](#crawl-an-entire-documentation-site) for the full flag set.

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
tool_version: 0.4.0
---

# Why We Rewrote Our Build System in Rust

Three years ago, our monorepo build pipeline was a sprawling Python application held together with shell scripts and prayer. ...
```

When `fetched_at` is enabled (the default), an extra `fetched_at: '2026-06-15T12:34:56Z'` line is included in the frontmatter. Fields whose value cannot be detected (e.g. `language`, `author`) are omitted from the YAML.

Two field pairs look similar but mean different things:

- `url` is the URL you requested; `final_url` is where you landed after any redirects (they match when there's no redirect).
- `date` is the content's own publication date (from the page metadata); `fetched_at` is when pagetomd retrieved the page.

## Common options

A compact overview — see `pagetomd --help` for the full list.

| Flag | Default | Description |
| ---- | ------- | ----------- |
| `--output / -o` | _derived from title_ | Output path, or `-` for stdout. |
| `--overwrite` | `false` | Replace an existing destination file. |
| `--follow-symlinks / --no-follow-symlinks` | `false` | Allow writes to a symlinked destination. Off by default so `--overwrite` cannot be tricked into clobbering a file outside the intended directory via a symlink. |
| `--fetcher` | `httpx` | `httpx`, `playwright`, or `auto`. |
| `--timeout` | `30.0` | Per-request HTTP timeout (seconds). |
| `--retries` | `4` | Per-page retry attempts on transient failures (default 4 = up to 5 total attempts). Honours the server's `Retry-After` header on 429/503 responses, capped at 5 minutes; falls back to exponential backoff otherwise. |
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
| `--crawl` | `false` | Crawl all linked sub-pages under the seed URL's path prefix and write one `.md` file per page. Requires `-o` to be a directory. |
| `--crawl-depth` | `1` | Maximum BFS depth from the seed URL when `--crawl` is active. `0` = seed only. |
| `--retry-failed` / `--no-retry-failed` | `true` | After `--crawl` finishes, retry pages that failed in the initial pass once. |
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
git clone https://github.com/gs202/PageToMD.git
cd pagetomd
uv sync --extra dev --extra playwright
pre-commit install
uv run pytest
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full contributor workflow.

## License

Business Source License 1.1 — source-available, free for non-commercial use. Converts to MIT on 2030-06-16. See [LICENSE](LICENSE) for full terms.
