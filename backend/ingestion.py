import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import uuid
from html import unescape
from urllib.parse import urljoin, urlparse

import time

logger = logging.getLogger(__name__)
from bs4 import BeautifulSoup
import tiktoken
from openai import OpenAI
from playwright.sync_api import sync_playwright
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from config import settings

# ============== PLAYWRIGHT / SCRAPING CONFIG ==============
# Summary of what we use with Playwright:
#   - browser: chromium, headless=True
#   - page.goto(url, wait_until="networkidle" then fallback "domcontentloaded"/"load", timeout=SCRAPE_TIMEOUT_MS)
#   - page.wait_for_load_state("networkidle") after load and after scrolling
#   - viewport: 1920x1080, locale en-IN, timezone Asia/Kolkata
#   - routes: block image/font/media; allow script/document/xhr/fetch
#   - then: POST_LOAD_WAIT_MS, content-length polling, accordion clicks, SCROLL_STEPS + scroll-to-bottom, then extract body.innerText / main content / TreeWalker
#
# Timeouts (all in seconds unless noted):
SCRAPE_OVERALL_TIMEOUT_SEC = 180   # whole scrape (subprocess or thread join)
SCRAPE_TIMEOUT_MS = 90000          # page.goto() timeout (ms)
POST_LOAD_WAIT_MS = 30000          # wait after load before interacting (ms)
WAIT_FOR_CONTENT_MS = 45000        # max wait for substantial body text (ms)
ACCORDION_CLICK_DELAY_MS = 300     # delay after clicking accordions (ms)
SCROLL_STEPS = 20                  # number of scroll steps (mouse wheel)
SCROLL_STEP_DELAY_MS = 1000        # delay between scroll steps (ms)
MIN_CONTENT_LEN = 30               # min chars to consider page valid
#
# Always use Playwright for all URLs (no simple HTTP fallback)

# Real-looking browser (Chrome 121) – headless default UA is a bot fingerprint
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)
EXTRA_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Ch-Ua": '"Google Chrome";v="144", "Chromium";v="144", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


def get_qdrant():
    return QdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
    )


def ensure_collection(client: QdrantClient):
    collections = client.get_collections().collections
    if not any(c.name == settings.collection_name for c in collections):
        client.create_collection(
            collection_name=settings.collection_name,
            vectors_config=VectorParams(size=1536, distance=Distance.COSINE),
        )


def clear_collection():
    """Delete all data in the Qdrant collection and recreate it empty."""
    client = get_qdrant()
    try:
        client.delete_collection(settings.collection_name)
    except Exception:
        pass
    ensure_collection(client)


