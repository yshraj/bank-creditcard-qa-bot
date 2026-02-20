"use client";

import { useState, useRef, useEffect } from "react";
import ReactMarkdown from "react-markdown";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import Link from "next/link";
import { sendChat, type ChatResponse } from "@/lib/api";
import styles from "./chat.module.css";

export default function ChatPage() {
  const [messages, setMessages] = useState<{ role: "user" | "assistant"; content: string; sources?: string[] }[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const q = input.trim();
    if (!q || loading) return;
    setInput("");
    setError(null);
    setMessages((prev) => [...prev, { role: "user", content: q }]);
    setLoading(true);
    try {
      const data: ChatResponse = await sendChat(q);
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: data.answer, sources: data.sources },
      ]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong");
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: "Sorry, I couldn't process your question. Please try again or check the API." },
      ]);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div>
          <h1>Emirates NBD Credit Card Q&A</h1>
          <p className={styles.subtitle}>ENBD QNA Bot</p>
        </div>
        <Link href="/admin" className={styles.adminLink}>Admin → Ingest</Link>
      </header>

      <main className={styles.main}>
        <div className={styles.messages}>
          {messages.length === 0 && (
            <div className={styles.placeholder}>
              Ask a question about Emirates NBD credit cards (e.g. fees, benefits, eligibility). Answers are powered by the ENBD QNA Bot and based on content from Emirates NBD&apos;s official website.
            </div>
          )}
          {messages.map((m, i) => (
            <div key={i} className={m.role === "user" ? styles.userMsg : styles.assistantMsg}>
              <div className={styles.bubble}>
                {m.role === "assistant" ? (
                  <ReactMarkdown
                    rehypePlugins={[rehypeSanitize]}
                    remarkPlugins={[remarkGfm]}
                    components={{
                      a: ({node, ...props}) => (
                        // open links in new tab and preserve rel
                        <a {...props} target="_blank" rel="noopener noreferrer" />
                      ),
                    }}
                  >
                    {m.content}
                  </ReactMarkdown>
                ) : (
                  <>{m.content}</>
                )}
              </div>
              {m.sources && m.sources.length > 0 && (
                <div className={styles.sources}>
                  Sources: {m.sources.map((s, j) => (
                    <a key={j} href={s} target="_blank" rel="noopener noreferrer">{s}</a>
                  ))}
                </div>
              )}
            </div>
          ))}
          {loading && <div className={styles.assistantMsg}><div className={styles.bubble}>...</div></div>}
          {error && <div className={styles.error}>{error}</div>}
          <div ref={bottomRef} />
        </div>

        <footer className={styles.disclaimer}>
          This bot provides information based on website content only. It does not offer financial advice.
        </footer>

        <form className={styles.form} onSubmit={handleSubmit}>
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Ask a question..."
            disabled={loading}
            className={styles.input}
          />
          <button type="submit" disabled={loading} className={styles.button}>Send</button>
        </form>
      </main>
    </div>
  );
}
