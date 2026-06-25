"""Tests for hybrid search (P2-2): keyword / semantic / hybrid modes + RRF.

Uses the FixtureProvider from test_embedding so the suite stays offline; the
real sentence-transformers smoke for hybrid lives in the e5-small test there
(this file would re-download nothing and just thrash CI)."""
import hashlib
import importlib
import math

import pytest

from app.embedding import EmbeddingProvider, vector_to_bytes


DIM = 16


class FixtureProvider(EmbeddingProvider):
    """Hash-of-text → 16D unit vector. Same text → identical vector (cosine 1.0)."""
    name = "fixture"
    model = "fixture-v1"
    dimension = DIM

    def _vec(self, text: str) -> list[float]:
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
    monkeypatch.setenv("CAIRN_DB", str(tmp_path / "test.db"))
    from app import db as db_module
    importlib.reload(db_module)
    yield db_module
    conn = getattr(db_module._local, "conn", None)
    if conn:
        conn.close()
        db_module._local.conn = None


def _conv(source_id: str, text: str, source="chatgpt",
          updated_at="2025-01-01T00:00:00Z"):
    from app.parsers.base import ParsedConversation, ParsedMessage
    return ParsedConversation(
        source=source, source_id=source_id, title=f"t-{source_id}",
        messages=[ParsedMessage(role="user", text=text, created_at=updated_at)],
        created_at=updated_at, updated_at=updated_at,
    )


# --- mode dispatch ----------------------------------------------------------

def test_default_mode_is_keyword_and_does_not_load_provider(db):
    """Existing callers (no `mode=`) must keep working without triggering an
    embedding load — the default has to stay free of side effects."""
    db.upsert_conversations([_conv("a", "alpha beta gamma")])
    # CAIRN_EMBED_PROVIDER unset and no embeddings table content; if the
    # default mode were semantic/hybrid this would raise.
    results = db.search("alpha")
    assert len(results) == 1
    assert results[0]["match_reason"] == "keyword"


def test_invalid_mode_raises(db):
    with pytest.raises(ValueError):
        db.search("alpha", mode="banana")


def test_keyword_mode_populates_matched_keywords_from_highlights(db):
    db.upsert_conversations([_conv("a", "alpha beta gamma alpha")])
    results = db.search("alpha", mode="keyword")
    assert results[0]["match_reason"] == "keyword"
    assert results[0]["matched_keywords"] == ["alpha"]
    assert results[0]["semantic_score"] is None


# --- semantic mode ----------------------------------------------------------

def test_semantic_mode_ranks_chunks_by_cosine(db):
    db.upsert_conversations([
        _conv("a", "alpha"),
        _conv("b", "beta"),
        _conv("c", "gamma"),
    ])
    provider = FixtureProvider()
    db.embed_chunks(provider)
    hits = db.search("beta", mode="semantic", provider=provider, limit=3)
    assert hits[0]["title"] == "t-b"
    assert hits[0]["match_reason"] == "semantic"
    assert math.isclose(hits[0]["semantic_score"], 1.0, abs_tol=1e-5)
    assert hits[0]["matched_keywords"] == []  # no [[…]] in chunk text


def test_semantic_mode_returns_empty_when_no_embeddings(db):
    db.upsert_conversations([_conv("a", "alpha")])
    # no embed_chunks call → embeddings table empty → semantic finds nothing
    provider = FixtureProvider()
    hits = db.search("alpha", mode="semantic", provider=provider)
    assert hits == []


def test_semantic_mode_aggregates_to_best_chunk_per_conversation(db):
    # Single conversation with multiple messages → multiple chunks. Semantic
    # search should return one row for the conversation, not one per chunk.
    from app.parsers.base import ParsedConversation, ParsedMessage
    pc = ParsedConversation(
        source="chatgpt", source_id="multi", title="multi",
        messages=[
            ParsedMessage(role="user", text="alpha", created_at="2025-01-01T00:00:00Z"),
            ParsedMessage(role="assistant", text="beta", created_at="2025-01-01T00:01:00Z"),
            ParsedMessage(role="user", text="gamma", created_at="2025-01-01T00:02:00Z"),
        ],
        created_at="2025-01-01T00:00:00Z", updated_at="2025-01-01T00:02:00Z",
    )
    db.upsert_conversations([pc])
    provider = FixtureProvider()
    db.embed_chunks(provider)
    hits = db.search("beta", mode="semantic", provider=provider, limit=10)
    # one row per conversation, hit_count reflects how many chunks matched
    titles = [h["title"] for h in hits]
    assert titles.count("multi") == 1
    # the best-scoring chunk for "beta" query is the "beta" message
    multi = next(h for h in hits if h["title"] == "multi")
    assert multi["snippet"] == "beta"
    assert multi["hit_count"] >= 1


