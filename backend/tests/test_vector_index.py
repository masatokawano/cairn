"""Tests for the VectorIndex abstraction (P2-1c, ADR-0001).

Three things to verify:
- Both backends (NumpyIndex, SQLiteVecIndex) agree on top-k for the same data.
- CAIRN_VECTOR_INDEX=numpy forces the fallback path even when sqlite-vec is
  installed (the escape hatch from ADR §7.3).
- A 1000-vector smoke check runs in well under a second on either backend,
  matching the personal-archive performance budget in the design doc.
"""
import hashlib
import importlib
import math
import struct
import time

import pytest

from app.embedding import EmbeddingProvider, vector_to_bytes


DIM = 16  # large enough to make collisions vanishingly rare


class FixtureProvider(EmbeddingProvider):
    """Same deterministic shape as test_embedding.FixtureProvider but wider
    (16 floats) — keeps cosine separation clean across many texts."""
    name = "fixture"
    model = "fixture-v1"
    dimension = DIM

    def _vec(self, text: str) -> list[float]:
        # SHA-256 → 32 bytes; pull DIM signed shorts then normalize.
        h = hashlib.sha256(text.encode("utf-8")).digest()
        raw = [int.from_bytes(h[2*i:2*i+2], "little", signed=True) for i in range(DIM)]
        norm = math.sqrt(sum(x * x for x in raw)) or 1.0
        return [x / norm for x in raw]

    def embed_passages(self, texts):
        return [vector_to_bytes(self._vec(t)) for t in texts]

    def embed_query(self, text):
        return vector_to_bytes(self._vec(text))


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Fresh CAIRN_DB for every test; force a module reload so the
    thread-local sqlite-vec load flag is recomputed against this DB."""
    monkeypatch.setenv("CAIRN_DB", str(tmp_path / "test.db"))
    from app import db as db_module
    importlib.reload(db_module)
    yield db_module
    conn = getattr(db_module._local, "conn", None)
    if conn:
        conn.close()
        db_module._local.conn = None


def _make_conv(source_id: str, text: str, source="chatgpt"):
    from app.parsers.base import ParsedConversation, ParsedMessage
    return ParsedConversation(
        source=source, source_id=source_id, title="t",
        messages=[ParsedMessage(role="user", text=text, created_at="2025-01-01T00:00:00Z")],
        created_at="2025-01-01T00:00:00Z", updated_at="2025-01-01T00:10:00Z",
    )


# --- backend selection -----------------------------------------------------

def test_default_backend_is_sqlite_vec_when_extension_loads(db):
    """sqlite-vec is in requirements.txt and the local Python supports
    LOAD EXTENSION, so connect() should pick SQLiteVecIndex."""
    db.connect()
    assert db.vector_index().name == "sqlite-vec"


def test_env_var_forces_numpy_backend(tmp_path, monkeypatch):
    """CAIRN_VECTOR_INDEX=numpy is the documented escape hatch (ADR §7.3)
    — used to pin to the fallback when the C extension misbehaves."""
    monkeypatch.setenv("CAIRN_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("CAIRN_VECTOR_INDEX", "numpy")
    from app import db as db_module
    importlib.reload(db_module)
    db_module.connect()
    assert db_module.vector_index().name == "numpy"


# --- backend equivalence ----------------------------------------------------

def test_both_backends_return_same_top_k_ranking(db, monkeypatch):
    """Whatever sqlite-vec ranks first, NumpyIndex must rank first too —
    same data, same cosine, only the implementation differs. We don't
    require identical floats (the C path runs in single precision); equal
    *order* of the top-3 ids is the contract callers see."""
    db.upsert_conversations([
        _make_conv(f"c{i}", f"text-{i}") for i in range(20)
    ])
    provider = FixtureProvider()
    db.embed_chunks(provider)
    q = provider.embed_query("text-7")  # exact match → must rank first

    # SQLiteVecIndex (default when loaded)
    assert db.vector_index().name == "sqlite-vec"
    hits_sv = db.find_similar_chunks(q, provider="fixture", model="fixture-v1", k=3)

    # Pin to numpy and ask the same question.
    monkeypatch.setenv("CAIRN_VECTOR_INDEX", "numpy")
    importlib.reload(__import__("app.db", fromlist=["x"]))
    from app import db as db_module
    db_module.connect()
    assert db_module.vector_index().name == "numpy"
    hits_np = db_module.find_similar_chunks(q, provider="fixture", model="fixture-v1", k=3)

    assert [h["chunk_id"] for h in hits_sv] == [h["chunk_id"] for h in hits_np]
    assert hits_sv[0]["text"] == "text-7"


def test_sqlite_vec_handles_dimension_mismatch_via_fallback(db):
    """A pre-existing embedding row at a different width must not poison
    the search — db.find_similar_chunks filters candidates on dimension."""
    db.upsert_conversations([_make_conv("a", "alpha")])
    db.embed_chunks(FixtureProvider())
    # corrupt the embedding to a different width
    bad = struct.pack("<4f", 1.0, 2.0, 3.0, 4.0)
    db.connect().execute(
        "UPDATE embeddings SET vector=?, dimension=4", (bad,)
    )
    db.connect().commit()
    q = FixtureProvider().embed_query("alpha")
    hits = db.find_similar_chunks(q, provider="fixture", model="fixture-v1", k=5)
    # the row is rejected by the dimension filter in candidates
    assert hits == []


# --- rebuild ---------------------------------------------------------------

def test_rebuild_repopulates_after_clear(db):
    """rebuild() pulls from `embeddings` (the source of truth) and recreates
    a usable index — the ADR's guarantee that index loss isn't data loss."""
    db.upsert_conversations([_make_conv(f"c{i}", f"text-{i}") for i in range(5)])
    db.embed_chunks(FixtureProvider())
    idx = db.vector_index()
    if idx.name != "sqlite-vec":
        pytest.skip("rebuild() is meaningful only for the sqlite-vec backend")
    idx.clear()
    n = idx.rebuild(db.connect())
    assert n == 5
    q = FixtureProvider().embed_query("text-2")
    hits = db.find_similar_chunks(q, provider="fixture", model="fixture-v1", k=1)
    assert hits and hits[0]["text"] == "text-2"


def test_rebuild_persists_across_connections(tmp_path, monkeypatch):
    """Regression: `admin rebuild-vector-index` used to roll back silently
    because Python 3.6+ stopped auto-committing DDL — DROP/CREATE/INSERTs
    inside rebuild() were all in one uncommitted transaction. Open a new
    connection after rebuild and verify chunk_vecs still has rows."""
    import importlib
    monkeypatch.setenv("CAIRN_DB", str(tmp_path / "test.db"))
    from app import db as db_module
    importlib.reload(db_module)
    db_module.upsert_conversations([_make_conv("c1", "t1"), _make_conv("c2", "t2")])
    db_module.embed_chunks(FixtureProvider())
    idx = db_module.vector_index()
    if idx.name != "sqlite-vec":
        pytest.skip("regression is sqlite-vec specific")
    idx.rebuild(db_module.connect())

    # Force a fresh connection by reloading the module — simulates the
    # behavior of `admin rebuild-vector-index` exiting and a separate
    # `admin integrity-check` process reading the DB anew.
    conn = getattr(db_module._local, "conn", None)
    if conn:
        conn.close()
        db_module._local.conn = None
    importlib.reload(db_module)
    n = db_module.connect().execute("SELECT COUNT(*) FROM chunk_vecs").fetchone()[0]
    assert n == 2  # the two upserted convs, each one chunk


# --- perf smoke -------------------------------------------------------------

def test_1000_vector_search_completes_quickly(db):
    """ADR §1.1: < 200ms target for ~tens-of-thousands of chunks. With 1000
    fixture chunks both backends must stay well under that budget; we set a
    loose 1s ceiling to keep the test stable on slow CI without falsely
    passing a regressed implementation."""
    db.upsert_conversations([_make_conv(f"c{i}", f"text-{i}") for i in range(1000)])
    db.embed_chunks(FixtureProvider())
    q = FixtureProvider().embed_query("text-500")
    start = time.monotonic()
    hits = db.find_similar_chunks(q, provider="fixture", model="fixture-v1", k=10)
    elapsed = time.monotonic() - start
    assert len(hits) == 10
    assert hits[0]["text"] == "text-500"
    assert elapsed < 1.0, f"search took {elapsed:.3f}s"
