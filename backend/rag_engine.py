from typing import List, Dict
import numpy as np
from rank_bm25 import BM25Okapi


class HybridRunbookRetriever:
    """
    Stage-1 Hybrid Retriever (BM25 + RRF Readiness) for enterprise architecture runbooks
    and Visual Studio repository context. Ready to expand in Stage 3.
    """
    def __init__(self):
        self.documents: List[Dict[str, str]] = [
            {
                "id": "DOC-AZ-01",
                "title": "Azure Function App & Service Bus Architecture Runbook",
                "content": (
                    "Production Azure Function Apps use Managed Identity for Azure Service Bus "
                    "and Azure Blob Storage connections. Timeout exceptions or dead-letter queue spikes "
                    "commonly occur when maxConcurrentCalls exceeds connection pool limits or lock renewal fails."
                ),
            },
            {
                "id": "DOC-AWS-01",
                "title": "AWS Lambda, SQS & S3 Event Processing Runbook",
                "content": (
                    "AWS Lambda handlers triggered by S3 ObjectCreated events or SQS queues must implement "
                    "idempotency keys in DynamoDB/Redis and configure visibility timeouts at least 6x the function timeout."
                ),
            },
            {
                "id": "DOC-API-01",
                "title": "Microservice Payload Validation & Auth Standards",
                "content": (
                    "Backend services enforce strict Pydantic / System.Text.Json payload validation, RBAC metadata "
                    "headers, and exponential backoff retry policies on downstream HTTP/MQTT calls."
                ),
            },
        ]
        tokenized_corpus = [doc["content"].lower().split() for doc in self.documents]
        self.bm25 = BM25Okapi(tokenized_corpus)

    def search(self, query: str, top_k: int = 2) -> str:
        tokens = query.lower().split()
        scores = self.bm25.get_scores(tokens)
        ranked_indices = np.argsort(scores)[::-1][:top_k]
        results = [f"[{self.documents[i]['id']}] {self.documents[i]['title']}: {self.documents[i]['content']}" for i in ranked_indices]
        return "\n".join(results)


hybrid_retriever = HybridRunbookRetriever()
