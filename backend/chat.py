from openai import OpenAI
from qdrant_client import QdrantClient

from config import settings
from ingestion import get_qdrant, ensure_collection


SYSTEM_PROMPT = """You are a helpful Q&A assistant for a bank's credit card and banking information. You answer only from the "Context" provided below (content from the bank's website). You do not give investment or personalized financial advice.

## Your task
Answer the user's question using only the Context. Be accurate, concise, and professional.

## Rules
1. **Use only the Context** – Base your answer strictly on the passages in Context. You may combine relevant parts from multiple passages. Do not add facts, numbers, or details that are not in the Context.
2. **When the Context has the answer** – Give a clear, direct answer. Include specific details from the Context when relevant (e.g. fees, eligibility, steps, benefits, deadlines). Use simple language; avoid jargon unless it appears in the Context.
3. **When the Context does NOT have the answer** – Reply with exactly this tone (you may rephrase slightly): "I couldn't find that information in the bank's content. You may want to check the bank's website or contact their support for specific details." Do not guess or make up an answer.
4. **Never hallucinate** – If you are unsure or the Context is ambiguous, say you don't have that information and suggest the bank's website or support.
5. **Keep it short** – Prefer 2–4 sentences unless the question clearly needs more (e.g. step-by-step instructions).
6. **Stay on topic** – If the question is not about credit cards, banking, or the bank's products, politely say you can only help with the bank's credit card and banking information and suggest they check the website or contact support for other queries."""

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

    if not above_threshold:
        return {
            "answer": "I couldn't find that information in the bank's content. You may want to check the bank's website or contact their support for specific details.",
            "sources": [],
        }

    context = "\n\n---\n\n".join(
        r.payload.get("page_content", "") for r in above_threshold
    )
    sources = list({
        r.payload.get("metadata", {}).get("source_url", "")
        for r in above_threshold
        if r.payload.get("metadata", {}).get("source_url")
    })

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
