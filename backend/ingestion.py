import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from html import unescape
from urllib.parse import urljoin, urlparse

import html2text
import httpx
from bs4 import BeautifulSoup
from openai import OpenAI
from playwright.sync_api import sync_playwright
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from config import settings

# Wait for JS-rendered content; increased for JS-heavy / API-driven pages (e.g. bank FAQs)
POST_LOAD_WAIT_MS = 10000
SCRAPE_TIMEOUT_MS = 60000
WAIT_FOR_CONTENT_MS = 25000
ACCORDION_CLICK_DELAY_MS = 300
SCROLL_STEPS = 10
SCROLL_STEP_DELAY_MS = 1200
MIN_CONTENT_LEN = 30

# Real-looking browser to reduce blocking (Chrome 144)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
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


def _fetch_simple(url: str) -> str | None:
    """Fetch URL with httpx (no JS). Returns HTML or None. Works for static/simple pages."""
    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=30.0,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        ) as client:
            r = client.get(url)
            r.raise_for_status()
            return r.text
    except Exception:
        return None


def scrape_url(
    url: str,
    status_store: dict | None = None,
    status_url: str | None = None,
) -> tuple[str, str, list[str]]:
    """Fetch URL (simple HTTP first, then Playwright), return (clean_text, page_title, same_domain_links)."""

    def _report(phase: str, progress: int) -> None:
        if status_store is not None and status_url is not None:
            status_store[status_url] = {**status_store.get(status_url, {}), "phase": phase, "progress": progress}

    # 1) Try simple HTTP first – only use if we get enough content (avoid using hero-only HTML for FAQ/JS pages)
    html_simple = _fetch_simple(url)
    if html_simple:
        text, title, links = _extract_from_html(html_simple, url, "")
        if text and len(text) >= 600:  # require meaningful length so FAQ/JS pages fall through to Playwright
            return text, title, links

    # 2) Fall back to Playwright for JS-heavy or blocked pages
    with sync_playwright() as p:
        _report("scraping", 0)
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1920, "height": 1080},
            ignore_https_errors=True,
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            extra_http_headers=EXTRA_HEADERS,
        )
        def _block_assets(route):
            if route.request.resource_type in ("image", "font", "media"):
                route.abort()
            else:
                route.continue_()
        context.route("**/*", _block_assets)
        page = context.new_page()
        _report("scraping", 5)
        # networkidle: wait for API calls to settle (better for bank/FAQ pages)
        try:
            page.goto(url, wait_until="networkidle", timeout=SCRAPE_TIMEOUT_MS)
        except Exception:
            page.goto(url, wait_until="load", timeout=SCRAPE_TIMEOUT_MS)
        _report("scraping", 10)
        page.wait_for_timeout(POST_LOAD_WAIT_MS)
        _report("scraping", 12)
        # Wait for substantial content (FAQ/accordion often loads after hero)
        try:
            page.wait_for_function(
                "document.body && document.body.innerText && document.body.innerText.length > 500",
                timeout=WAIT_FOR_CONTENT_MS,
            )
        except Exception:
            page.wait_for_timeout(8000)
        # Expand accordions / collapsibles so FAQ text is in the DOM
        try:
            for selector in [
                "button[aria-expanded], [data-toggle], details summary, .accordion-trigger, [class*='accordion'] button, .faq-item button",
                "button",
                "[role='button']",
            ]:
                try:
                    els = page.query_selector_all(selector)
                    for el in els[:50]:  # cap to avoid endless clicking
                        try:
                            if el.is_visible():
                                el.click()
                                page.wait_for_timeout(ACCORDION_CLICK_DELAY_MS)
                        except Exception:
                            pass
                except Exception:
                    pass
        except Exception:
            pass
        page.wait_for_timeout(2000)
        _report("scraping", 16)
        # Realistic scroll with mouse wheel to trigger lazy load
        try:
            for _ in range(SCROLL_STEPS):
                page.mouse.wheel(0, 1000)
                page.wait_for_timeout(SCROLL_STEP_DELAY_MS)
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(1000)
        except Exception:
            try:
                for _ in range(6):
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(1000)
                page.evaluate("window.scrollTo(0, 0)")
            except Exception:
                pass
        _report("scraping", 20)
        html = page.content()
        page_title = page.title()
        # Visible text from main frame (critical for JS-heavy / shadow DOM)
        try:
            visible_text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
        except Exception:
            visible_text = ""
        # Iframe content: FAQ sometimes lives in an iframe
        frame_texts = [visible_text]
        try:
            for frame in page.frames:
                if frame == page.main_frame:
                    continue
                try:
                    t = frame.evaluate("() => document.body ? document.body.innerText : ''") or ""
                    if t and len(t) > 100:
                        frame_texts.append(t)
                except Exception:
                    pass
        except Exception:
            pass
        best_visible = max(frame_texts, key=len) if frame_texts else ""
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


