"use client";

import { useState, useEffect, useMemo, useCallback } from "react";
import Link from "next/link";
import { clearData, getSources, ingestUrl, type SourceItem } from "@/lib/api";
import styles from "./admin.module.css";

const PROGRESS_DURATION_MS = 60000; // fallback simulated progress when backend doesn't send progress yet

function useProgress(sources: SourceItem[]) {
  const [now, setNow] = useState(Date.now());
  const [startTimes, setStartTimes] = useState<Record<string, number>>({});

  useEffect(() => {
    const next: Record<string, number> = { ...startTimes };
    sources.forEach((s) => {
      if (s.status === "processing" && next[s.url] == null) next[s.url] = Date.now();
    });
    if (Object.keys(next).length > Object.keys(startTimes).length) setStartTimes((prev) => ({ ...prev, ...next }));
  }, [sources]);

  useEffect(() => {
    const hasProcessing = sources.some((s) => s.status === "processing");
    if (!hasProcessing) return;
    const t = setInterval(() => setNow(Date.now()), 500);
    return () => clearInterval(t);
  }, [sources]);

  return useMemo(() => {
    const map: Record<string, number> = {};
    sources.forEach((s) => {
      if (s.status === "completed") {
        map[s.url] = s.progress ?? 100;
      } else if (s.status === "processing") {
        // Use real progress from backend when available
        if (typeof s.progress === "number") {
          map[s.url] = Math.min(99, s.progress);
        } else {
          const start = startTimes[s.url] ?? now;
          const elapsed = now - start;
          map[s.url] = Math.min(92, (elapsed / PROGRESS_DURATION_MS) * 92);
        }
      }
    });
    return map;
  }, [sources, startTimes, now]);
}

const PHASE_LABELS: Record<string, string> = {
  scraping: "Scraping page…",
  chunking: "Chunking text…",
  embedding: "Creating embeddings…",
  uploading: "Uploading to index…",
  completed: "Done",
};

function scrapingDetail(progress: number): string {
  if (progress <= 7) return "Connecting…";
  if (progress <= 14) return "Loading page…";
  if (progress <= 19) return "Waiting for content…";
  return "Extracting…";
}

function ProgressBar({
  progress,
  done,
  error,
  phase,
}: {
  progress: number;
  done: boolean;
  error?: string | null;
  phase?: string;
}) {
  const phaseLabel =
    phase && PHASE_LABELS[phase]
      ? phase === "scraping"
        ? `${PHASE_LABELS[phase]} ${scrapingDetail(progress)}`
        : PHASE_LABELS[phase]
      : phase
        ? `${phase}…`
        : null;
  return (
    <div className={styles.progressWrap}>
      {!done && phaseLabel && <span className={styles.progressPhase}>{phaseLabel}</span>}
      <div className={styles.progressTrack}>
        <div
          className={`${styles.progressFill} ${done ? styles.progressDone : ""} ${error ? styles.progressError : ""}`}
          style={{ width: `${progress}%` }}
        />
        {!done && progress < 100 && (
          <div className={styles.progressShine} style={{ left: `${progress}%` }} />
        )}
      </div>
      <span className={styles.progressPct}>{done ? "100" : Math.round(progress)}%</span>
    </div>
  );
}

