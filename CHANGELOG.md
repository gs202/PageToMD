# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

_Nothing yet._

## [0.4.0] - 2026-06-18

### Security

- **Decompression-bomb DoS closed** (`fetcher.py`) — response body is now read with
  `client.stream()` + `iter_bytes()` and the size cap fires mid-stream, before the full
  decompressed body lands in memory. Gzip bombs and other compressed payloads can no longer
  OOM the process before the cap triggers. Removes `_enforce_body_size_limit`.
- **SSRF bypass made test-only** (`ssrf.py`) — `PAGETOMD_INTERNAL_SKIP_SSRF` is no longer
  honoured in production. The bypass now requires both an in-process `_BYPASS: bool` flag
  set via `monkeypatch.setattr` (for unit tests) **and** the env var double-gated on
  `PYTEST_CURRENT_TEST` (for subprocess-based integration tests). The bypass is physically
  unreachable in any process pytest did not launch.
- **Out-of-scope crawl URLs rejected** (`crawler.py`) — `relative_path_from_url` now raises
  `WriteError` instead of silently mapping URLs that fall outside the seed subtree. A hostile
  site can no longer shape the output tree via cross-scope links. Added `Path.is_relative_to`
  guard in `_drain_queue` as defence-in-depth.

### Fixed

- **`_atomic_write` parent-directory fsync** (`writer.py`) — `os.fsync` is now called on the
  parent directory file descriptor after `os.replace`, closing the crash-consistency hole where
  a power loss between the rename and a later kernel sync could leave directory metadata
  inconsistent. No-op on Windows (`O_DIRECTORY` guard).
- **`crawl.page.error` log carries full root cause** (`crawler.py`) — the structured log event
  now includes `error_class`, `root_cause` (the `__cause__` chain), `exit_code`, and
  `exc_info=True`. All five crawler log call sites now pass URLs through `redact_url`.
- **Pipeline unexpected-error log** (`pipeline.py`) — the `except Exception` catch now emits
  `pipeline.unexpected_error` with `error_class` and `exc_info=True` before re-raising, giving
  operators a breadcrumb instead of a silent exit 1.
- **Dead `bound` logger parameter removed** (`fetcher.py`) — the `bound: object` scaffold
  parameter was threaded through four private helper signatures but never used. Removed from
  `_fetch_with_meta_refresh`, `_parse_url`, `_check_robots`, and `_do_get` and all nine call
  sites.
- **SPA-detection regression closed during the same release** (`pipeline.py`) — the
  regex-based body-text measurement (see the performance entry below) initially did not
  strip `<script>`/`<style>` content before counting characters. Inline JSON state blobs
  and CSS could inflate the count above the 200-char threshold, suppressing the Playwright
  fallback on pages that genuinely needed it. Fixed by applying a `<script>/<style>`
  content strip before measuring.
- **`ExtractionEmptyError` in crawl mode no longer logs as an error with a stack trace**
  (`crawler.py`) — pages that produce no extractable content were previously logged at
  `error` level with `exc_info=True` and counted as failures. They are now logged at
  `warning` as `crawl.page.empty` with no traceback, and counted as a distinct "empty"
  category rather than a failure (see also the `empty_urls` change below).
- **Preclean over-firing on portal pages** (`extractor.py`) — when `_preclean`'s
  junk-pattern remover decomposed an element whose class/id matched a portal UI term (e.g.
  `feedback`, `component-loader`) that happened to be the main content container,
  trafilatura received an empty document and raised `ExtractionEmptyError` even though the
  page had real content. A fallback pass now retries trafilatura with a minimal strip
  (only `_ALWAYS_DROP_TAGS` removed, no junk-pattern matching) before giving up. SPA
  shells still correctly produce `ExtractionEmptyError` because the minimal strip removes
  `<script>`/`<noscript>` content.

### Performance

- **SPA-detection no longer parses HTML** (`pipeline.py`) — `_should_fallback_to_playwright`
  now uses a regex tag-strip over `html[:50_000]` instead of a full BeautifulSoup/lxml parse,
  saving ~30-100 ms per page in crawl+auto mode.
