const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export type SourceItem = {
  url: string;
  status: string;
  chunks: number;
  error: string | null;
  phase?: string;
  progress?: number;
};

export async function getSources(): Promise<SourceItem[]> {
  const res = await fetch(`${API_URL}/sources`, { cache: "no-store" });
  if (!res.ok) throw new Error("Failed to fetch sources");
  return res.json();
}

export async function clearData(): Promise<{ status: string; message: string }> {
  const res = await fetch(`${API_URL}/clear`, { method: "DELETE" });
  if (!res.ok) throw new Error("Failed to clear data");
  return res.json();
}

export async function ingestUrl(
  url: string,
  options?: { crawl?: boolean; crawlMaxPages?: number }
): Promise<{ status: string; task_id: string; message: string }> {
  const res = await fetch(`${API_URL}/ingest`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      url,
      crawl: options?.crawl ?? true,
      crawl_max_pages: options?.crawlMaxPages ?? 10,
    }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || "Ingestion failed");
  }
  return res.json();
}

export type ChatResponse = { answer: string; sources: string[] };

export async function sendChat(query: string): Promise<ChatResponse> {
  const res = await fetch(`${API_URL}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query }),
  });
  if (!res.ok) throw new Error("Chat request failed");
  return res.json();
}
