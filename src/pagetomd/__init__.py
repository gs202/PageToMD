"""pagetomd — convert any webpage URL to clean, LLM-ready Markdown."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("pagetomd")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0+unknown"

from pagetomd.crawler import CrawlResult, crawl

__all__ = ["CrawlResult", "__version__", "crawl"]