def _scrape_url_via_subprocess(url: str) -> tuple[str, str, list[str]]:
    """On Windows, run Playwright in a subprocess to avoid NotImplementedError. Returns (text, title, links)."""
    backend_dir = os.path.dirname(os.path.abspath(__file__))
    worker = os.path.join(backend_dir, "scrape_worker.py")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        out_path = f.name
    try:
        proc = subprocess.run(
            [sys.executable, worker, url, out_path],
            cwd=backend_dir,
            capture_output=True,
            timeout=180,
        )
        if os.path.isfile(out_path):
            with open(out_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("error"):
                raise RuntimeError(data["error"])
            return data["text"], data["title"], data["links"]
        proc.check_returncode()
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace") if proc.stderr else "Scrape worker failed")
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
    """Scrape URL; on Windows use subprocess to avoid event loop issues."""
    if sys.platform == "win32":
        if status_store and status_url:
            status_store[status_url] = {**status_store.get(status_url, {}), "phase": "scraping", "progress": 10}
        text, title, links = _scrape_url_via_subprocess(url)
        if status_store and status_url:
            status_store[status_url] = {**status_store.get(status_url, {}), "phase": "scraping", "progress": 20}
        return text, title, links
    return scrape_url(url, status_store=status_store, status_url=status_url)


# Keywords that indicate a page is FAQ / Q&A related (only such pages are chunked and indexed)
FAQ_URL_KEYWORDS = ("faq", "faqs", "question", "questions", "help", "support")
FAQ_TITLE_TEXT_KEYWORDS = (
    "faq", "frequently asked", "questions and answers", "q&a", "q and a",
    "common questions", "help", "faqs", "know more", "learn more",
)


def _is_faq_related(url: str, title: str, text: str) -> bool:
    """Return True if the page appears to be FAQ-related; only such pages are chunked and indexed."""
    url_lower = url.lower()
    title_lower = (title or "").lower()
    text_sample = (text or "")[:4000].lower()

    if any(kw in url_lower for kw in FAQ_URL_KEYWORDS):
        return True
    if any(kw in title_lower for kw in FAQ_TITLE_TEXT_KEYWORDS):
        return True
    if any(kw in text_sample for kw in FAQ_TITLE_TEXT_KEYWORDS):
        return True
    return False


def chunk_text(text: str, size: int = None, overlap: int = None) -> list[str]:
    size = size or settings.chunk_size
    overlap = overlap or settings.chunk_overlap
    chunks = []
    start = 0
    while start < len(text):
        end = start + size
        chunk = text[start:end]
        if chunk.strip():
            chunks.append(chunk.strip())
        start = end - overlap
    return chunks


def embed_chunks(client: OpenAI, chunks: list[str]) -> list[list[float]]:
    resp = client.embeddings.create(
        model=settings.embed_model,
        input=chunks,
    )
    return [d.embedding for d in sorted(resp.data, key=lambda x: x.index)]


def add_page_to_index(url: str, text: str, title: str, status_store: dict) -> None:
    """Chunk, embed, upsert one page; update status_store with phase and progress."""
    def _update(phase: str, progress: int, **extra):
        status_store[url] = {**status_store.get(url, {}), "phase": phase, "progress": progress, **extra}

    if not text or len(text) < MIN_CONTENT_LEN:
        status_store[url] = {
            "status": "completed",
            "chunks": 0,
            "error": "No meaningful content extracted (page may be JS-heavy, blocked, or slow to load). Try again or use a different URL.",
            "phase": "completed",
            "progress": 100,
        }
        return
    if not _is_faq_related(url, title, text):
        status_store[url] = {
            "status": "completed",
            "chunks": 0,
            "error": "Skipped: page does not appear to be FAQ-related (only FAQ/Q&A pages are indexed).",
            "phase": "completed",
            "progress": 100,
        }
        return
    _update("chunking", 25)
    chunks = chunk_text(text)
    if not chunks:
        status_store[url] = {"status": "completed", "chunks": 0, "error": "No chunks produced from extracted text.", "phase": "completed", "progress": 100}
        return
    _update("embedding", 50)
    openai_client = OpenAI(api_key=settings.openai_api_key)
    vectors = embed_chunks(openai_client, chunks)
    _update("uploading", 75)
    qdrant = get_qdrant()
    ensure_collection(qdrant)
    points = [
        PointStruct(
            id=str(uuid.uuid4()),
            vector=vec,
            payload={
                "page_content": chunk,
                "metadata": {
                    "source_url": url,
                    "title": title,
                    "ingest_timestamp": __import__("datetime").datetime.utcnow().isoformat() + "Z",
                },
            },
        )
        for chunk, vec in zip(chunks, vectors)
    ]
    qdrant.upsert(collection_name=settings.collection_name, points=points)
    status_store[url] = {"status": "completed", "chunks": len(chunks), "error": None, "phase": "completed", "progress": 100}


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