def test_semantic_mode_honors_source_and_date_filters(db):
    db.upsert_conversations([
        _conv("a", "alpha", source="chatgpt", updated_at="2025-01-01T00:00:00Z"),
        _conv("b", "alpha", source="claude_cli", updated_at="2026-01-01T00:00:00Z"),
    ])
    provider = FixtureProvider()
    db.embed_chunks(provider)
    hits = db.search("alpha", mode="semantic", provider=provider, source="chatgpt")
    assert [h["source"] for h in hits] == ["chatgpt"]
    hits = db.search("alpha", mode="semantic", provider=provider,
                     after="2025-12-01T00:00:00Z")
    assert [h["source"] for h in hits] == ["claude_cli"]


# --- hybrid mode (RRF) ------------------------------------------------------

def test_hybrid_promotes_conversations_hit_by_both_paths(db):
    """RRF's key property: a row that surfaces in BOTH ranked lists adds
    contributions from both, so it should outrank rows surfacing in only one."""
    db.upsert_conversations([
        # conv_kw: matches the keyword "alpha" but the chunk vector is far
        # from the query vector for "needle" (different text content).
        _conv("conv_kw", "alpha haystack haystack haystack"),
        # conv_sem: matches semantically (vector is close to "needle" query)
        # but doesn't contain the word "alpha" or "needle" literally.
        _conv("conv_sem", "needle"),
        # conv_both: contains "alpha" AND its chunk text is "needle".
        # Both paths should rank it.
        _conv("conv_both", "needle alpha"),
    ])
    provider = FixtureProvider()
    db.embed_chunks(provider)
    # Query uses "alpha needle" — keyword "alpha" hits conv_kw and conv_both;
    # semantic query closest to the chunk text "needle" matches conv_sem best.
    hits = db.search("alpha needle", mode="hybrid", provider=provider, limit=5)
    titles = [h["title"] for h in hits]
    # conv_both should be in the top 2 because RRF adds contributions
    assert "t-conv_both" in titles[:2]
    # match_reason is "both" when both paths surfaced the conversation
    both = next(h for h in hits if h["title"] == "t-conv_both")
    assert both["match_reason"] == "both"
    assert both["semantic_score"] is not None


def test_hybrid_falls_back_to_keyword_when_semantic_empty(db):
    """If no embeddings exist, the semantic side contributes nothing — the
    hybrid result equals the keyword result."""
    db.upsert_conversations([_conv("a", "alpha")])
    provider = FixtureProvider()
    # deliberately skip embed_chunks
    hits = db.search("alpha", mode="hybrid", provider=provider)
    assert len(hits) == 1
    assert hits[0]["match_reason"] == "keyword"


def test_hybrid_uses_keyword_snippet_when_both_paths_hit(db):
    """The keyword snippet has FTS [[…]] markers a UI can highlight. When
    both paths agree, the hybrid result must preserve those, not replace
    them with the bare chunk text the semantic path emitted."""
    db.upsert_conversations([_conv("a", "alpha beta")])
    provider = FixtureProvider()
    db.embed_chunks(provider)
    hits = db.search("alpha", mode="hybrid", provider=provider)
    assert "[[alpha]]" in hits[0]["snippet"]


# --- provider resolution ---------------------------------------------------

def test_active_provider_resolution_requires_embeddings(db):
    """Without env var and without any embeddings, the resolver can't guess —
    it must raise a clear error rather than silently picking a default."""
    db.upsert_conversations([_conv("a", "alpha")])
    with pytest.raises(RuntimeError, match="no embeddings"):
        db._active_embedding_provider()


def test_active_provider_resolution_via_env_var(db, monkeypatch):
    """CAIRN_EMBED_PROVIDER overrides the dominant-row inference; we verify
    it's parsed without actually loading the (real) provider."""
    monkeypatch.setenv("CAIRN_EMBED_PROVIDER", "unknown-provider:some-model")
    with pytest.raises(ValueError, match="unknown provider"):
        db._active_embedding_provider()


def test_active_provider_env_var_rejects_malformed_value(db, monkeypatch):
    monkeypatch.setenv("CAIRN_EMBED_PROVIDER", "no-colon-form")
    with pytest.raises(ValueError, match="CAIRN_EMBED_PROVIDER"):
        db._active_embedding_provider()
