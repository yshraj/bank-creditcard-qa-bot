import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException
from urllib.parse import urlparse

# Ensure ingestion/scrape logs are visible when running uvicorn
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logging.getLogger("ingestion").setLevel(logging.INFO)
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl

from chat import retrieve_and_answer
from ingestion import clear_collection, get_qdrant, get_sources_from_qdrant, ingest_url, ensure_collection

# In-memory store for ingestion status (per-URL). For POC only.
ingestion_status: dict[str, dict[str, Any]] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        qdrant = get_qdrant()
        ensure_collection(qdrant)
    except Exception:
        pass
    yield


app = FastAPI(title="Credit Card Q&A API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class IngestBody(BaseModel):
    url: HttpUrl
    crawl: bool = True  # by default, follow same-domain links until limit
    crawl_max_pages: int = 10


class ChatBody(BaseModel):
    query: str


@app.post("/ingest")
def ingest(body: IngestBody, background_tasks: BackgroundTasks):
    url = str(body.url)
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    if not host.endswith("emiratesnbd.com"):
        raise HTTPException(status_code=400, detail="Ingestion only allowed for Emirates NBD (emiratesnbd.com) URLs.")
    if url in ingestion_status and ingestion_status[url].get("status") == "processing":
        raise HTTPException(status_code=409, detail="Ingestion already in progress for this URL")
    crawl_max_pages = max(1, min(body.crawl_max_pages, 50))
    background_tasks.add_task(
        ingest_url,
        url,
        ingestion_status,
        crawl=body.crawl,
        crawl_max_pages=crawl_max_pages,
    )
    msg = "Ingestion started (with crawl)." if body.crawl else "Ingestion started. Check /sources for status."
    return {"status": "processing", "task_id": url, "message": msg}


@app.delete("/clear")
def clear_data():
    """Clear all indexed data from Qdrant and reset ingestion status."""
    clear_collection()
    ingestion_status.clear()
    return {"status": "ok", "message": "All data cleared."}


@app.get("/sources")
def sources():
    # Merge in-memory status with Qdrant-backed counts (so list survives backend restart)
    qdrant_counts = get_sources_from_qdrant()
    seen = set()
    items = []
    for url, data in ingestion_status.items():
        seen.add(url)
        chunks = data.get("chunks", 0) or qdrant_counts.get(url, 0)
        items.append({
            "url": url,
            "status": data["status"],
            "chunks": chunks,
            "error": data.get("error"),
            "phase": data.get("phase"),
            "progress": data.get("progress"),
        })
    for url, count in qdrant_counts.items():
        if url not in seen:
            items.append({
                "url": url,
                "status": "completed",
                "chunks": count,
                "error": None,
                "phase": "completed",
                "progress": 100,
            })
    return items


@app.post("/chat")
def chat(body: ChatBody):
    if not body.query or not body.query.strip():
        raise HTTPException(status_code=400, detail="Query is required")
    result = retrieve_and_answer(body.query.strip())
    return result


@app.get("/health")
def health():
    return {"status": "ok"}
