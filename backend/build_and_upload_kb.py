# backend/build_and_upload_kb.py
import os
import sys
import pickle
import math
import fitz  # PyMuPDF
from typing import List, Dict, Any, Tuple
from pathlib import Path

from langchain.text_splitter import RecursiveCharacterTextSplitter

# Ensuring we can import config.py even if run from elsewhere
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from config import embeddings_client, container_client  # uses your .env

# -------------------
# CONFIGURING (edit paths)
# -------------------
# Keys MUST match the "domain" values that we use in graph.py's agent registry.
PDF_FILES = {
    "account_management": "pdfs/Account_SOP.pdf",
    "cards": "pdfs/Card_SOP.pdf",
    "complaints": "pdfs/Complaints___Escalations_SOPs.pdf",
    "digital_banking": "pdfs/Digital_Banking_SOP.pdf",
    "loans_EMI": "pdfs/Loan___EMI_SOP.pdf",
    "regulatory_compliance": "pdfs/Regulatory___Compliance_SOPs.pdf",
}

BLOB_FOLDER = "faiss_indexes"

# Chunking params 
CHUNK_SIZE = 800
CHUNK_OVERLAP = 200

# Batch size for embeddings
EMBED_BATCH_SIZE = 64


def log(msg: str):
    print(f"[KB] {msg}")


def extract_pages(pdf_path: str) -> List[Tuple[int, str]]:
    """
    Return list of (1-indexed page_number, page_text).
    """
    doc = fitz.open(pdf_path)
    out = []
    for i, page in enumerate(doc, start=1):
        txt = page.get_text("text") or ""
        txt = txt.strip()
        out.append((i, txt))
    return out


def guess_section_label(page_text: str) -> str:
    """
    Heuristic: take the first non-empty line that looks like a heading.
    - Prefer short lines (<= 80 chars)
    - Prefer lines with many capital letters or Title Case
    Fallback: first non-empty line.
    """
    lines = [l.strip() for l in page_text.splitlines() if l.strip()]
    if not lines:
        return ""

    def is_heading(s: str) -> bool:
        if len(s) > 80:
            return False
        # uppercase ratio or title-ish
        letters = [c for c in s if c.isalpha()]
        upp = sum(1 for c in letters if c.isupper())
        ratio = (upp / max(1, len(letters)))
        titleish = s.istitle()
        return ratio > 0.6 or titleish

    for l in lines[:10]:  # only scanning first few lines; headings are usually near top
        if is_heading(l):
            return l[:80]
    return lines[0][:80]


def chunk_page(page_text: str) -> List[str]:
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        separators=["\n\n", "\n", " ", ""],
    )
    return splitter.split_text(page_text or "")


def embed_chunks_batched(chunks: List[str], batch_size: int = EMBED_BATCH_SIZE) -> List[List[float]]:
    """
    Use embeddings_client.embed_documents for efficient batch embedding.
    Falls back to per-chunk embed_query if needed.
    """
    embeddings = []
    try:
        for i in range(0, len(chunks), batch_size):
            batch = chunks[i : i + batch_size]
            batch_emb = embeddings_client.embed_documents(batch)  # List[List[float]]
            embeddings.extend(batch_emb)
            log(f"   → embedded batch {i//batch_size + 1}/{math.ceil(len(chunks)/batch_size)}")
    except Exception as e:
        log(f"   ⚠️ batch embedding failed ({e}), falling back to per-chunk")
        embeddings = [embeddings_client.embed_query(c) for c in chunks]
    return embeddings


