"""Vector KNN index abstraction (Phase 2, P2-1c, ADR-0001).

`embeddings.vector` is the source of truth. A VectorIndex is a *derived*
KNN structure built on top: drop it and rebuild from the embeddings table,
no data loss. The interface exists so the search path doesn't care whether
the backend is sqlite-vec (a C extension shipping a `vec0` virtual table)
or a Python fallback that loops over the BLOB column.

Indexes are chunk-id keyed. Filters (provider, model, source, date) live in
db.py — the caller pre-computes the candidate chunk-id set and passes it
in, so each backend can apply it efficiently in its own dialect.
"""
from __future__ import annotations

import logging
import math
import sqlite3
from abc import ABC, abstractmethod

from .embedding import bytes_to_vector, cosine_similarity

log = logging.getLogger("cairn.vector_index")


class VectorIndex(ABC):
    """KNN over chunk_id ↔ vector pairs. Stateless on (provider, model);
    callers filter via the `candidates` list and the JOINs that produce it.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable id for logging / integrity-check (e.g. 'sqlite-vec', 'numpy')."""

    @abstractmethod
    def upsert(self, chunk_id: int, vector: bytes, dimension: int) -> None:
        """Insert or replace one chunk's vector. Called from embed_chunks
        after the embeddings row is written, so the index mirrors the
        canonical store one row at a time."""

    @abstractmethod
    def delete_chunks(self, chunk_ids: list[int]) -> None:
        """Drop these chunk_ids from the index. CASCADE deletes from chunks/
        messages don't propagate to a virtual table — db.py calls this
        explicitly when it removes embeddings."""

    @abstractmethod
    def search(
        self,
        query: bytes,
        k: int,
        *,
        candidates: list[int] | None = None,
    ) -> list[tuple[int, float]]:
        """Return [(chunk_id, score)] top-k, higher score = closer (cosine).
        If `candidates` is given, restrict the search to those chunk_ids."""

    @abstractmethod
    def rebuild(self, conn: sqlite3.Connection) -> int:
        """Drop and re-populate from the embeddings table for the dominant
        dimension. Returns the number of rows inserted."""

    @abstractmethod
    def clear(self) -> None:
        """Drop every stored vector. Used in tests and by rebuild()."""


# --- NumpyIndex: SQL + pure-Python cosine, always available ----------------

class NumpyIndex(VectorIndex):
    """The dependency-free fallback. Reads vectors from `embeddings` on each
    search; no separate storage to keep in sync. Despite the name, numpy is
    not required — the cosine loop is pure Python so this works wherever
    SQLite does. For personal-archive scale (tens of thousands of chunks)
    a single query stays well under 100ms.
    """

    def __init__(self, conn_provider):
        # Take a callable so the index always uses the *current* connection
        # (the connection is thread-local in db.py and may rotate during
        # tests that reload the module).
        self._conn = conn_provider

    @property
    def name(self) -> str:
        return "numpy"

    def upsert(self, chunk_id: int, vector: bytes, dimension: int) -> None:
        # Nothing to do: embeddings.vector IS the index for this backend.
        return

    def delete_chunks(self, chunk_ids: list[int]) -> None:
        return

    def search(
        self,
        query: bytes,
        k: int,
        *,
        candidates: list[int] | None = None,
    ) -> list[tuple[int, float]]:
        q = bytes_to_vector(query)
        conn = self._conn()
        if candidates is None:
            rows = conn.execute(
                "SELECT chunk_id, vector, dimension FROM embeddings"
            ).fetchall()
        elif not candidates:
            return []
        else:
            # IN (?,?,?) — SQLite allows up to ~999 host parameters per
            # statement by default; chunk in batches if we ever cross that.
            scored: list[tuple[int, float]] = []
            for i in range(0, len(candidates), 900):
                batch = candidates[i:i + 900]
                placeholders = ",".join("?" * len(batch))
                for row in conn.execute(
                    f"SELECT chunk_id, vector, dimension FROM embeddings "
                    f"WHERE chunk_id IN ({placeholders})",
                    batch,
                ):
                    if row["dimension"] != len(q):
                        continue
                    scored.append(
                        (row["chunk_id"], cosine_similarity(q, bytes_to_vector(row["vector"])))
                    )
            scored.sort(key=lambda r: r[1], reverse=True)
            return scored[:k]
        scored = []
        for row in rows:
            if row["dimension"] != len(q):
                continue
            scored.append(
                (row["chunk_id"], cosine_similarity(q, bytes_to_vector(row["vector"])))
            )
        scored.sort(key=lambda r: r[1], reverse=True)
        return scored[:k]

    def rebuild(self, conn: sqlite3.Connection) -> int:
        # No own storage to rebuild; report the row count for symmetry.
        return conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]

    def clear(self) -> None:
        return


