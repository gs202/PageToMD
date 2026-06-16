# Contributing to `pagetomd`

Thanks for considering a contribution. This guide covers the minimum a new
contributor needs to be productive.

This project follows the [Code of Conduct](CODE_OF_CONDUCT.md).

## Local setup

```bash
git clone https://github.com/gs202/pagetomd.git
cd pagetomd
uv sync --extra dev --extra playwright
pre-commit install
```

The `playwright` extra is optional — only required if you want to run or
modify the SPA-fallback path. Skip it with `uv sync --extra dev` if you
don't need it.

After installing the Playwright Python package, fetch the headless
Chromium browser binary once:

```bash
uv run playwright install chromium
```

## Run the tests

The default invocation runs every suite except the Playwright-marked tests:

```bash
uv run pytest
```

Run the Playwright suite explicitly (requires the extra above):

```bash
uv run pytest -m playwright -o asyncio_mode=strict
```

Run only the Hypothesis property-based suite:

```bash
uv run pytest -m property
```

CI runs the full matrix on every push — see
[`.github/workflows/ci.yml`](.github/workflows/ci.yml) for the exact jobs.

## Code style

Style is enforced by `pre-commit` (`ruff format`, `ruff check`, `mypy
--strict`) on every commit. To run it manually:

```bash
uv run pre-commit run --all-files
```

Mypy runs in strict mode against `src/pagetomd`. New code must type-check
clean.

## Conventional Commits

Commit messages follow the
[Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/) spec
(`feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, `test:`, …). One-line
subjects are fine for small changes; multi-paragraph bodies are welcome
when context helps a reviewer.

## Adding a fixture or snapshot

1. Drop the new HTML file under
   [`tests/fixtures/pages/`](tests/fixtures/pages/). Keep it self-contained
   (no external CDN references) and small (ideally <50 KB).
2. Add a snapshot test to
   [`tests/snapshot/test_pipeline_snapshots.py`](tests/snapshot/test_pipeline_snapshots.py)
   referencing the fixture.
3. Run `uv run pytest --snapshot-update` once locally to materialise the
   `.ambr` snapshot, then `uv run pytest` to confirm the snapshot is
   stable.
4. Add a corresponding entry to
   [`scripts/render_fixtures.py`](scripts/render_fixtures.py) if the
   fixture should also flow through the markdownlint contract check in
   CI.

## Reporting issues

Open an issue on
<https://github.com/gs202/pagetomd/issues> with:

- The URL (or a minimal reproducing fixture) and the exact CLI command.
- The full stderr output (re-run with `--debug` if it's not obviously a
  user error).
- Your `pagetomd --version` and `python --version`.
