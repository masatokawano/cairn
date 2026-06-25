"""Tests for embeddings (P2-1b): provider abstraction + DB integration.

Uses a deterministic FixtureProvider so the suite stays dependency-free —
the real sentence-transformers provider gets a separate smoke check.
"""
import hashlib
import importlib
import math
import struct

import pytest

from app.embedding import (
    EmbeddingProvider,
    bytes_to_vector,
    cosine_similarity,
    vector_to_bytes,
)


DIM = 8  # small enough to inspect by hand


class FixtureProvider(EmbeddingProvider):
    """Deterministic provider: vector derived from a SHA-256 of the text.

    Same text → same vector → cosine == 1, distinct texts get distinct vectors.
    Normalized so the cosine ranking is purely about direction.
    """
    name = "fixture"
    model = "fixture-v1"
    dimension = DIM

    def _vec(self, text: str) -> list[float]:
        h = hashlib.sha256(text.encode("utf-8")).digest()
        # 8 floats from the first 32 bytes; signed so cosine spans [-1, 1]
        raw = [b - 128 for b in h[:DIM]]
        norm = math.sqrt(sum(x * x for x in raw)) or 1.0
        return [x / norm for x in raw]

    def embed_passages(self, texts):
        return [vector_to_bytes(self._vec(t)) for t in texts]

    def embed_query(self, text):
        return vector_to_bytes(self._vec(text))


# --- utility functions ------------------------------------------------------

def test_vector_round_trip_preserves_f32_values():
    vec = [0.1, -0.25, 3.5, 0.0, 1e-3, -1e-3, 100.0, -100.0]
    b = vector_to_bytes(vec)
    assert len(b) == len(vec) * 4
    out = bytes_to_vector(b)
    for a, c in zip(vec, out):
        assert math.isclose(a, c, rel_tol=1e-6, abs_tol=1e-6)


def test_cosine_basic_cases():
    assert math.isclose(cosine_similarity([1, 0], [1, 0]), 1.0)
    assert math.isclose(cosine_similarity([1, 0], [-1, 0]), -1.0)
    assert math.isclose(cosine_similarity([1, 0], [0, 1]), 0.0, abs_tol=1e-9)
    # zero vector → 0 (no NaN)
    assert cosine_similarity([0, 0], [1, 1]) == 0.0


def test_provider_abc_is_abstract():
    with pytest.raises(TypeError):
        EmbeddingProvider()  # type: ignore[abstract]


# --- DB integration ---------------------------------------------------------

@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_DB", str(tmp_path / "test.db"))
    from app import db as db_module
    importlib.reload(db_module)
    yield db_module
    conn = getattr(db_module._local, "conn", None)
    if conn:
        conn.close()
        db_module._local.conn = None


def make_conv(source_id="c1", text="本文テキスト", source="chatgpt",
              updated_at="2025-01-01T00:10:00Z"):
    from app.parsers.base import ParsedConversation, ParsedMessage
    return ParsedConversation(
        source=source, source_id=source_id, title="テスト",
        messages=[ParsedMessage(role="user", text=text, created_at="2025-01-01T00:00:00Z")],
        created_at="2025-01-01T00:00:00Z", updated_at=updated_at,
    )


def test_fresh_db_has_embeddings_table_at_v6(db):
    conn = db.connect()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "embeddings" in tables


def test_embed_chunks_writes_one_row_per_chunk(db):
    db.upsert_conversations([make_conv(text="アルファ")])
    stats = db.embed_chunks(FixtureProvider())
    assert stats == {"chunks": 1, "skipped": 0}
    rows = db.connect().execute(
        "SELECT chunk_id, provider, model, dimension, length(vector) AS vlen "
        "FROM embeddings"
    ).fetchall()
    assert len(rows) == 1
    assert (rows[0]["provider"], rows[0]["model"]) == ("fixture", "fixture-v1")
    assert rows[0]["dimension"] == DIM
    assert rows[0]["vlen"] == DIM * 4  # f32 LE


def test_embed_chunks_only_missing_is_idempotent(db):
    db.upsert_conversations([make_conv()])
    db.embed_chunks(FixtureProvider())
    stats = db.embed_chunks(FixtureProvider())
    assert stats == {"chunks": 0, "skipped": 1}


def test_embed_chunks_overwrites_when_only_missing_false(db):
    db.upsert_conversations([make_conv()])
    db.embed_chunks(FixtureProvider())
    # second pass with only_missing=False must replace, not duplicate
    stats = db.embed_chunks(FixtureProvider(), only_missing=False)
    assert stats == {"chunks": 1, "skipped": 0}
    n = db.connect().execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    assert n == 1


