# backend/tools/knowledge_base.py
import pickle
from collections import OrderedDict
from sentence_transformers import util
from config import container_client, embeddings_client

def _meta_get(d: dict, *keys, default=None):
    """Return the first present non-empty value from the provided keys."""
    for k in keys:
        v = d.get(k)
        if v is not None and str(v).strip() != "":
            return v
    return default

def search_knowledge_base(query: str, domain: str, k: int = 5, min_score: float = 0.55) -> str:
    """
    Search KB chunks (stored in Blob as faiss_indexes/{domain}.pkl) using cosine similarity.
    Returns an answer with a de-duplicated 'Sources' list grouped by (pdf, page).
    Works even if metas are missing (then 'Sources' is omitted).

    Expected pickle structure:
      {
        "chunks": List[str] or List[dict{"text": "..."}],
        "embeddings": List[List[float]],
        "metas": Optional[List[dict]] or "metadata": Optional[List[dict]]
      }
    """
    print(f"--- TOOL: Searching Knowledge Base for domain '{domain}' with query '{query}' ---")
    try:
        blob_name = f"faiss_indexes/{domain}.pkl"
        blob_client = container_client.get_blob_client(blob_name)
        if not blob_client.exists():
            return f"Error: Knowledge base for domain '{domain}' not found."

        data = pickle.loads(blob_client.download_blob().readall())
        raw_chunks = data.get("chunks", [])
        embeddings = data.get("embeddings", [])
        metas = data.get("metas") or data.get("metadata")  # optional list[dict]

        if not raw_chunks or not embeddings:
            return "Knowledge base is empty or malformed for this domain."

        # Normalizing chunks -> List[str]
        chunks = []
        for c in raw_chunks:
            if isinstance(c, dict):
                chunks.append(str(_meta_get(c, "text", "chunk", default="")).strip())
            else:
                chunks.append(str(c).strip())

        # Embed query & score
        q_emb = embeddings_client.embed_query(query)
        scores = util.cos_sim(q_emb, embeddings)[0]  # tensor [N]
        top_scores, top_idx = scores.topk(min(k, len(chunks)))

        # Collect hits (text + meta), keep unique snippets
        seen_texts = set()
        answer_lines = []
        hit_metas = []

        for s, idx in zip(top_scores, top_idx):
            score = float(s.item())
            if score < min_score:
                continue

            text = chunks[idx]
            if not text:
                continue

            # Deduplicate by normalized text
            norm = " ".join(text.split()).lower()
            if norm in seen_texts:
                continue
            seen_texts.add(norm)

            # Keeping chunks readable
            snippet = text[:600].rstrip() + (" …" if len(text) > 600 else "")
            answer_lines.append(f"- {snippet}")

            # Tracking metas if available
            if metas and idx < len(metas) and isinstance(metas[idx], dict):
                hit_metas.append(metas[idx])

        if not answer_lines:
            return "No relevant information found in the knowledge base for this query."

        # Build Sources (only if we have metas)
        src_block = ""
        if hit_metas:
            # Group by (pdf, page), merging sections
            grouped = OrderedDict()  # (pdf, page) -> set(sections)
            for m in hit_metas:
                pdf = _meta_get(m, "pdf", "document", "source", "file", "filename", default="KB.pdf")
                page = _meta_get(m, "page", "page_number", "pageIndex", "page_no", default="?")
                section = str(_meta_get(m, "section", "heading", "title", "h2", default="")).strip()
                key = (str(pdf), str(page))
                if key not in grouped:
                    grouped[key] = set()
                if section:
                    grouped[key].add(section)

            lines = []
            for i, ((pdf, page), sections) in enumerate(grouped.items(), start=1):
                if sections:
                    sec_list = sorted(sections)
                    clip = ", ".join(sec_list[:3])
                    if len(sec_list) > 3:
                        clip += " …"
                    lines.append(f"{i}) {pdf} — p.{page} — {clip}")
                else:
                    lines.append(f"{i}) {pdf} — p.{page}")

            if lines:
                src_block = "\n\nSources:\n" + "\n".join(lines)

        return "Found the following relevant information:\n" + "\n".join(answer_lines) + src_block

    except Exception as e:
        print(f"Error in knowledge base search: {e}")
        return "An error occurred while searching the knowledge base."