def build_domain_pickle(domain: str, pdf_path: str) -> Dict[str, Any]:
    """
    Create dict: {"chunks": [...], "embeddings": [...], "metas": [...]}
    metas[i] = {"pdf": <basename>, "page": <int>, "section": <str>}
    """
    log(f"📄 Processing domain='{domain}' from '{pdf_path}'")

    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"File not found: {pdf_path}")

    pages = extract_pages(pdf_path)
    if not pages:
        raise ValueError("No text extracted")

    all_chunks: List[str] = []
    all_metas: List[Dict[str, Any]] = []
    pdf_basename = Path(pdf_path).name

    for page_num, page_text in pages:
        if not page_text.strip():
            continue
        section = guess_section_label(page_text)
        chunks = chunk_page(page_text)
        for ch in chunks:
            all_chunks.append(ch)
            all_metas.append({
                "pdf": pdf_basename,
                "page": page_num,
                "section": section,
            })

    if not all_chunks:
        raise ValueError("No chunks created")

    log(f"   → {len(all_chunks)} chunks")
    all_embeddings = embed_chunks_batched(all_chunks)
    if not all_embeddings or len(all_embeddings) != len(all_chunks):
        raise RuntimeError(f"Embedding count mismatch (got {len(all_embeddings)} for {len(all_chunks)})")

    log(f"   → {len(all_embeddings)} embeddings")
    return {"chunks": all_chunks, "embeddings": all_embeddings, "metas": all_metas}


def upload_pickle(domain: str, data: Dict[str, Any]):
    blob_name = f"{BLOB_FOLDER}/{domain}.pkl"
    log(f"Uploading → container='{container_client.container_name}', blob='{blob_name}'")
    container_client.upload_blob(blob_name, pickle.dumps(data), overwrite=True)
    log(f"✅ Uploaded {blob_name}")


def main():
    log(f"Target container: '{container_client.container_name}'")
    processed = 0

    for domain, pdf_path in PDF_FILES.items():
        try:
            data = build_domain_pickle(domain, pdf_path)
        except Exception as e:
            log(f"❌ Skipping domain '{domain}': {e}")
            continue

        try:
            upload_pickle(domain, data)
            processed += 1
        except Exception as e:
            log(f"❌ Upload failed for domain '{domain}': {e}")

    if processed == 0:
        log("❌ No domains processed. Check your file paths and names.")
    else:
        log(f"🎉 Done. Uploaded KBs with metadata for {processed} domain(s).")


if __name__ == "__main__":
    main()



















# # backend/build_and_upload_kb.py
# """
# Build chunk embeddings (dict of {'chunks', 'embeddings'}) from local PDFs
# and upload them to Azure Blob Storage at: faiss_indexes/<domain>.pkl

# This matches tools/knowledge_base.py which loads a dict and does cosine similarity.
# # """


# # backend/build_and_upload_kb.py
# import os
# import sys
# import pickle
# import math
# import fitz  # PyMuPDF
# from langchain.text_splitter import RecursiveCharacterTextSplitter

# # Ensure we can import config.py even if run from elsewhere
# sys.path.append(os.path.dirname(os.path.abspath(__file__)))
# from config import embeddings_client, container_client  # uses your .env

# # CONFIG
# # PDF_FILES = {
# #     "account_management": "pdfs/Account_SOP.pdf",
# #     "cards": "pdfs/Card_SOP.pdf",
# #     "complaints": "pdfs/Complaints___Escalations_SOPs.pdf",
# #     "digital_banking": "pdfs/Digital_Banking_SOP.pdf",
# #     "loans_EMI": "pdfs/Loan___EMI_SOP.pdf",
# #     "regulatory_compliance": "pdfs/Regulatory___Compliance_SOPs.pdf",
# # }

# PDF_FILES = {
#     "account_management": "pdfs/Account_SOP.pdf",
#     "cards": "pdfs/Card_SOP.pdf",
#     "complaints": "pdfs/Complaints___Escalations_SOPs.pdf",
#     "digital_banking": "pdfs/Digital_Banking_SOP.pdf",
#     "loans_EMI": "pdfs/Loan___EMI_SOP.pdf",                 # <-- if your graph uses "loans_EMI"
#     "regulatory_compliance": "pdfs/Regulatory___Compliance_SOPs.pdf",
# }