def test_two_providers_coexist_per_chunk(db):
    class AltProvider(FixtureProvider):
        name = "alt"
        model = "alt-v1"

    db.upsert_conversations([make_conv()])
    db.embed_chunks(FixtureProvider())
    db.embed_chunks(AltProvider())
    rows = db.connect().execute(
        "SELECT provider, model FROM embeddings ORDER BY provider"
    ).fetchall()
    assert [(r["provider"], r["model"]) for r in rows] == [
        ("alt", "alt-v1"), ("fixture", "fixture-v1"),
    ]


def test_chunk_cascade_removes_embeddings(db):
    db.upsert_conversations([make_conv(text="原文")])
    db.embed_chunks(FixtureProvider())
    # re-upserting with new text replaces the message+chunk → embedding CASCADE
    db.upsert_conversations([make_conv(text="新しい本文")])
    # now embeddings should be empty (the old chunk was deleted)
    n = db.connect().execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    assert n == 0


def test_find_similar_chunks_returns_top_k_by_cosine(db):
    # three chunks, query equals the second → it must rank first with score≈1
    db.upsert_conversations([
        make_conv(source_id="a", text="alpha"),
        make_conv(source_id="b", text="beta"),
        make_conv(source_id="c", text="gamma"),
    ])
    provider = FixtureProvider()
    db.embed_chunks(provider)
    q = provider.embed_query("beta")
    hits = db.find_similar_chunks(q, provider="fixture", model="fixture-v1", k=2)
    assert len(hits) == 2
    assert hits[0]["text"] == "beta"
    assert math.isclose(hits[0]["score"], 1.0, abs_tol=1e-6)
    # k clips correctly
    assert db.find_similar_chunks(q, provider="fixture", model="fixture-v1", k=1)[0]["text"] == "beta"


def test_find_similar_filters_by_source_and_date(db):
    db.upsert_conversations([
        make_conv(source_id="a", text="alpha", source="chatgpt", updated_at="2025-01-01T00:00:00Z"),
        make_conv(source_id="b", text="alpha", source="claude_cli", updated_at="2026-01-01T00:00:00Z"),
    ])
    provider = FixtureProvider()
    db.embed_chunks(provider)
    q = provider.embed_query("alpha")
    # source filter keeps only the matching one
    hits = db.find_similar_chunks(q, provider="fixture", model="fixture-v1",
                                  k=5, source="chatgpt")
    assert {h["source"] for h in hits} == {"chatgpt"}
    # date filter
    hits = db.find_similar_chunks(q, provider="fixture", model="fixture-v1",
                                  k=5, after="2025-12-01T00:00:00Z")
    assert {h["source"] for h in hits} == {"claude_cli"}


def test_find_similar_ignores_wrong_dimension_rows(db):
    # A row stamped with the same (provider, model) but a different dimension
    # is corrupt: skip it instead of scoring against truncated/garbage bytes.
    db.upsert_conversations([make_conv()])
    db.embed_chunks(FixtureProvider())
    # force-corrupt: rewrite vector to a different width
    bad = struct.pack("<4f", 1.0, 2.0, 3.0, 4.0)
    db.connect().execute(
        "UPDATE embeddings SET vector=?, dimension=4", (bad,)
    )
    db.connect().commit()
    q = FixtureProvider().embed_query("anything")
    hits = db.find_similar_chunks(q, provider="fixture", model="fixture-v1", k=5)
    assert hits == []  # the corrupt row is silently dropped, not a crash


def test_integrity_check_reports_embeddings_clean(db):
    db.upsert_conversations([make_conv()])
    db.embed_chunks(FixtureProvider())
    report = db.integrity_check()
    assert report["ok"]
    assert report["checks"]["orphan_embeddings"] == 0


# --- real-model smoke (only runs if sentence-transformers is installed) ----

sentence_transformers = pytest.importorskip("sentence_transformers")


def test_local_sbert_smoke_returns_relevant_chunk(db):
    """End-to-end with the real e5-small model.

    Three semantically distinct passages, query similar to one of them →
    that passage must rank first. Loads ~470MB of weights on first run;
    pytest.importorskip above gates the whole section, so suites without
    sentence-transformers installed cleanly skip rather than fail.
    """
    from app.embedding.local_sbert import LocalSbertProvider
    db.upsert_conversations([
        make_conv(source_id="cooking", text="今日は鶏肉と野菜でカレーを作った。スパイスは控えめ。"),
        make_conv(source_id="travel", text="京都の寺巡りの旅程を計画している。紅葉の季節がいい。"),
        make_conv(source_id="code", text="Pythonの非同期処理について調べた。asyncioのイベントループ。"),
    ])
    provider = LocalSbertProvider()
    assert provider.dimension == 384
    stats = db.embed_chunks(provider)
    assert stats["chunks"] == 3
    q = provider.embed_query("プログラミングで並行処理を実装したい")
    hits = db.find_similar_chunks(q, provider=provider.name, model=provider.model, k=3)
    assert len(hits) == 3
    # the Python/async chunk must rank first
    assert "Python" in hits[0]["text"]