def get_sources_from_qdrant() -> dict[str, int]:
    """Return map of source_url -> chunk count from Qdrant (so UI shows indexed data after backend restart)."""
    out: dict[str, int] = {}
    try:
        client = get_qdrant()
        offset = None
        while True:
            points, offset = client.scroll(
                collection_name=settings.collection_name,
                limit=100,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for p in points:
                url = (p.payload or {}).get("metadata", {}).get("source_url") or ""
                if url:
                    out[url] = out.get(url, 0) + 1
            if offset is None:
                break
    except Exception:
        pass
    return out


def _same_domain(url: str, base_parsed) -> bool:
    try:
        p = urlparse(url)
        return p.netloc == base_parsed.netloc and p.scheme in ("http", "https")
    except Exception:
        return False


def _extract_text(soup: BeautifulSoup) -> str:
    for tag in soup.find_all(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return unescape(text).strip()


def _html_to_markdown(html: str, base_url: str = "") -> str:
    """Convert HTML to markdown for clean, structure-preserving text extraction."""
    h = html2text.HTML2Text()
    h.ignore_links = False
    h.ignore_images = True
    h.ignore_emphasis = False
    h.body_width = 0
    h.skip_internal_links = False
    if base_url:
        h.baseurl = base_url
    text = h.handle(html)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_from_html(html: str, base_url: str, page_title: str = "") -> tuple[str, str, list[str]]:
    """Parse HTML and return (text, title, same_domain_links). Shared by HTTP and Playwright paths."""
    soup = BeautifulSoup(html, "html.parser")
    base_parsed = urlparse(base_url)
    base = f"{base_parsed.scheme}://{base_parsed.netloc}"

    def _class_matches(classes):
        if not classes:
            return False
        s = " ".join(classes) if isinstance(classes, list) else str(classes)
        return bool(re.search(r"content|main|body|faq|accordion", s, re.I))

    # Remove common cookie banners, consent widgets, breadcrumbs and repeated nav/footer blocks
    for sel in [
        "[id*=cookie]", "[class*=cookie]", "[id*=consent]", "[class*=consent]",
        "[class*=cookie-banner]", "[id*=cookie-banner]", "[class*=banner]",
        "[class*=breadcrumb]", "[id*=breadcrumb]", 
        "nav", "footer", "header",
    ]:
        for el in soup.select(sel):
            try:
                el.decompose()
            except Exception:
                pass

    main = soup.find(["main", "article"]) or soup.find(role="main") or soup.find(class_=_class_matches)
    root_html = str(main) if main else str(soup)

    def _try_markdown(html_fragment: str) -> str:
        try:
            return _html_to_markdown(html_fragment, base)
        except Exception:
            return ""

    text = _try_markdown(root_html)
    if not text or len(text) < MIN_CONTENT_LEN:
        text = _extract_text(BeautifulSoup(root_html, "html.parser"))
    if not text or len(text) < MIN_CONTENT_LEN:
        text = _try_markdown(html)
    if not text or len(text) < MIN_CONTENT_LEN:
        text = _extract_text(soup)

    # Remove repeated boilerplate blocks: if identical text blocks (paragraphs) appear multiple
    # times across the page, drop duplicates to reduce navigation/footer noise.
    blocks = [b.strip() for b in re.split(r"\n{2,}", text) if b.strip()]
    seen: set[str] = set()
    deduped_blocks: list[str] = []
    for b in blocks:
        key = re.sub(r"\s+", " ", b).strip()
        if key in seen:
            continue
        seen.add(key)
        deduped_blocks.append(b)
    text = "\n\n".join(deduped_blocks)

    title = page_title or (soup.title.string or "").strip() or base_parsed.path or base_url

    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#") or href.startswith("mailto:") or href.startswith("javascript:"):
            continue
        full = urljoin(base, href)
        full = full.split("#")[0].rstrip("/") or full
        if _same_domain(full, base_parsed) and full not in links:
            links.append(full)

    return text, title, links


def scrape_url(
    url: str,
    status_store: dict | None = None,
    status_url: str | None = None,
) -> tuple[str, str, list[str]]:
    """Fetch URL using Playwright (always). Returns (clean_text, page_title, same_domain_links)."""

    def _report(phase: str, progress: int) -> None:
        if status_store is not None and status_url is not None:
            status_store[status_url] = {**status_store.get(status_url, {}), "phase": phase, "progress": progress}

    logger.info("scrape_url url=%s (always using Playwright)", url)

    # Always use Playwright for all URLs
    try:
        return _run_playwright_scrape(url, _report, _extract_from_html)
    except Exception as e:
        logger.error("scrape_url Playwright failed for url=%s error=%s", url, e)
        raise


def _run_playwright_scrape(url: str, _report, _extract_from_html) -> tuple[str, str, list[str]]:
    """Run Playwright scrape. Returns (clean_text, page_title, same_domain_links)."""
    debug = getattr(settings, "scrape_debug", False)
    headless = getattr(settings, "scrape_headless", True)
    backend_dir = os.path.dirname(os.path.abspath(__file__))
    # Always write debug HTML for emiratesnbd.com to help diagnose issues
    always_debug = "emiratesnbd.com" in url.lower()

    with sync_playwright() as p:
        _report("scraping", 0)
        logger.info("scrape start url=%s headless=%s debug=%s", url, headless, debug)
        # For certain problematic sites prefer a headful, slowed run to match manual testing
        launch_kwargs = {"headless": headless}
        if debug:
            # Use headful + slow_mo only when explicit debugging is enabled
            launch_kwargs["headless"] = False
            launch_kwargs["slow_mo"] = 100
        browser = p.chromium.launch(**launch_kwargs)
        # UAE-focused: en-US + Asia/Dubai; real UA to reduce bot fingerprint
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1920, "height": 1080},
            ignore_https_errors=True,
            locale="en-US",
            timezone_id="Asia/Dubai",
            extra_http_headers=EXTRA_HEADERS,
            java_script_enabled=True,
        )
        page = context.new_page()
        _report("scraping", 5)

        # domcontentloaded first; simpler flow mirroring the working test script
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=SCRAPE_TIMEOUT_MS)
        except Exception as e:
            logger.warning("goto domcontentloaded failed: %s", e)
            try:
                page.goto(url, wait_until="load", timeout=SCRAPE_TIMEOUT_MS)
            except Exception as e2:
                logger.warning("goto load failed: %s", e2)
                raise
        _report("scraping", 10)

        # Handle cookie consent if present
        try:
            accept_btn = page.get_by_role("button", name="Accept All")
            accept_btn.wait_for(timeout=8000)
            accept_btn.click()
            page.wait_for_timeout(2000)
        except Exception:
            pass

        # Wait for dynamic content (text length heuristic)
        try:
            page.wait_for_function(
                "document.body && document.body.innerText.length > 1500",
                timeout=30000,
            )
            logger.info("Main content loaded.")
        except Exception:
            logger.info("Timed out waiting for large body text.")

        # Scroll to trigger lazy loading
        for _ in range(3):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(4000)

        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(2000)

        # Extract text and HTML
        body_len = page.evaluate("() => document.body ? document.body.innerText.length : 0")
        visible_text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
        html = page.content()
        page_title = page.title()
        logger.info("scrape before extract: body_len=%s title=%s", body_len, page_title)

        # Debug: write HTML to file for inspection
        if debug or always_debug:
            safe_name = re.sub(r"[^\w\-.]", "_", urlparse(url).netloc or "page")[:80]
            debug_path = os.path.join(backend_dir, f"debug_{safe_name}.html")
            try:
                with open(debug_path, "w", encoding="utf-8") as f:
                    f.write(html)
                logger.info("scrape debug HTML written to %s (body_len=%s)", debug_path, body_len)
                print(f"[DEBUG] HTML written to: {debug_path} (body_len={body_len})", file=sys.stderr)
            except Exception as e:
                logger.warning("scrape could not write debug HTML: %s", e)
                print(f"[DEBUG] Failed to write HTML: {e}", file=sys.stderr)

        # Preserve the visible innerText for use as a fallback against HTML parsing
        best_visible = visible_text

        browser.close()

    text, title, links = _extract_from_html(html, url, page_title)
    # Prefer visible/innerText when HTML parsing got little (JS-heavy, accordion, iframe)
    if best_visible:
        visible_clean = re.sub(r"\n{3,}", "\n\n", unescape(best_visible)).strip()
        if len(visible_clean) >= MIN_CONTENT_LEN and (
            len(visible_clean) > len(text) or len(text) < MIN_CONTENT_LEN
        ):
            text = visible_clean
    return text, title, links


