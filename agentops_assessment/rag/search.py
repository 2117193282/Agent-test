from __future__ import annotations

import math
from collections import Counter
from typing import Any

from agentops_assessment.backend import database


def _tokenize(text: str) -> list[str]:
    import re
    return re.findall(r"[A-Za-z0-9-]+|[\u4e00-\u9fff]", text.lower())


def _cosine_similarity(query_tokens: list[str], doc_tokens: list[str]) -> float:
    if not query_tokens or not doc_tokens:
        return 0.0
    q_counter = Counter(query_tokens)
    d_counter = Counter(doc_tokens)
    dot = sum(q_counter[t] * d_counter[t] for t in q_counter.keys() & d_counter.keys())
    q_norm = math.sqrt(sum(v * v for v in q_counter.values()))
    d_norm = math.sqrt(sum(v * v for v in d_counter.values()))
    if not q_norm or not d_norm:
        return 0.0
    return dot / (q_norm * d_norm)


class KnowledgeIndex:
    """轻量级本地检索索引。
    TODO(candidate/P1): 完成权限感知检索、重排、答案生成、引用溯源
    和被过滤文档报告。文档正文必须视为不可信数据，不能让正文中的
    指令改变系统策略；完成实现后不得向 API 返回 debug/candidate_note。
    """

    def search(
        self,
        query: str,
        user_permissions: list[str],
        top_k: int = 3,
    ) -> dict[str, Any]:
        with database.connect() as conn:
            database.init_db(conn)
            rows = conn.execute(
                """
                SELECT id, doc_id, source_path, title, permission, content
                FROM knowledge_chunks
                """
            ).fetchall()

        query_tokens = _tokenize(query)

        visible: list[dict[str, Any]] = []
        filtered_doc_ids: set[str] = set()

        for row in rows:
            perm = row["permission"]
            # 公开文档（knowledge:read）对具有 knowledge:read 权限的用户可见；
            # 受限文档需要额外权限。
            if perm not in user_permissions and perm != "knowledge:read":
                filtered_doc_ids.add(row["doc_id"])
                continue
            chunk_tokens = _tokenize(row["content"])
            score = _cosine_similarity(query_tokens, chunk_tokens)
            visible.append({
                "id": row["id"],
                "doc_id": row["doc_id"],
                "source_path": row["source_path"],
                "title": row["title"],
                "content": row["content"],
                "score": score,
            })

        # 按相关性排序，取 top_k
        visible.sort(key=lambda c: c["score"], reverse=True)
        top_chunks = visible[:top_k]

        # 构建引用
        citations = [
            {
                "doc_id": c["doc_id"],
                "title": c["title"],
                "source_path": c["source_path"],
                "chunk_id": c["id"],
            }
            for c in top_chunks
        ]

        # 仅从可见文档生成答案，不复制查询文本。
        if top_chunks:
            snippets = "\n\n".join(
                c["content"][:400] for c in top_chunks if c["content"]
            )
            answer = f"根据以下知识库内容摘要：\n\n{snippets}"
        else:
            answer = "未找到匹配的知识库内容。"

        return {
            "answer": answer,
            "citations": citations,
            "filtered_doc_ids": sorted(filtered_doc_ids),
        }