- **`_extract_base_href` no longer parses HTML** (`extractor.py`) — replaced with a single
  `re.search` for the `<base href>` attribute.
- **`PlaywrightFetcher` reuses one httpx.Client for robots checks** (`fetcher.py`) — entering
  the `HttpxFetcher` delegate in `PlaywrightFetcher.__enter__` means robots checks share a
  persistent connection pool across all pages instead of paying a TLS handshake per page in
  crawl+Playwright mode.

### Changed

- **CLI option consolidation** (`cli.py`) — the four parallel structures (22-param `main()`
  signature, mirrored `_build_config()` signature, `values` dict, and `_CLI_OVERRIDE_NAMES`
  tuple) are reduced to two: `main()` signature + `_OPTION_TRANSFORMS` dict. Adding a new CLI
  flag now requires edits in exactly two places.
- **Private API imports eliminated** (`cli.py`, `converter.py`) — `typer._click.core.ParameterSource`
  replaced with a `.name` string comparison (no import needed); `markdownify.chomp` replaced
  with a vendored `_chomp()` helper, removing the `# type: ignore[attr-defined]` admission.
- **Crawl summary distinguishes three skip categories** (`crawler.py`, `cli.py`) —
  `CrawlResult` gains an `empty_urls` list for pages with no extractable content, separate
  from `skipped_urls` (file already exists) and `failed_urls` (fetch/conversion error). The
  CLI summary and `crawl.done` structured log event reflect all three counts and print each
  list with an accurate label.

## [0.3.0] - 2026-06-18

### Added

- **Auto-retry failed crawl pages (`--retry-failed`)** — after a `--crawl` run, pages that failed (fetch or conversion error) are automatically retried once with a fresh fetcher context. Successes are removed from the failed list; persistent failures remain. Disable with `--no-retry-failed`.

## [0.2.0] - 2026-06-17

### Added

- **Site crawl (`--crawl`)** — BFS-crawl every same-subtree link under a seed URL and write one `.md` file per page into a directory that mirrors the URL hierarchy. Configurable via `--crawl-depth N` (default 1) and `--overwrite`. A single fetcher context is reused across the whole crawl, so Playwright doesn't relaunch Chromium per page.
- **Shadow DOM / FluidTopics support** — the Playwright fetcher now serialises shadow roots recursively, capturing content inside Web Components that the static DOM misses entirely.
- **"Choosing a mode" README section** — new decision table and prose explaining when to use `httpx`, `playwright`, `auto`, and `--crawl`.
- **`uv run` usage** — README now documents how to run `pagetomd` without installing via `uv run --with pagetomd`.
- **`pytest-xdist`** — parallel test execution via `-n auto --dist=loadscope`.

### Changed

- **Python 3.12+** — minimum supported version lowered from 3.13 to 3.12.
- **Exponential backoff ceiling** — raised from 8 s to 60 s so rate-limited sites (429/503) get longer breathing room between retries.
- **CI** — all jobs now use `astral-sh/setup-uv` with `python-version` input directly, removing the separate `actions/setup-python` step.
- **Dependencies** — bumped `markdownify` to 1.x and updated the converter for its new API; bumped GitHub Actions to latest.

### Fixed

- **Shadow-DOM serializer** — `<meta>` `name` and `content` attributes are now preserved during serialisation (previously dropped).
- **Converter** — updated for `markdownify` 1.x breaking changes; fixed mypy overrides; regenerated snapshots.

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
- **GitHub Actions CI** — lint, type-check, and test matrix across Python 3.12; project-wide 85% coverage floor and 90% per-module floor on critical modules.
- **GitHub Actions release workflow** — builds sdist + wheel, publishes to PyPI via Trusted Publishing (OIDC), and creates a GitHub Release with changelog body.
- **Test suites** — unit, integration (e2e httpx/playwright, determinism, packaging), property-based (`hypothesis`), and snapshot tests with 8 HTML fixture pages.

[Unreleased]: https://github.com/gs202/PageToMD/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/gs202/PageToMD/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/gs202/PageToMD/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/gs202/PageToMD/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/gs202/PageToMD/releases/tag/v0.1.0