export default function AdminPage() {
  const [sources, setSources] = useState<SourceItem[]>([]);
  const [url, setUrl] = useState("");
  const [crawl, setCrawl] = useState(false);
  const [loading, setLoading] = useState(false);
  const [ingesting, setIngesting] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const progressMap = useProgress(sources);

  const loadSources = useCallback(async () => {
    setLoading(true);
    try {
      const data = await getSources();
      setSources(data);
    } catch {
      setError("Could not load sources");
    } finally {
      setLoading(false);
    }
  }, []);

  // Poll sources - less frequently when idle, more frequently when processing
  const hasProcessing = useMemo(() => sources.some((s) => s.status === "processing"), [sources]);
  
  useEffect(() => {
    loadSources(); // Load immediately on mount or when processing status changes
    
    // Use different intervals based on processing status
    // 15 seconds when idle, 3 seconds when processing
    const interval = hasProcessing ? 3000 : 15000;
    
    const t = setInterval(loadSources, interval);
    return () => clearInterval(t);
  }, [hasProcessing, loadSources]); // Re-run when processing status changes to switch intervals

  async function handleIngest(e: React.FormEvent) {
    e.preventDefault();
    const u = url.trim();
    if (!u || ingesting) return;
    setError(null);
    setIngesting(true);
    try {
      await ingestUrl(u, { crawl, crawlMaxPages: 10 });
      setUrl("");
      await loadSources();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Ingestion failed");
    } finally {
      setIngesting(false);
    }
  }

  async function handleClearData() {
    if (!confirm("Clear all indexed data from Qdrant? This cannot be undone. The source list will be reset.")) return;
    setError(null);
    setClearing(true);
    try {
      await clearData();
      await loadSources();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to clear data");
    } finally {
      setClearing(false);
    }
  }

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div>
          <h1>Emirates NBD Ingestion Dashboard</h1>
          <p className={styles.subtitle}>ENBD QNA Admin</p>
        </div>
        <div className={styles.headerActions}>
          <button
            type="button"
            onClick={handleClearData}
            disabled={clearing || ingesting}
            className={styles.clearButton}
            title="Clear all indexed data from Qdrant"
          >
            {clearing ? "Clearing…" : "Clear Data"}
          </button>
          <Link href="/chat" className={styles.chatLink}>← Chat</Link>
        </div>
      </header>

      <main className={styles.main}>
        <section className={styles.section}>
          <h2>Ingest a URL</h2>
          <p className={styles.hint}>Enter an Emirates NBD (emiratesnbd.com) credit card page URL. Content will be scraped, chunked, and indexed for the ENBD QNA Bot.</p>
          <form onSubmit={handleIngest} className={styles.form}>
            <input
              type="url"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://www.bank.com/credit-cards"
              disabled={ingesting}
              className={styles.input}
              required
            />
            <label className={styles.checkbox}>
              <input
                type="checkbox"
                checked={crawl}
                onChange={(e) => setCrawl(e.target.checked)}
                disabled={ingesting}
              />
              Crawl until end (follow same-domain links, up to 10 pages)
            </label>
            <button type="submit" disabled={ingesting} className={styles.button}>
              {ingesting ? "Starting…" : "Start ingestion"}
            </button>
          </form>
          {error && <div className={styles.error}>{error}</div>}
        </section>

        <section className={styles.section}>
          <h2>Indexed sources</h2>
          {loading && sources.length === 0 ? (
            <p className={styles.muted}>Loading…</p>
          ) : sources.length === 0 ? (
            <p className={styles.muted}>No sources yet. Ingest a URL above.</p>
          ) : (
            <ul className={styles.list}>
              {sources.map((s, i) => (
                <li key={i} className={styles.item}>
                  <div className={styles.itemTop}>
                    <span className={styles.itemUrl}>{s.url}</span>
                    <span className={`${styles.itemStatus} ${s.status === "completed" ? styles.itemStatusDone : ""}`}>
                      {s.status === "processing" ? (
                        <span className={styles.statusPulse}>Processing</span>
                      ) : s.status === "completed" ? (
                        <>✓ Completed</>
                      ) : (
                        s.status
                      )}
                    </span>
                  </div>
                  <ProgressBar
                    progress={progressMap[s.url] ?? 0}
                    done={s.status === "completed"}
                    error={s.error}
                    phase={s.phase}
                  />
                  <div className={styles.itemMeta}>
                    {s.status === "completed" && (
                      <span className={s.chunks > 0 ? styles.itemChunks : styles.itemChunksZero}>
                        {s.chunks} chunks indexed
                      </span>
                    )}
                    {(s.error || (s.status === "completed" && s.chunks === 0)) && (
                      <span className={styles.itemError}>
                        {s.error || "No content extracted. Page may be blocked or need more time to load."}
                      </span>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>
      </main>
    </div>
  );
}