# --- SQLiteVecIndex: vec0 virtual table, brute-force KNN in C --------------

# vec0 carries a single fixed dimension. Multi-dimension archives would need
# one table per width; in practice a Cairn install runs one active model, so
# we track the served dimension in `vector_index_meta` and treat dimension
# mismatches as "fall back to NumpyIndex" rather than building parallel tables.
_META_TABLE = "vector_index_meta"
_VEC_TABLE = "chunk_vecs"


class SQLiteVecIndex(VectorIndex):
    """sqlite-vec backed index. Stores one vector per chunk_id in a `vec0`
    virtual table that lives inside `cairn.db` (single-file backup, ADR §4.1).

    The vec0 table has a fixed dimension fixed at creation time. The first
    upsert sets the width, recorded in `vector_index_meta`. Upserts with a
    different width are skipped (see warning in upsert) so search results
    don't quietly mix incompatible vectors. To re-key onto a new width,
    call `clear()` then re-`rebuild()` against the new embeddings.
    """

    def __init__(self, conn_provider):
        self._conn = conn_provider
        self._ensured = False

    @property
    def name(self) -> str:
        return "sqlite-vec"

    def _ensure_meta_table(self) -> None:
        if self._ensured:
            return
        conn = self._conn()
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {_META_TABLE} "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self._ensured = True

    def _served_dimension(self) -> int | None:
        self._ensure_meta_table()
        row = self._conn().execute(
            f"SELECT value FROM {_META_TABLE} WHERE key='dimension'"
        ).fetchone()
        return int(row["value"]) if row else None

    def _create_vec_table(self, dimension: int) -> None:
        conn = self._conn()
        # distance_metric=cosine because we always normalize at the provider
        # (e5 outputs unit vectors); on un-normalized vectors cosine is the
        # only metric that still ranks correctly.
        conn.execute(
            f"CREATE VIRTUAL TABLE {_VEC_TABLE} "
            f"USING vec0(embedding float[{dimension}] distance_metric=cosine)"
        )
        conn.execute(
            f"INSERT OR REPLACE INTO {_META_TABLE}(key,value) VALUES('dimension', ?)",
            (str(dimension),),
        )

    def upsert(self, chunk_id: int, vector: bytes, dimension: int) -> None:
        self._ensure_meta_table()
        conn = self._conn()
        served = self._served_dimension()
        if served is None:
            self._create_vec_table(dimension)
        elif served != dimension:
            log.warning(
                "sqlite-vec index serves dim=%d but got upsert dim=%d for chunk %d; "
                "skipping. Call admin reindex --rebuild to re-key.",
                served, dimension, chunk_id,
            )
            return
        # vec0 doesn't accept INSERT OR REPLACE, so re-embed is a DELETE then
        # INSERT. The pair is atomic inside the caller's transaction.
        #
        # Multi-(provider, model) note: if two providers embed the same chunk,
        # this overwrite makes vec0 hold whichever upsert ran last. Searches
        # for the *other* provider's embedding fall through to NumpyIndex via
        # the empty-results fallback in db.find_similar_chunks. Personal-archive
        # use cases are single-model, so the common path is correct.
        conn.execute(f"DELETE FROM {_VEC_TABLE} WHERE rowid = ?", (chunk_id,))
        conn.execute(
            f"INSERT INTO {_VEC_TABLE}(rowid, embedding) VALUES (?, ?)",
            (chunk_id, vector),
        )

    def delete_chunks(self, chunk_ids: list[int]) -> None:
        if not chunk_ids or self._served_dimension() is None:
            return
        conn = self._conn()
        placeholders = ",".join("?" * len(chunk_ids))
        conn.execute(
            f"DELETE FROM {_VEC_TABLE} WHERE rowid IN ({placeholders})",
            chunk_ids,
        )

    def search(
        self,
        query: bytes,
        k: int,
        *,
        candidates: list[int] | None = None,
    ) -> list[tuple[int, float]]:
        served = self._served_dimension()
        if served is None or served * 4 != len(query):
            # No vectors stored yet, or query width doesn't match the served
            # width — let the caller fall through to NumpyIndex semantics by
            # returning empty. (Empty results are indistinguishable from
            # "nothing matches" downstream; db.py logs the mismatch.)
            return []
        conn = self._conn()
        # vec0 requires exactly one of `k = ?` or `LIMIT`. We pick `k = ?`
        # in both branches: `LIMIT` + single-element `rowid IN (?)` is
        # rewritten by SQLite's planner to `rowid = ?` and the resulting
        # plan never registers a LIMIT, so vec0 errors out. `k = ?` is
        # constraint-side and survives that rewrite.
        if candidates is None:
            rows = conn.execute(
                f"SELECT rowid, distance FROM {_VEC_TABLE} "
                f"WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                (query, k),
            ).fetchall()
        else:
            if not candidates:
                return []
            placeholders = ",".join("?" * len(candidates))
            rows = conn.execute(
                f"SELECT rowid, distance FROM {_VEC_TABLE} "
                f"WHERE embedding MATCH ? AND k = ? AND rowid IN ({placeholders}) "
                f"ORDER BY distance",
                (query, k, *candidates),
            ).fetchall()
        # cosine distance in [0,2]; we want score in [-1,1] with higher = closer
        return [(r["rowid"], 1.0 - r["distance"]) for r in rows]

    def rebuild(self, conn: sqlite3.Connection) -> int:
        self.clear()
        rows = conn.execute(
            "SELECT chunk_id, vector, dimension FROM embeddings"
        ).fetchall()
        if not rows:
            return 0
        # Pick the dominant dimension — usually the only one — so the
        # rebuild produces a usable index even if a stray off-dim row exists.
        dims: dict[int, int] = {}
        for row in rows:
            dims[row["dimension"]] = dims.get(row["dimension"], 0) + 1
        active = max(dims, key=dims.get)
        self._create_vec_table(active)
        n = 0
        for row in rows:
            if row["dimension"] != active:
                continue
            conn.execute(
                f"INSERT OR REPLACE INTO {_VEC_TABLE}(rowid, embedding) VALUES (?, ?)",
                (row["chunk_id"], row["vector"]),
            )
            n += 1
        return n

    def clear(self) -> None:
        conn = self._conn()
        # Drop both the virtual table and the meta row so a subsequent upsert
        # creates a fresh vec0 sized to the new dimension.
        conn.execute(f"DROP TABLE IF EXISTS {_VEC_TABLE}")
        self._ensure_meta_table()
        conn.execute(f"DELETE FROM {_META_TABLE} WHERE key='dimension'")


def try_load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    """Best-effort extension load. Returns True on success.

    Failure modes we tolerate (per ADR §5.1): system SQLite built without
    LOAD EXTENSION, sqlite-vec not installed, OS sandboxing the load. In
    every case db.vector_index() falls back to NumpyIndex.
    """
    try:
        import sqlite_vec  # type: ignore
    except ImportError:
        log.info("sqlite-vec not installed; using NumpyIndex fallback")
        return False
    try:
        conn.enable_load_extension(True)
    except (AttributeError, sqlite3.NotSupportedError):
        log.info("sqlite3 build does not support LOAD EXTENSION; using NumpyIndex")
        return False
    try:
        sqlite_vec.load(conn)
    except sqlite3.OperationalError as exc:
        log.info("sqlite-vec.load failed (%s); using NumpyIndex", exc)
        conn.enable_load_extension(False)
        return False
    finally:
        # Always close the extension-load surface once we've loaded what we
        # need; leaving it open is an unnecessary attack surface.
        try:
            conn.enable_load_extension(False)
        except (AttributeError, sqlite3.NotSupportedError):
            pass
    return True
