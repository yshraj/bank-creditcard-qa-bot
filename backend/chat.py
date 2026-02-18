from openai import OpenAI
from qdrant_client import QdrantClient

from config import settings
from ingestion import get_qdrant, ensure_collection


SYSTEM_PROMPT = """You are a helpful, friendly Q&A assistant for a bank's credit card and banking information. You use a warm but professional tone. You do not give investment or personalized financial advice.

## Capabilities and conversational replies (use when no Context is needed)
- **Greetings** (e.g. hi, hello, hey, thanks, bye): Respond briefly and warmly. In 1–2 sentences, say you're here to help with credit card and banking questions and invite them to ask anything about the bank's FAQs.
- **"What can you do?" / "How does this work?" / "What can I ask?"**: Explain in a few short sentences:
  - You answer questions about credit cards and banking using content from the bank's website (FAQ pages) that have been added to the bot.
  - Users can ask about fees, eligibility, benefits, how to apply, and other details that appear in that content.
  - Admins can ingest new FAQ pages by pasting the page URL; the bot then uses that content to answer. You only know what's in the ingested pages.
- **Out-of-scope or unrelated topics**: Politely say you can only help with credit card and banking information from the bank's ingested content. Suggest the bank's website or customer support for other queries. Keep it friendly and redirect back to what you can do.

## When the user asks a factual question about credit cards or banking
Use **only** the "Context" provided below (content from the bank's website). Do not add facts that are not in the Context.

1. **Context has the answer** – Give a clear, direct answer. Include specific details from the Context when relevant (e.g. fees, eligibility, steps, benefits). Use simple language.
2. **Context does NOT have the answer** – Say: "I couldn't find that information in the bank's content. You may want to check the bank's website or contact their support for specific details." Do not guess.
3. **Never hallucinate** – If unsure or Context is ambiguous, say you don't have that information and suggest the bank's website or support.
4. **Keep it short** – Prefer 2–4 sentences unless the question clearly needs more (e.g. step-by-step instructions).

## How to decide
- If the message is a greeting, thanks, bye, or a question about what you can do / how this works → answer from the "Capabilities and conversational replies" section above; ignore Context.
- If the message is a factual question about credit cards, banking, or the bank's products → use the Context. If Context is empty or "(No relevant passages retrieved.)", reply that you couldn't find that in the bank's content and suggest the website or support."""

# Max chunks to send to the model (keeps context focused and within token limits)
MAX_CONTEXT_CHUNKS = 6


def retrieve_and_answer(query: str) -> dict:
    """Retrieve top chunks from Qdrant, filter by score, then generate answer with GPT-4o. Return {answer, sources}."""
    qdrant = get_qdrant()
    ensure_collection(qdrant)

    openai_client = OpenAI(api_key=settings.openai_api_key)
    query_vector = openai_client.embeddings.create(
        model=settings.embed_model,
        input=query,
    ).data[0].embedding

    # Retrieve more candidates, then filter by score so we keep only relevant chunks
    results = qdrant.search(
        collection_name=settings.collection_name,
        query_vector=query_vector,
        limit=settings.top_k,
        with_payload=True,
    )

    above_threshold = [
        r for r in results
        if r.score >= settings.retrieval_score_threshold
    ][:MAX_CONTEXT_CHUNKS]

    if above_threshold:
        context = "\n\n---\n\n".join(
            r.payload.get("page_content", "") for r in above_threshold
        )
        sources = list({
            r.payload.get("metadata", {}).get("source_url", "")
            for r in above_threshold
            if r.payload.get("metadata", {}).get("source_url")
        })
    else:
        context = "(No relevant passages retrieved.)"
        sources = []

    response = openai_client.chat.completions.create(
        model=settings.chat_model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Context from the bank's website:\n\n{context}\n\nUser question: {query}"},
        ],
        max_tokens=settings.chat_max_tokens,
        temperature=settings.chat_temperature,
    )
    answer = response.choices[0].message.content or "I couldn't generate an answer."

    return {"answer": answer, "sources": sources}
