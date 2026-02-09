# Credit Card Q&A FAQ Bot

A proof-of-concept RAG chatbot that answers customer questions about bank credit cards using content ingested from the bank's website.

## Architecture

- **Frontend (Next.js 14):** Chat at `/chat`, Admin ingestion at `/admin`
- **Backend (FastAPI):** REST API for chat and ingestion; background scraping with Playwright
- **Vector DB (Qdrant):** Used via **API** (Qdrant Cloud or remote); stores embeddings for semantic search
- **LLM (OpenAI):** GPT-4o for answers, text-embedding-3-small for embeddings

## Prerequisites

- **Windows** (local run): Python 3.11+, Node.js 20+
- **Accounts/keys:** [OpenAI API key](https://platform.openai.com/api-keys), [Qdrant Cloud](https://cloud.qdrant.io) (or another Qdrant instance with API access)

---

## First-time setup (do this once)

From the project root (e.g. `D:\Credit Card QNA FAQ Bot`):

**Backend**
```powershell
cd backend
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```
Create `backend\.env` from `backend\.env.example` and set `OPENAI_API_KEY`, `QDRANT_URL`, `QDRANT_API_KEY` (see [Environment variables](#environment-variables)).

**Frontend** (in a new terminal)
```powershell
cd frontend
npm ci
```
Create `frontend\.env.local` with `NEXT_PUBLIC_API_URL=http://localhost:8000` (or copy from `frontend\.env.local.example`).

---

## Steps to run (every time)

If you haven’t set up the project yet, do [First-time setup](#first-time-setup-do-this-once) above first.

Use **two terminals**. From the project root:

**Terminal 1 – Backend**
```powershell
cd backend
.\venv\Scripts\activate
uvicorn main:app --reload --port 8000
```
Leave running. API: http://localhost:8000

**Terminal 2 – Frontend**
```powershell
cd frontend
npm run dev
```
Then open in browser: **http://localhost:3000/chat** and **http://localhost:3000/admin**

Before asking questions, add documents (see [Adding documents](#adding-documents-ingesting-urls) below).

---

## Adding documents (ingesting URLs)

The bot answers only from content you ingest. Add FAQ or credit-card pages via the **Admin** UI (or the API).

### Using the Admin page

1. Open **http://localhost:3000/admin** (with backend and frontend running).
2. In **URL to ingest**, paste a credit card FAQ or product page URL. Examples:
   - `https://www.hsbc.co.in/credit-cards/faq/`
   - `https://www.paisabazaar.com/faqs/credit-card/`
3. (Optional) Check **Crawl same-domain links** to follow and index linked pages from the same site (up to 10 pages by default). Useful for multi-page FAQs.
4. Click **Start ingestion**. The list below shows status (e.g. *Scraping…*, *Chunking…*, *Completed*) and chunk count.
5. Wait until status is **Completed** and **Chunks** is greater than 0. You can add more URLs one by one; each ingestion runs in the background.
6. Go to **Chat** (http://localhost:3000/chat) and ask questions; answers will use the ingested content.

**Note:** Only pages that look FAQ-related (URL/title or content contain terms like “faq”, “question”, “help”) are indexed; other pages are skipped. To start over, use **Clear Data** on the Admin page (this removes all indexed content).

### Using the API

```bash
# Ingest a single URL (no crawl)
curl -X POST http://localhost:8000/ingest -H "Content-Type: application/json" -d "{\"url\": \"https://www.hsbc.co.in/credit-cards/faq/\"}"

# Ingest with crawl (same-domain links, up to 10 pages)
curl -X POST http://localhost:8000/ingest -H "Content-Type: application/json" -d "{\"url\": \"https://www.paisabazaar.com/faqs/credit-card/\", \"crawl\": true, \"crawl_max_pages\": 10}"
```

Check status: `GET http://localhost:8000/sources` — lists ingested URLs and chunk counts.

---

## Environment variables

| Variable | Where | Required | Description |
|----------|--------|----------|-------------|
| `OPENAI_API_KEY` | Backend `.env` | Yes | OpenAI API key for GPT-4o and embeddings |
| `QDRANT_URL` | Backend `.env` | Yes | Qdrant cluster URL (e.g. `https://xxx.aws.cloud.qdrant.io`) |
| `QDRANT_API_KEY` | Backend `.env` | Yes | Qdrant API key (when using Qdrant Cloud / remote API) |
| `COLLECTION_NAME` | Backend `.env` | No (default: `credit_card_knowledge`) | Qdrant collection name; **created automatically** if missing |
| `EMBEDDING_PROVIDER` | Backend `.env` | No (default: `openai`) | Embedding provider (only `openai` supported in this POC) |
| `EMBED_MODEL` | Backend `.env` | No (default: `text-embedding-3-small`) | OpenAI embedding model (1536 dimensions) |
| `NEXT_PUBLIC_API_URL` | Frontend `.env.local` | No (default: `http://localhost:8000`) | Backend API URL for the browser |

---

## Setup and run (Windows, local backend + frontend, Qdrant via API)

### 1. Qdrant

Create a cluster at [cloud.qdrant.io](https://cloud.qdrant.io) and note:

- **Cluster URL** (e.g. `https://your-cluster-id.aws.cloud.qdrant.io`)
- **API key**

No local Qdrant container is required.

**Collection:** The backend **creates the Qdrant collection automatically** on first use (first ingest or first chat) if it doesn’t exist. You do not need to create it in Qdrant yourself. The collection name is set by `COLLECTION_NAME` in backend `.env` (default: `credit_card_knowledge`).

### 2. Backend

```powershell
cd "D:\Credit Card QNA FAQ Bot\backend"
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

Create `backend\.env` (copy from `backend\.env.example` and fill in):

```env
OPENAI_API_KEY=sk-your-openai-api-key
QDRANT_URL=https://your-cluster-id.aws.cloud.qdrant.io
QDRANT_API_KEY=your-qdrant-api-key
```

Start the API:

```powershell
uvicorn main:app --reload --port 8000
```

Keep this terminal open. API: http://localhost:8000

### 3. Frontend

In a **new** terminal:

```powershell
cd "D:\Credit Card QNA FAQ Bot\frontend"
npm ci
```

Create `frontend\.env.local` (copy from `frontend\.env.local.example` or add):

```env
NEXT_PUBLIC_API_URL=http://localhost:8000
```

Start the dev server:

```powershell
npm run dev
```

Open in browser:

- **Chat:** http://localhost:3000/chat  
- **Admin:** http://localhost:3000/admin  

### 4. Use the app

1. **Admin** → paste a bank credit card page URL → optionally check **Crawl same-domain links** → **Start ingestion**. Wait until status is **completed** and chunks > 0 (refresh or wait for auto-refresh).
2. **Chat** → ask questions (e.g. “What credit cards do you offer?”, “What is the annual fee?”, “How do I apply?”).

---

## API reference

| Method | Endpoint | Body | Description |
|--------|----------|------|-------------|
| POST | `/ingest` | `{ "url": "https://...", "crawl": false, "crawl_max_pages": 10 }` | Start background ingestion. If `crawl` is true, same-domain links are followed and ingested up to `crawl_max_pages` (max 50). Returns status and `task_id`. |
| GET | `/sources` | — | List ingested URLs with status and chunk count (includes Qdrant-backed list after restart). |
| DELETE | `/clear` | — | Clear all indexed data from Qdrant and reset ingestion status. |
| POST | `/chat` | `{ "query": "..." }` | Get answer and source URLs: `{ "answer": "...", "sources": ["url1", ...] }`. |
| GET | `/health` | — | Health check. |

---

## POC requirements checklist

| Requirement | How it is satisfied |
|-------------|---------------------|
| **Purpose** — Q&A bot for bank credit cards using website as source of truth | RAG pipeline: ingest bank URL(s) → chunk → embed → store in Qdrant; chat retrieves relevant chunks and answers with GPT-4o. |
| **FR1 — Content ingestion** — Fetch/process content from given URL(s), enough to answer common questions | **Admin** (or API): provide bank URL; optional **crawl** follows same-domain links (up to 10 pages). Scrape → extract text → chunk → embed → upsert to Qdrant. Only FAQ-related pages are indexed (URL/title/text check). **Admin** and **GET /sources** show what was ingested. |
| **FR2 — Question answering** — User asks; bot answers from ingested content | **Chat** UI and **POST /chat**: natural-language question → vector search → top chunks sent to LLM → answer returned. Prompt instructs the model to use only the provided context. |
| **FR3 — Source grounding** — Show source link(s) or citation | Every chat response includes **sources** (list of URLs). The UI displays "Sources: [links]" under each answer so users can verify. |
| **FR4 — Unknown / not found** — Polite response when answer not in data | If no chunks above score threshold (0.55), bot returns: *"I couldn't find that information in the bank's content. You may want to check the bank's website or contact their support."* |
| **FR5 — Basic guardrails** — No "official advice"; short disclaimer | System prompt says answers are informational and from website content only. **Chat footer** shows: *"This bot provides information based on website content only. It does not offer financial advice."* |
| **Minimal interface** — CLI or simple web UI | **Web UI:** Chat at `/chat`, Admin at `/admin`. No auth; minimal and focused. |
| **README** — Setup, run steps, what was ingested, design choices, limitations, production ideas | This README: run steps (§ Steps to run, § Setup and run), ingested data (§ Ingested data, Admin/GET /sources), design choices and limitations (§ Design choices and limitations), production improvements (§ Possible production improvements). |
| **No sensitive personal data** | No user accounts; no collection or storage of customer PII. Only public website content is ingested and queried. |

## Design choices and limitations

- **Single-URL or crawl:** By default one URL per request; optional **crawl** follows same-domain links and ingests up to 10 (or 50) pages. Only **FAQ-related** pages are chunked and indexed (others are skipped).
- **In-memory status:** Ingestion status is not persisted across backend restarts; **GET /sources** merges with Qdrant so indexed URLs still appear after restart.
- **Search:** Dense vector search only (no sparse/keyword layer in this POC). Retrieval uses score threshold 0.55 and up to 6 chunks for context.
- **Guardrails:** If no chunks meet the score threshold (0.55), the bot returns an “I don’t know” style message. A disclaimer is shown in the chat footer.

## Ingested data

The **Admin** page (and **GET /sources**) list all ingested URLs and their status/chunk count. Use that as the record of what was indexed.

## Example questions the bot can handle

(No hardcoding—handled via RAG.) Examples: *"What credit cards do you offer?"*, *"Which card is best for travel rewards?"*, *"What is the annual fee for Card X?"*, *"Do you offer cashback cards?"*, *"What are the eligibility requirements?"*, *"How do I apply?"*, *"What are the benefits of Card Y?"*, *"Are there any welcome bonuses?"*

## Possible production improvements

- Recursive crawl from the given URL (e.g. depth=2)
- Persist ingestion status in a database
- RBAC for the Admin route
- Source highlighting (link to specific paragraph on the source page)