SCRAPE_TIMEOUT_MSG = (
    f"We are not able to scrape this URL (timeout after {SCRAPE_OVERALL_TIMEOUT_SEC} seconds). "
    "Try again or use a different URL."
)


def _scrape_url_via_subprocess(url: str) -> tuple[str, str, list[str]]:
    """Run Playwright in a subprocess with overall timeout. Returns (text, title, links)."""
    backend_dir = os.path.dirname(os.path.abspath(__file__))
    worker = os.path.join(backend_dir, "scrape_worker.py")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        out_path = f.name
    try:
        proc = subprocess.run(
            [sys.executable, worker, url, out_path],
            cwd=backend_dir,
            capture_output=True,
            timeout=SCRAPE_OVERALL_TIMEOUT_SEC,
        )
        if os.path.isfile(out_path):
            with open(out_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("error"):
                raise RuntimeError(data["error"])
            return data["text"], data["title"], data["links"]
        proc.check_returncode()
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace") if proc.stderr else "Scrape worker failed")
    except subprocess.TimeoutExpired:
        raise RuntimeError(SCRAPE_TIMEOUT_MSG)
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def _scrape_for_ingest(
    url: str,
    status_store: dict | None,
    status_url: str | None,
) -> tuple[str, str, list[str]]:
    """Scrape URL with overall timeout; on Windows use subprocess to avoid event loop issues."""
    """
    Scrape URL with overall timeout; on Windows use subprocess to avoid event loop issues.
    (PDF scraping removed — ingestion uses Playwright HTML scraping only.)
    """
    if status_store and status_url:
        status_store[status_url] = {**status_store.get(status_url, {}), "phase": "scraping", "progress": 10}

    if sys.platform == "win32":
        text, title, links = _scrape_url_via_subprocess(url)
    else:
        # Non-Windows: run in-process but enforce overall timeout via thread + join
        result: list = []
        exc: list = []

        def run():
            try:
                result.append(scrape_url(url, status_store=status_store, status_url=status_url))
            except Exception as e:
                exc.append(e)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(timeout=SCRAPE_OVERALL_TIMEOUT_SEC)
        if thread.is_alive():
            raise RuntimeError(SCRAPE_TIMEOUT_MSG)
        if exc:
            raise exc[0]
        text, title, links = result[0]

    if status_store and status_url:
        status_store[status_url] = {**status_store.get(status_url, {}), "phase": "scraping", "progress": 20}
    return text, title, links


# Keywords that indicate a page is FAQ / Q&A related (only such pages are chunked and indexed)
FAQ_URL_KEYWORDS = ("faq", "faqs", "question", "questions", "help", "support")
FAQ_TITLE_TEXT_KEYWORDS = (
    "faq", "frequently asked", "questions and answers", "q&a", "q and a",
    "common questions", "help", "faqs", "know more", "learn more",
)

# Credit card product page keywords (for Emirates NBD credit card pages)
CREDIT_CARD_KEYWORDS = (
    "credit card", "credit-card", "creditcard", "card", "cards",
    "benefits", "eligibility", "fees", "rewards", "cashback", "points",
    "apply", "application", "features", "annual fee", "interest rate"
)


def _is_faq_related(url: str, title: str, text: str) -> bool:
    """Return True if the page appears to be FAQ-related or is a credit card product page from Emirates NBD.
    Only such pages are chunked and indexed."""
    url_lower = url.lower()
    title_lower = (title or "").lower()
    text_sample = (text or "")[:4000].lower()

    # Always allow pages from Emirates NBD domain (emiratesnbd.com)
    if "emiratesnbd.com" in url_lower:
        return True

    # Check for FAQ-related keywords
    if any(kw in url_lower for kw in FAQ_URL_KEYWORDS):
        return True
    if any(kw in title_lower for kw in FAQ_TITLE_TEXT_KEYWORDS):
        return True
    if any(kw in text_sample for kw in FAQ_TITLE_TEXT_KEYWORDS):
        return True

    # Check for credit card product page indicators (URL or content)
    if any(kw in url_lower for kw in CREDIT_CARD_KEYWORDS):
        return True
    if any(kw in title_lower for kw in CREDIT_CARD_KEYWORDS):
        return True
    if any(kw in text_sample for kw in CREDIT_CARD_KEYWORDS):
        return True

    return False


def _count_tokens(text: str) -> int:
    try:
        enc = tiktoken.encoding_for_model(getattr(settings, "embed_model", "text-embedding-3-small"))
    except Exception:
        enc = tiktoken.get_encoding("cl100k_base")
    return len(enc.encode(text))


def chunk_text(text: str, size: int = None, overlap: int = None) -> tuple[list[dict], dict]:
    """Rule-based chunker.

    Returns (chunks, summary) where chunks is a list of dicts:
      {"text":..., "page_section":..., "token_count": ...}
    """
    size = size or settings.chunk_size
    overlap = overlap or settings.chunk_overlap
    summary = {"total_before": 0, "total_after": 0, "avg_tokens": 0, "discarded": [], "sections": []}
    if not text or not text.strip():
        return [], summary

    # Normalize and split preserving markdown-style headings (#, ##, ###, ####)
    lines = text.splitlines()
    blocks: list[tuple[str, str]] = []  # (section_heading, block_text)
    current_heading = ""
    buf_lines: list[str] = []
    heading_levels_found: list[str] = []

    heading_re = re.compile(r"^(#{1,4})\s*(.+)$")
    for ln in lines:
        m = heading_re.match(ln.strip())
        if m:
            # flush
            if buf_lines:
                blocks.append((current_heading, "\n".join(buf_lines).strip()))
                buf_lines = []
            current_heading = m.group(2).strip()
            heading_levels_found.append(current_heading)
            # start a new empty block that will collect following content
            continue
        # use double-newline paragraph boundaries to break
        buf_lines.append(ln)

    if buf_lines:
        blocks.append((current_heading, "\n".join(buf_lines).strip()))

    # If no headings detected, try paragraph breaks
    if not any(h for h, _ in blocks):
        paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
        blocks = [("", p) for p in paragraphs]

    summary["sections"] = [s for s in heading_levels_found]

    # Remove repeated identical blocks
    seen_blocks: set[str] = set()
    unique_blocks: list[tuple[str, str]] = []
    for sec, blk in blocks:
        key = re.sub(r"\s+", " ", blk).strip()
        if key in seen_blocks:
            summary["discarded"].append({"reason": "duplicate_block", "text_snippet": (blk[:120] + "...")})
            continue
        seen_blocks.add(key)
        unique_blocks.append((sec, blk))

    # Now produce chunks following heading boundaries first
    raw_chunks: list[dict] = []
    for sec, blk in unique_blocks:
        # split by double newlines first inside the block
        parts = [p.strip() for p in re.split(r"\n{2,}", blk) if p.strip()]
        if not parts:
            continue
        # If parts empty (very long single block), split by single newline
        assembled: list[str] = []
        for p in parts:
            if not assembled:
                assembled.append(p)
            else:
                # try to keep target token size
                current = assembled[-1]
                if _count_tokens(current + "\n\n" + p) <= 250:
                    assembled[-1] = current + "\n\n" + p
                else:
                    assembled.append(p)

        for a in assembled:
            tok = _count_tokens(a)
            raw_chunks.append({"text": a.strip(), "page_section": sec or "", "token_count": tok})

    summary["total_before"] = len(raw_chunks)

    # Quality filters and token-size enforcement
    good_chunks: list[dict] = []
    discarded_reasons: dict[str, int] = {}

    def is_mostly_links(s: str) -> bool:
        words = re.findall(r"\S+", s)
        if not words:
            return False
        url_like = sum(1 for w in words if re.search(r"https?://|www\.|\\.com|\\.net|/", w))
        return (url_like / len(words)) > 0.6

    for idx, c in enumerate(raw_chunks):
        txt = c["text"]
        tok = c["token_count"]
        # discard if contains no alphanumeric
        if not re.search(r"[A-Za-z0-9]", txt):
            discarded_reasons.setdefault("no_alnum", 0)
            discarded_reasons["no_alnum"] += 1
            summary["discarded"].append({"reason": "no_alphanumeric", "chunk_index": idx})
            continue
        # discard if too few tokens
        if tok < 30:
            discarded_reasons.setdefault("too_short", 0)
            discarded_reasons["too_short"] += 1
            summary["discarded"].append({"reason": "too_short", "token_count": tok, "chunk_index": idx})
            continue
        # discard if mostly links
        if is_mostly_links(txt):
            discarded_reasons.setdefault("links_only", 0)
            discarded_reasons["links_only"] += 1
            summary["discarded"].append({"reason": "mostly_links", "chunk_index": idx})
            continue

        # enforce max token limit: split further at sentence boundaries or newlines
        if tok > 300:
            # try split at double newlines, then sentences
            parts = [p.strip() for p in re.split(r"\n{2,}", txt) if p.strip()]
            if len(parts) > 1:
                for p in parts:
                    ptok = _count_tokens(p)
                    if ptok >= 30:
                        good_chunks.append({"text": p, "page_section": c["page_section"], "token_count": ptok})
                continue
            # fallback to sentence split
            sents = re.split(r"(?<=[\.\!\?])\s+", txt)
            buf = ""
            for s in sents:
                if not buf:
                    buf = s
                else:
                    if _count_tokens(buf + " " + s) <= 300:
                        buf = buf + " " + s
                    else:
                        btok = _count_tokens(buf)
                        if btok >= 30:
                            good_chunks.append({"text": buf.strip(), "page_section": c["page_section"], "token_count": btok})
                        buf = s
            if buf and _count_tokens(buf) >= 30:
                good_chunks.append({"text": buf.strip(), "page_section": c["page_section"], "token_count": _count_tokens(buf)})
            continue

        # merge tiny chunks (<30 tokens) with next — but we already filtered <30 above, so only edge cases remain
        good_chunks.append(c)

    # Merge adjacent small chunks (<30 tokens) if any slipped through
    merged: list[dict] = []
    i = 0
    while i < len(good_chunks):
        cur = good_chunks[i]
        if cur["token_count"] < 30 and i + 1 < len(good_chunks):
            nxt = good_chunks[i + 1]
            merged_text = (cur["text"] + "\n\n" + nxt["text"]).strip()
            tok = _count_tokens(merged_text)
            merged.append({"text": merged_text, "page_section": cur.get("page_section") or nxt.get("page_section"), "token_count": tok})
            i += 2
        else:
            merged.append(cur)
            i += 1

    # Re-index chunks and compute stats
    for idx, c in enumerate(merged):
        c["chunk_index"] = idx
    total_tokens = sum(c["token_count"] for c in merged) if merged else 0
    summary["total_after"] = len(merged)
    summary["avg_tokens"] = (total_tokens // len(merged)) if merged else 0
    return merged, summary


def embed_chunks(client: OpenAI, chunks: list) -> list[list[float]]:
    """Accepts either list[str] or list[dict] (with 'text' key). Returns embeddings in order."""
    texts = []
    if not chunks:
        return []
    if isinstance(chunks[0], dict):
        texts = [c.get("text", "") for c in chunks]
    else:
        texts = list(chunks)
    resp = client.embeddings.create(
        model=settings.embed_model,
        input=texts,
    )
    return [d.embedding for d in sorted(resp.data, key=lambda x: x.index)]





def add_page_to_index(url: str, text: str, title: str, status_store: dict) -> None:
    """Chunk, embed, upsert one page; update status_store with phase and progress."""
    def _update(phase: str, progress: int, **extra):
        status_store[url] = {**status_store.get(url, {}), "phase": phase, "progress": progress, **extra}

    logger.info("add_page_to_index url=%s text_len=%s title=%s", url, len(text) if text else 0, title)
    if not text or len(text) < MIN_CONTENT_LEN:
        text_preview = (text[:200] + "...") if text and len(text) > 200 else (text or "(empty)")
        logger.warning("add_page_to_index skipped: text too short url=%s len=%s preview=%s", url, len(text) if text else 0, text_preview)
        error_msg = (
            f"No meaningful content extracted (got {len(text) if text else 0} chars, need {MIN_CONTENT_LEN}+). "
            f"Page may be JS-heavy, blocked by bot detection, or slow to load. "
            f"Check debug HTML file in backend directory if available."
        )
        status_store[url] = {
            "status": "completed",
            "chunks": 0,
            "error": error_msg,
            "phase": "completed",
            "progress": 100,
        }
        return
    if not _is_faq_related(url, title, text):
        status_store[url] = {
            "status": "completed",
            "chunks": 0,
            "error": "Skipped: page does not appear to be FAQ-related or a credit card product page (only FAQ/Q&A pages and credit card pages are indexed).",
            "phase": "completed",
            "progress": 100,
        }
        return
    _update("chunking", 25)
    chunks, chunk_summary = chunk_text(text)
    if not chunks:
        status_store[url] = {"status": "completed", "chunks": 0, "error": "No chunks produced from extracted text.", "phase": "completed", "progress": 100}
        return
    _update("embedding", 50)
    openai_client = OpenAI(api_key=settings.openai_api_key)
    vectors = None
    # Retry embeddings to handle transient OpenAI errors
    for attempt in range(3):
        try:
            vectors = embed_chunks(openai_client, chunks)
            break
        except Exception as e:
            logger.warning("embed attempt %s failed: %s", attempt + 1, e)
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)

    _update("uploading", 75)
    qdrant = get_qdrant()
    ensure_collection(qdrant)
    # Build points with extended metadata per chunk
    points = []
    ts_now = __import__("datetime").datetime.utcnow().isoformat() + "Z"
    for c, vec in zip(chunks, vectors):
        page_content = c.get("text") if isinstance(c, dict) else str(c)
        metadata = {
            "source_url": url,
            "title": title,
            "ingest_timestamp": ts_now,
            "page_section": c.get("page_section", "") if isinstance(c, dict) else "",
            "chunk_index": int(c.get("chunk_index", 0)) if isinstance(c, dict) else 0,
            "token_count": int(c.get("token_count", 0)) if isinstance(c, dict) else _count_tokens(page_content),
        }
        points.append(
            PointStruct(
                id=str(uuid.uuid4()),
                vector=vec,
                payload={
                    "page_content": page_content,
                    "metadata": metadata,
                },
            )
        )
    # Retry upsert to Qdrant to mitigate transient write timeouts
    for attempt in range(3):
        try:
            qdrant.upsert(collection_name=settings.collection_name, points=points)
            break
        except Exception as e:
            logger.warning("qdrant upsert attempt %s failed: %s", attempt + 1, e)
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
    logger.info("add_page_to_index indexed url=%s chunks=%s", url, len(chunks))
    # Attach summary information
    status_store[url] = {
        "status": "completed",
        "chunks": len(chunks),
        "error": None,
        "phase": "completed",
        "progress": 100,
        "summary": {**chunk_summary, "indexed_chunks": len(chunks)},
    }


def ingest_url(url: str, status_store: dict, crawl: bool = False, crawl_max_pages: int = 10) -> None:
    """Background ingestion: scrape, optionally crawl same-domain links, chunk, embed, upsert to Qdrant."""
    status_store[url] = {"status": "processing", "chunks": 0, "error": None, "phase": "scraping", "progress": 0}
    try:
        text, title, links = _scrape_for_ingest(url, status_store=status_store, status_url=url)
        add_page_to_index(url, text, title, status_store)

        if not crawl or crawl_max_pages <= 1:
            return

        visited = {url}
        queue = [u for u in links if u not in visited][: crawl_max_pages - 1]
        while queue and len(visited) < crawl_max_pages:
            next_url = queue.pop(0)
            if next_url in visited:
                continue
            visited.add(next_url)
            status_store[next_url] = {"status": "processing", "chunks": 0, "error": None, "phase": "scraping", "progress": 0}
            try:
                sub_text, sub_title, sub_links = _scrape_for_ingest(next_url, status_store=status_store, status_url=next_url)
                add_page_to_index(next_url, sub_text, sub_title, status_store)
                for u in sub_links:
                    if u not in visited and u not in queue and len(visited) + len(queue) < crawl_max_pages:
                        queue.append(u)
            except Exception as e:
                status_store[next_url] = {"status": "completed", "chunks": 0, "error": str(e), "phase": "completed", "progress": 100}
    except Exception as e:
        status_store[url] = {"status": "completed", "chunks": 0, "error": str(e) or "Scraping or indexing failed.", "phase": "completed", "progress": 100}
