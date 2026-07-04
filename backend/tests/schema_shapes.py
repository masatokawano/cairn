"""Shared helper for migration tests that simulate old DB shapes.

Since v12 the chunks table carries a CHECK constraint referencing item_id,
so the old downgrade trick (`ALTER TABLE chunks DROP COLUMN item_id`) fails —
SQLite refuses to drop a column a CHECK mentions. Downgrading now means
rebuilding chunks to its pre-v11 shape wholesale and removing every v11/v12
artefact (items registry, chunks_fts and its triggers).
"""


def downgrade_chunks_pre_v11(conn) -> None:
    """Rebuild chunks to the pre-v11 shape (NOT NULL message/conversation,
    no item_id, no CHECK) and drop the v11/v12 artefacts.

    Must be called OUTSIDE any open transaction: it toggles
    PRAGMA foreign_keys (a silent no-op inside a transaction) around the
    DROP/RENAME, exactly like migration 12 — otherwise dropping the current
    chunks table would cascade-delete every embeddings row, and renaming it
    would rewrite embeddings' FK clause to point at the temp name.
    """
    conn.commit()  # ensure the pragma below is not a no-op
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        with conn:
            conn.execute("DROP TRIGGER IF EXISTS chunks_fts_ai")
            conn.execute("DROP TRIGGER IF EXISTS chunks_fts_ad")
            conn.execute("DROP TRIGGER IF EXISTS chunks_fts_au")
            conn.execute("DROP TABLE IF EXISTS chunks_fts")
            conn.execute("""
                CREATE TABLE chunks_pre11 (
                    id INTEGER PRIMARY KEY,
                    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    idx INTEGER NOT NULL,
                    start_offset INTEGER NOT NULL,
                    end_offset INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'message_text',
                    chunking_version TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )""")
            conn.execute(
                "INSERT INTO chunks_pre11"
                " SELECT id, message_id, conversation_id, idx, start_offset,"
                "        end_offset, text, kind, chunking_version, created_at"
                " FROM chunks WHERE message_id IS NOT NULL"
            )
            conn.execute("DROP TABLE chunks")
            conn.execute("ALTER TABLE chunks_pre11 RENAME TO chunks")
            conn.execute("DROP TABLE IF EXISTS item_links")
            conn.execute("DROP TABLE IF EXISTS items")
            conn.execute("DROP TABLE IF EXISTS sync_state")
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