# BLOB_FOLDER = "faiss_indexes"

# CHUNK_SIZE = 800
# CHUNK_OVERLAP = 200
# EMBED_BATCH_SIZE = 64

# def log(msg: str):
#     print(f"[KB] {msg}")

# def pdf_pages(path: str):
#     """Yield (page_index, page_text, section_hint) for each page."""
#     doc = fitz.open(path)
#     for i in range(len(doc)):
#         text = (doc[i].get_text("text") or "").strip()
#         # A lightweight "section" hint: the first non-empty, shortish line
#         section_hint = ""
#         for line in text.splitlines():
#             s = line.strip()
#             if s:
#                 section_hint = s[:120]
#                 break
#         yield i, text, section_hint

# def chunk_page_text(page_text: str):
#     splitter = RecursiveCharacterTextSplitter(
#         chunk_size=CHUNK_SIZE,
#         chunk_overlap=CHUNK_OVERLAP,
#         separators=["\n\n", "\n", " ", ""],
#     )
#     return splitter.split_text(page_text)

# def embed_chunks_batched(chunks, batch_size=EMBED_BATCH_SIZE):
#     embeddings = []
#     try:
#         for i in range(0, len(chunks), batch_size):
#             batch = chunks[i : i + batch_size]
#             batch_emb = embeddings_client.embed_documents(batch)
#             embeddings.extend(batch_emb)
#             log(f"   → embedded batch {i//batch_size + 1}/{math.ceil(len(chunks)/batch_size)}")
#     except Exception as e:
#         log(f"   ⚠️ batch embedding failed ({e}), falling back to per-chunk embedding")
#         embeddings = [embeddings_client.embed_query(c) for c in chunks]
#     return embeddings

# def upload_pickle(domain: str, data: dict):
#     blob_name = f"{BLOB_FOLDER}/{domain}.pkl"
#     log(f"Uploading → container='{container_client.container_name}', blob='{blob_name}'")
#     container_client.upload_blob(blob_name, pickle.dumps(data), overwrite=True)
#     log(f"✅ Uploaded {blob_name}")

# def main():
#     log(f"Target container: '{container_client.container_name}'")
#     processed = 0

#     for domain, pdf_path in PDF_FILES.items():
#         if not os.path.exists(pdf_path):
#             log(f"⚠️ Skipping domain '{domain}': file not found '{pdf_path}'")
#             continue

#         log(f"📄 Processing domain='{domain}' from '{pdf_path}'")
#         all_chunks, all_metas = [], []

#         for page_idx, page_text, page_hint in pdf_pages(pdf_path):
#             if not page_text:
#                 continue
#             page_chunks = chunk_page_text(page_text)
#             if not page_chunks:
#                 continue
#             for ch in page_chunks:
#                 all_chunks.append(ch)
#                 # 1-based page number for humans
#                 all_metas.append({
#                     "pdf": os.path.basename(pdf_path),
#                     "page": page_idx + 1,
#                     # if the chunk starts with a nice line, use it, else fallback to page_hint
#                     "section": (ch.splitlines()[0].strip()[:120] if ch.splitlines() else page_hint)
#                 })

#         if not all_chunks:
#             log(f"⚠️ No chunks for '{domain}'. Skipping.")
#             continue

#         log(f"   → total chunks: {len(all_chunks)}")
#         embs = embed_chunks_batched(all_chunks)
#         if not embs or len(embs) != len(all_chunks):
#             log(f"❌ Embedding count mismatch for '{domain}'.")
#             continue

#         data = {
#             "chunks": all_chunks,
#             "embeddings": embs,
#             "metas": all_metas,  # NEW
#         }
#         upload_pickle(domain, data)
#         processed += 1

#     if processed == 0:
#         log("❌ No domains processed.")
#     else:
#         log(f"🎉 Done. Uploaded KBs for {processed} domain(s).")

# if __name__ == "__main__":
#     main()








