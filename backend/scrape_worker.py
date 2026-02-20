"""
Standalone scraper entry point for Windows. Run with:
  python scrape_worker.py <url> <output_json_path>

Sets the Windows event loop policy before any Playwright/asyncio use, then scrapes
the URL and writes {text, title, links} as JSON. Used by ingestion when on Windows
to avoid NotImplementedError from running Playwright in a background thread.
"""
import asyncio
import json
import logging
import sys

# Configure logging FIRST (before any imports that use logging)
logging.basicConfig(
    level=logging.INFO,
    format="[scrape_worker] %(levelname)s %(name)s %(message)s",
    stream=sys.stderr,
    force=True,  # Override any existing config
)
logger = logging.getLogger(__name__)

if sys.platform == "win32":
    # Use the Proactor loop on Windows to ensure subprocess support works
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

# Import after policy is set so Playwright gets the correct loop
from ingestion import scrape_url


def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: python scrape_worker.py <url> <output_json_path>", file=sys.stderr)
        sys.exit(1)
    url = sys.argv[1]
    out_path = sys.argv[2]
    logger.info("scrape_worker start url=%s", url)
    try:
        text, title, links = scrape_url(url, status_store=None, status_url=None)
        logger.info("scrape_worker done url=%s text_len=%s title=%s links_count=%s", url, len(text) if text else 0, title, len(links) if links else 0)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"text": text, "title": title, "links": links}, f, ensure_ascii=False)
    except Exception as e:
        logger.error("scrape_worker error url=%s error=%s", url, e, exc_info=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"error": str(e), "text": "", "title": "", "links": []}, f, ensure_ascii=False)
        sys.exit(1)


if __name__ == "__main__":
    main()
