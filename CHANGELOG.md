# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

_Nothing yet._

## [0.1.0] - 2026-06-16

### Added

- **Core pipeline** — fetch → extract → convert → postprocess → write, converting any public URL to clean, LLM-ready Markdown with YAML frontmatter.
- **Dual fetcher** — `httpx` (default, sub-second) and `playwright` (opt-in headless Chromium for SPAs), selectable via `--fetcher httpx|playwright|auto`.
- **Content extraction** — BeautifulSoup pre-clean pass (strips scripts, styles, nav, ads) followed by `trafilatura` for main-content identification and metadata harvesting.
- **Markdown conversion** — customised `markdownify` subclass with ATX headings, fenced code blocks with language hints, and GFM tables with wide-table fallback strategies (`kv`, `html`, `drop`).
- **Postprocessing** — NFC normalisation, zero-width character stripping, monotonic heading hierarchy enforcement, and absolute URL resolution.
- **YAML frontmatter** — `url`, `final_url`, `title`, `author`, `date`, `description`, `site_name`, `language`, `fetched_at`, `tool`, `tool_version` (empty fields omitted).
- **Atomic file writes** — write-to-temp then rename, with `--overwrite` and `--follow-symlinks` safety controls.
- **SSRF protection** — blocks private, loopback, link-local, multicast, reserved, and cloud-metadata addresses with no override flag.
- **`robots.txt` enforcement** — enabled by default, relaxed for loopback/RFC 1918, opt-out via `--no-respect-robots`.
- **Typer CLI** — full `PAGETOMD_*` env-var precedence, stable exit codes (`0`/`1`/`2`/`3`/`4`/`5`/`64`/`130`), structured JSON logging (`--log-json`), and `--no-fetched-at` for byte-deterministic output.
- **Output controls** — `--include-images`, `--include-links`, `--include-comments`, `--code-fences`, `--heading-style`, `--wide-tables`.
- **GitHub Actions CI** — lint, type-check, and test matrix across Python 3.11–3.13; project-wide 85% coverage floor and 90% per-module floor on critical modules.
- **GitHub Actions release workflow** — builds sdist + wheel, publishes to PyPI via Trusted Publishing (OIDC), and creates a GitHub Release with changelog body.
- **Test suites** — unit, integration (e2e httpx/playwright, determinism, packaging), property-based (`hypothesis`), and snapshot tests with 8 HTML fixture pages.

[Unreleased]: https://github.com/gs202/pagetomd/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/gs202/pagetomd/releases/tag/v0.1.0
