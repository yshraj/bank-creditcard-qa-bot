from openai import OpenAI
from qdrant_client import QdrantClient

from config import settings
from ingestion import get_qdrant, ensure_collection


SYSTEM_PROMPT = """You are an informational assistant for a bank's credit card and banking Q&A bot. You answer only using the provided context from the bank's website. You do not offer investment or personalized financial advice.

Rules:
- Base your answer strictly on the context below. Use the most relevant parts of the context; you may combine information from multiple passages.
- If the context does not contain the answer, say clearly that you do not have that information and suggest checking the bank's website or contacting support.
- Do not make up information. If unsure, say you don't know.
- Give concise, direct answers. Include relevant details (fees, benefits, eligibility, steps) when they appear in the context.
- This is informational only, based on the bank's website content."""

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
        max_tokens=600,
    )
    answer = response.choices[0].message.content or "I couldn't generate an answer."

    return {"answer": answer, "sources": sources}
