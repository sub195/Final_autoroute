import pickle
from sentence_transformers import util
from config import container_client, embeddings_client


def test_kb_query(domain: str, query: str, top_k: int = 3):
    """
    Downloads the .pkl for a given domain from Azure Blob
    and searches it with a query string.
    """
    blob_name = f"faiss_indexes/{domain}.pkl"
    blob_client = container_client.get_blob_client(blob_name)

    if not blob_client.exists():
        print(f"❌ No PKL found for domain '{domain}' at {blob_name}")
        return

    # Download pickle
    blob_bytes = blob_client.download_blob().readall()
    data = pickle.loads(blob_bytes)

    chunks = data["chunks"]
    embeddings = data["embeddings"]

    # Embed the query
    query_embedding = embeddings_client.embed_query(query)

    # Compute cosine similarities
    cos_scores = util.cos_sim(query_embedding, embeddings)[0]
    top_results = cos_scores.topk(top_k)

    print(f"\n🔎 Query: {query}")
    print(f"📂 Domain: {domain}")
    print("Top matches:")

    found = False
    for score, idx in zip(top_results[0], top_results[1]):
        print(f"  [Score {score:.4f}] {chunks[idx]}")
        found = True

    if not found:
        print("⚠️ No relevant matches found.")


if __name__ == "__main__":
    # Example test queries
    test_kb_query("loans", "How can I apply for a loan?")
    test_kb_query("cards", "What benefits do credit cards provide?")
    test_kb_query("digital_banking", "How do I reset my online banking password?")
