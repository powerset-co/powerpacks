"""Local DuckDB-backed search store for Powerpacks primitives.

This module deliberately uses top-level imports that work when
``packs/search/primitives/lib`` is added directly to ``sys.path``.  DuckDB is
imported lazily by ``LocalDuckDBSearchStore`` so normal TurboPuffer mode does not
require the local backend dependency to be importable at module import time.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from local_filter_eval import filter_rows


K_RRF = 60
TOKEN_RE = re.compile(r"[a-z0-9]+")
_STEMMER: Any | None = None


class LocalDuckDBError(RuntimeError):
    """Clear local-backend runtime error."""


@dataclass
class LocalQueryResponse:
    rows: list[Any]


class LocalQueryRow:
    """TurboPuffer-compatible lightweight row object."""

    def __init__(self, row_id: Any, attrs: dict[str, Any] | None = None):
        self.id = str(row_id)
        self.model_extra = dict(attrs or {})

    def __getattr__(self, name: str) -> Any:
        if name in self.model_extra:
            return self.model_extra[name]
        raise AttributeError(name)

    def __repr__(self) -> str:
        return f"LocalQueryRow(id={self.id!r}, model_extra={self.model_extra!r})"


class LocalDuckDBNamespace:
    def __init__(self, store: "LocalDuckDBSearchStore", logical_name: str):
        self.store = store
        self.logical_name = logical_name

    def query(self, *, rank_by: Any = None, filters: Any = None, top_k: int = 10, include_attributes: list[str] | None = None, **_: Any) -> LocalQueryResponse:
        return self.store.query_namespace(self.logical_name, rank_by, filters, top_k, include_attributes or [])

    def multi_query(self, *, queries: list[dict[str, Any]], **_: Any) -> Any:
        results = [
            self.query(
                rank_by=query.get("rank_by"),
                filters=query.get("filters"),
                top_k=int(query.get("top_k") or 10),
                include_attributes=list(query.get("include_attributes") or []),
            )
            for query in queries
        ]
        return type("LocalMultiQueryResponse", (), {"results": results})()


class LocalDuckDBSearchStore:
    NAMESPACE_TABLES = {
        "people": "local_people_positions",
        "summaries": "local_summaries",
        "education": "local_people_education",
        "schools": "local_education",
    }

    def __init__(self, db_path: str, *, read_only: bool = True):
        try:
            import duckdb  # type: ignore
        except ModuleNotFoundError as exc:
            raise RuntimeError("duckdb is required for local DuckDB search") from exc

        self.db_path = str(db_path)
        self.read_only = read_only
        self.conn = duckdb.connect(self.db_path, read_only=read_only)

    def namespace(self, logical_name: str) -> LocalDuckDBNamespace:
        self._table_for_namespace(logical_name)
        return LocalDuckDBNamespace(self, logical_name)

    def _table_for_namespace(self, logical_name: str) -> str:
        table = self.NAMESPACE_TABLES.get(str(logical_name))
        if not table:
            supported = ", ".join(sorted(self.NAMESPACE_TABLES))
            raise LocalDuckDBError(f"unknown local DuckDB namespace {logical_name!r}; supported namespaces: {supported}")
        if not self._table_exists(table):
            raise LocalDuckDBError(f"local DuckDB namespace {logical_name!r} requires missing table {table!r}")
        return table

    def _table_exists(self, table: str) -> bool:
        row = self.conn.execute(
            "select count(*) from information_schema.tables where table_schema in ('main', 'temp') and table_name = ?",
            [table],
        ).fetchone()
        return bool(row and row[0])

    def _rows_for_namespace(self, logical_name: str) -> list[dict[str, Any]]:
        table = self._table_for_namespace(logical_name)
        try:
            rows = self.conn.execute(f"select * from {table}").fetchall()
            columns = [desc[0] for desc in self.conn.description or []]
        except Exception as exc:
            raise LocalDuckDBError(f"failed reading local DuckDB table {table!r} for namespace {logical_name!r}: {exc}") from exc
        return [self._normalize_row(dict(zip(columns, row))) for row in rows]

    def _normalize_row(self, row: dict[str, Any]) -> dict[str, Any]:
        return {key: self._normalize_value(value) for key, value in row.items()}

    def _normalize_value(self, value: Any) -> Any:
        if hasattr(value, "tolist"):
            try:
                value = value.tolist()
            except Exception:
                pass
        if isinstance(value, tuple):
            return [self._normalize_value(item) for item in value]
        if isinstance(value, list):
            return [self._normalize_value(item) for item in value]
        if isinstance(value, dict):
            return {str(key): self._normalize_value(item) for key, item in value.items()}
        if isinstance(value, str):
            text = value.strip()
            if text and text[0] in "[{" and text[-1] in "]}":
                try:
                    return self._normalize_value(json.loads(text))
                except Exception:
                    return value
        return value

    def _row_id(self, row: dict[str, Any]) -> str:
        for field in ("id", "position_id", "person_id", "canonical_education_id", "base_id"):
            if row.get(field) is not None:
                return str(row[field])
        raise LocalDuckDBError("local DuckDB row is missing id/position_id/person_id/canonical_education_id/base_id")

    def _project_row_object(self, row: dict[str, Any], include_attributes: list[str]) -> LocalQueryRow:
        row_id = self._row_id(row)
        attrs = {key: row.get(key) for key in include_attributes if key in row}
        if "score" in row:
            attrs["score"] = row.get("score")
        return LocalQueryRow(row_id, attrs)

    def _project_dict(self, row: dict[str, Any], include_attributes: list[str]) -> dict[str, Any]:
        out = {"id": self._row_id(row)}
        for key in include_attributes:
            if key in row:
                out[key] = row[key]
        return out

    def _filtered_rows(self, logical_name: str, filters: Any) -> list[dict[str, Any]]:
        return filter_rows(self._rows_for_namespace(logical_name), filters)

    def filter_only_rows_for_namespace(
        self,
        logical_name: str,
        filters: Any,
        include_attributes: list[str],
        page_size: int = 10000,
        max_results: int = 0,
    ) -> list[dict[str, Any]]:
        rows = sorted(self._filtered_rows(logical_name, filters), key=lambda row: self._row_id(row))
        if max_results and max_results > 0:
            rows = rows[:max_results]
        return [self._project_dict(row, include_attributes) for row in rows]

    def query_namespace(
        self,
        logical_name: str,
        rank_by: Any,
        filters: Any,
        top_k: int,
        include_attributes: list[str],
    ) -> LocalQueryResponse:
        rows = self._filtered_rows(logical_name, filters)
        ranked = self._rank_rows(logical_name, rows, rank_by)
        if top_k and top_k > 0:
            ranked = ranked[:top_k]
        return LocalQueryResponse([self._project_row_object(row, include_attributes) for row in ranked])

    def _with_score(self, row: dict[str, Any], score: float) -> dict[str, Any]:
        out = dict(row)
        out["score"] = score
        return out

    def _rank_rows(self, logical_name: str, rows: list[dict[str, Any]], rank_by: Any) -> list[dict[str, Any]]:
        if not rank_by:
            if logical_name == "schools":
                return sorted(rows, key=lambda row: (-float(row.get("person_count") or 0), self._row_id(row)))
            return sorted(rows, key=lambda row: self._row_id(row))

        if isinstance(rank_by, (list, tuple)) and len(rank_by) == 2:
            field, direction = str(rank_by[0]), str(rank_by[1]).lower()
            reverse = direction == "desc"
            return sorted(rows, key=lambda row: (row.get(field) is None, row.get(field), self._row_id(row)), reverse=reverse)

        if isinstance(rank_by, (list, tuple)) and len(rank_by) >= 3:
            field, operator, value = str(rank_by[0]), str(rank_by[1]), rank_by[2]
            if operator == "BM25":
                scored = self._bm25_rank(rows, field, value)
                return [self._with_score(row, score) for row, score in scored]
            if operator == "kNN":
                scored = self._vector_rank(rows, field, value)
                return [self._with_score(row, score) for row, score in scored]

        raise ValueError(f"unsupported local DuckDB rank_by expression: {rank_by!r}")

    def _stem_words(self, words: list[str]) -> list[str]:
        global _STEMMER
        try:
            if _STEMMER is None:
                import snowballstemmer  # type: ignore

                _STEMMER = snowballstemmer.stemmer("english")
            return [_STEMMER.stemWord(word) for word in words]
        except Exception:
            return words

    def _phrase_query_tokens(self, query: str) -> list[str]:
        words = TOKEN_RE.findall(str(query).lower())
        stems = self._stem_words(words)
        return [" ".join(stems)] if stems else []

    def _word_query_tokens(self, query: str) -> list[str]:
        words = TOKEN_RE.findall(str(query).lower())
        result = list(words)
        for index in range(len(words) - 1):
            result.append(f"{words[index]} {words[index + 1]}")
        return result

    def _tokens_for_value(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value.lower()] if " " in value.strip() else TOKEN_RE.findall(value.lower())
        if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, dict)):
            tokens: list[str] = []
            for item in value:
                if isinstance(item, str):
                    text = item.strip().lower()
                    tokens.extend([text] if " " in text else TOKEN_RE.findall(text))
                else:
                    tokens.extend(TOKEN_RE.findall(str(item).lower()))
            return tokens
        return TOKEN_RE.findall(str(value).lower())

    def _bm25_rank(self, rows: list[dict[str, Any]], field: str, query: Any) -> list[tuple[dict[str, Any], float]]:
        query_tokens = self._tokens_for_value(query)
        if not query_tokens or not rows:
            return []
        docs = [self._tokens_for_value(row.get(field)) for row in rows]
        avgdl = sum(len(doc) for doc in docs) / max(1, len(docs))
        df: Counter[str] = Counter()
        for doc in docs:
            for token in set(doc):
                df[token] += 1
        query_counts = Counter(query_tokens)
        k1 = 1.2
        b = 0.75
        scored: list[tuple[dict[str, Any], float]] = []
        for row, doc in zip(rows, docs):
            if not doc:
                continue
            tf = Counter(doc)
            score = 0.0
            dl = len(doc)
            for token, qf in query_counts.items():
                freq = tf.get(token, 0)
                if not freq:
                    continue
                idf = math.log(1.0 + (len(docs) - df[token] + 0.5) / (df[token] + 0.5))
                denom = freq + k1 * (1.0 - b + b * dl / max(avgdl, 1e-9))
                score += qf * idf * (freq * (k1 + 1.0) / denom)
            if score > 0.0:
                scored.append((row, score))
        return sorted(scored, key=lambda item: (-item[1], self._row_id(item[0])))

    def _vector_values(self, value: Any) -> list[float]:
        if value is None:
            return []
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return self._vector_values(parsed)
            except Exception:
                return []
        if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, dict)):
            out: list[float] = []
            for item in value:
                try:
                    out.append(float(item))
                except (TypeError, ValueError):
                    return []
            return out
        return []

    def _cosine(self, left: list[float], right: list[float]) -> float:
        if not left or not right or len(left) != len(right):
            return 0.0
        left_norm = math.sqrt(sum(value * value for value in left))
        right_norm = math.sqrt(sum(value * value for value in right))
        if left_norm == 0.0 or right_norm == 0.0:
            return 0.0
        return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)

    def _vector_rank(self, rows: list[dict[str, Any]], field: str, query_vector: Any) -> list[tuple[dict[str, Any], float]]:
        query = self._vector_values(query_vector)
        if not query:
            return []
        scored = []
        for row in rows:
            score = self._cosine(self._vector_values(row.get(field)), query)
            if score > 0.0:
                scored.append((row, score))
        return sorted(scored, key=lambda item: (-item[1], self._row_id(item[0])))

    def _rrf(self, result_lists: list[list[dict[str, Any]]], weights: list[float]) -> list[tuple[str, float]]:
        scores: dict[str, float] = {}
        for rows, weight in zip(result_lists, weights):
            for rank, row in enumerate(rows, start=1):
                row_id = self._row_id(row)
                scores[row_id] = scores.get(row_id, 0.0) + weight / (K_RRF + rank)
        return sorted(scores.items(), key=lambda item: (-item[1], item[0]))

    def _base_person_id(self, value: str) -> str:
        parts = str(value).split("-")
        if len(parts) == 6 and parts[5].isdigit():
            return "-".join(parts[:5])
        return str(value)

    def _role_output_row(self, row: dict[str, Any], include_attributes: list[str], score: float, retrieval_mode: str) -> dict[str, Any]:
        row_id = self._row_id(row)
        out = self._project_dict(row, include_attributes)
        out["id"] = row_id
        out["score"] = score
        out["person_id"] = row.get("person_id") or row.get("base_id") or self._base_person_id(row_id)
        out["position_id"] = row.get("position_id") or row_id
        out["retrieval_mode"] = retrieval_mode
        return out

    async def hybrid_role_rows(
        self,
        payload: dict[str, Any],
        filters: Any,
        top_k: int,
        include_attributes: list[str],
        embedding_fn: Callable[[str], Any] | None = None,
    ) -> list[dict[str, Any]]:
        semantic_query = str(payload.get("semantic_query") or "").strip()
        has_explicit_query_embedding = payload.get("query_embedding") is not None
        if len(semantic_query) < 80 and not has_explicit_query_embedding:
            rows = sorted(self._filtered_rows("people", filters), key=lambda row: self._row_id(row))
            return [self._role_output_row(row, include_attributes, 1.0, "filter_only") for row in rows]

        candidates = self._filtered_rows("people", filters)
        bm25_queries = [str(query) for query in payload.get("bm25_queries") or [] if str(query).strip()]
        result_lists: list[list[dict[str, Any]]] = []
        weights: list[float] = []

        phrase_tokens: list[str] = []
        word_tokens: list[str] = []
        for query in bm25_queries:
            phrase_tokens.extend(self._phrase_query_tokens(query))
            word_tokens.extend(self._word_query_tokens(query))
        for field, tokens, weight in [
            ("phrase_tokens", list(dict.fromkeys(phrase_tokens)), 1.5),
            ("word_tokens", list(dict.fromkeys(word_tokens)), 1.0),
        ]:
            if tokens:
                result_lists.append([row for row, _score in self._bm25_rank(candidates, field, tokens)[:top_k]])
                weights.append(weight)

        query_embedding = payload.get("query_embedding")
        if query_embedding is None and embedding_fn is not None:
            maybe = embedding_fn(semantic_query)
            query_embedding = await maybe if asyncio.iscoroutine(maybe) else maybe
        if query_embedding is not None:
            result_lists.append([row for row, _score in self._vector_rank(candidates, "vector", query_embedding)[:top_k]])
            weights.append(0.6)

        by_id = {self._row_id(row): row for rows in result_lists for row in rows}
        fused = self._rrf(result_lists, weights) if result_lists else []
        return [
            self._role_output_row(by_id[row_id], include_attributes, score, "hybrid")
            for row_id, score in fused[:top_k]
            if row_id in by_id
        ]

    def bm25_adjacency_rows(
        self,
        queries: list[str],
        filters: Any,
        top_k: int,
        include_attributes: list[str],
    ) -> list[dict[str, Any]]:
        candidates = self._filtered_rows("people", filters)
        result_lists: list[list[dict[str, Any]]] = []
        query_indexes: dict[str, list[int]] = {}
        for query_index, query in enumerate(queries):
            tokens = self._phrase_query_tokens(str(query))
            if not tokens:
                continue
            ranked = [row for row, _score in self._bm25_rank(candidates, "phrase_tokens", tokens)[:top_k]]
            result_lists.append(ranked)
            for row in ranked:
                query_indexes.setdefault(self._row_id(row), []).append(query_index)
        fused = self._rrf(result_lists, [1.0] * len(result_lists))
        by_id = {self._row_id(row): row for rows in result_lists for row in rows}
        out: list[dict[str, Any]] = []
        for rank, (row_id, score) in enumerate(fused[:top_k], start=1):
            row = self._role_output_row(by_id[row_id], include_attributes, score, "company_adjacency_bm25")
            row["company_adjacency_rank"] = rank
            row["adjacency_query_indexes"] = query_indexes.get(row_id, [])
            out.append(row)
        return out
