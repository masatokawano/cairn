"""Rules-based entity extraction runner (P3-B).

Runs URL and GitHub repo detectors over messages, writes results to
entities + entity_mentions, and records the batch in extraction_runs.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from .. import db
from .rules.urls import extract_urls
from .rules.github import extract_repos

log = logging.getLogger(__name__)

RULES_VERSION = "rules-entity-v1"
_DETECTOR_FUNCS = [extract_urls, extract_repos]


def run_rules_extraction(
    *,
    conversation_id: int | None = None,
    limit: int | None = None,
) -> dict:
    """Extract entities from messages using rules-based detectors.

    Args:
        conversation_id: restrict to a single conversation, or None for all.
        limit: cap the number of conversations processed (useful for testing).

    Returns a summary dict: {conversations, messages, entities_new, mentions_new, warnings}.
    """
    started_at = _now()
    scope = f"conversation:{conversation_id}" if conversation_id else "all"

    run_id = db.start_extraction_run(
        kind="rules-entity",
        scope=scope,
        provider="rules",
        model=None,
        prompt_version=RULES_VERSION,
        started_at=started_at,
    )

    warnings: list[str] = []
    conv_count = 0
    msg_count = 0
    entities_new = 0
    mentions_new = 0

    try:
        conversations = _load_conversations(conversation_id, limit)
        for conv in conversations:
            cid = conv["id"]
            conv_count += 1
            messages = _load_messages(cid)
            for msg in messages:
                msg_count += 1
                mid = msg["id"]
                text = msg["text"] or ""
                if not text:
                    continue
                ts = msg.get("created_at") or started_at
                for detect in _DETECTOR_FUNCS:
                    try:
                        matches = detect(text)
                    except Exception as exc:
                        warnings.append(f"detector error msg={mid}: {exc}")
                        continue
                    for match in matches:
                        try:
                            eid = db.upsert_entity(
                                kind=match.kind,
                                canonical_name=match.canonical_name,
                                external_id=match.external_id,
                                created_at=ts,
                            )
                            before = db.count_entity_mentions()
                            db.upsert_entity_mention(
                                entity_id=eid,
                                message_id=mid,
                                conversation_id=cid,
                                start_offset=match.start,
                                end_offset=match.end,
                                surface=match.surface,
                                detector=match.detector,
                                created_at=ts,
                            )
                            after = db.count_entity_mentions()
                            if after > before:
                                mentions_new += 1
                        except Exception as exc:
                            warnings.append(f"db error msg={mid}: {exc}")

        # Count entities that were newly inserted during this run (approximate:
        # we track total after vs before the run).
        entities_new = db.count_entities()

        status = "partial" if warnings else "ok"
        db.finish_extraction_run(
            run_id,
            completed_at=_now(),
            status=status,
            warnings=warnings[:200],  # cap to avoid massive summaries
        )
        log.info(
            "rules extraction done: convs=%d msgs=%d mentions_new=%d warnings=%d",
            conv_count, msg_count, mentions_new, len(warnings),
        )

    except Exception as exc:
        db.finish_extraction_run(
            run_id,
            completed_at=_now(),
            status="failed",
            error=str(exc),
        )
        raise

    return {
        "conversations": conv_count,
        "messages": msg_count,
        "entities_new": entities_new,
        "mentions_new": mentions_new,
        "warnings": len(warnings),
        "run_id": run_id,
    }


def _load_conversations(
    conversation_id: int | None, limit: int | None
) -> list[dict]:
    conn = db.connect()
    if conversation_id:
        rows = conn.execute(
            "SELECT id FROM conversations WHERE id=?", (conversation_id,)
        ).fetchall()
    else:
        q = "SELECT id FROM conversations ORDER BY id"
        if limit:
            q += f" LIMIT {int(limit)}"
        rows = conn.execute(q).fetchall()
    return [dict(r) for r in rows]


def _load_messages(conversation_id: int) -> list[dict]:
    conn = db.connect()
    rows = conn.execute(
        "SELECT id, text, created_at FROM messages WHERE conversation_id=? ORDER BY id",
        (conversation_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
