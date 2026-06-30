"""Segment extraction runner (P3-C).

Calls an LLMProvider to divide each conversation into topic-coherent segments,
validates the output (schema + grounding), and writes results to the segments
table via extraction_runs.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from .. import db
from ..llm import LLMProvider, ValidationError
from .validate import GroundingContext, extract_with_validation

log = logging.getLogger(__name__)

PROMPT_VERSION = "segment-v1"

_PROMPT_PATH = Path(__file__).parent / "prompts" / "segment_v1.txt"

SEGMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_message_id": {"type": "integer"},
                    "end_message_id": {"type": "integer"},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "topics": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["start_message_id", "end_message_id", "title", "summary", "topics"],
            },
            "minItems": 1,
        }
    },
    "required": ["segments"],
}

_SYSTEM = (
    "You are a conversation analyst. Extract structured data exactly as requested. "
    "Output ONLY the JSON object — no prose, no markdown fences."
)


def run_segment_extraction(
    provider: LLMProvider,
    *,
    conversation_id: int | None = None,
    since: str | None = None,
    limit: int | None = None,
    force: bool = False,
    max_retries: int = 3,
) -> dict:
    """Extract segments for conversations that don't have them yet (or all if force).

    Args:
        provider: LLMProvider instance (OllamaProvider or FixtureProvider).
        conversation_id: restrict to a single conversation.
        since: ISO date string; skip conversations updated before this date.
        limit: cap the number of conversations processed.
        force: if True, regenerate segments even if they already exist
               (locked_by_user=1 rows are always preserved).
        max_retries: per-conversation retry budget passed to extract_with_validation.

    Returns summary dict.
    """
    started_at = _now()
    scope = f"conversation:{conversation_id}" if conversation_id else "all"

    run_id = db.start_extraction_run(
        kind="segment",
        scope=scope,
        provider=provider.name,
        model=provider.model,
        prompt_version=PROMPT_VERSION,
        started_at=started_at,
    )

    prompt_template = _PROMPT_PATH.read_text(encoding="utf-8")
    generated_by = f"{provider.name}:{provider.model or 'unknown'}"

    total_input = 0
    total_output = 0
    total_retries = 0
    warnings: list[str] = []
    conv_count = 0
    seg_count = 0

    try:
        conversations = _load_conversations(conversation_id, since, limit, force)

        for conv in conversations:
            cid = conv["id"]
            conv_count += 1

            messages = _load_messages_for_prompt(cid)
            if not messages:
                log.debug("conversation %d has no messages, skipping", cid)
                continue

            msg_ids = [m["id"] for m in messages]
            msg_id_set = set(msg_ids)
            prompt = prompt_template.replace("{messages}", _format_messages(messages))

            # Retry loop: includes both provider-level and structural validation.
            feedback: list[str] = []
            succeeded = False
            conv_input = conv_output = conv_retries = 0
            for attempt in range(max_retries + 1):
                retry_prompt = prompt
                if feedback:
                    retry_prompt += "\n\nPREVIOUS ATTEMPT FAILED:\n" + "\n".join(feedback)
                    retry_prompt += "\nPlease fix the issues and try again."

                try:
                    result = extract_with_validation(
                        provider,
                        retry_prompt,
                        SEGMENT_SCHEMA,
                        system=_SYSTEM,
                        max_retries=0,  # inner loop handles only schema; outer handles structure
                    )
                except ValidationError as exc:
                    if attempt >= max_retries:
                        warnings.append(f"conv={cid}: validation exhausted: {exc}")
                        break
                    feedback.append(str(exc))
                    conv_retries += 1
                    continue

                conv_input += result.input_tokens
                conv_output += result.output_tokens
                conv_retries += result.retries

                # Structural validation (message ids, ordering, overlaps).
                try:
                    _validate_segment_list(result.data["segments"], msg_id_set, msg_ids)
                except ValidationError as exc:
                    if attempt >= max_retries:
                        warnings.append(f"conv={cid}: structural validation failed: {exc}")
                        break
                    feedback.append(
                        f"Structural error: {exc}. "
                        f"Valid message ids are: {sorted(msg_id_set)}"
                    )
                    conv_retries += 1
                    continue

                # All good — write and move on.
                try:
                    n = _write_segments(
                        cid, result.data["segments"], run_id, generated_by, force
                    )
                    seg_count += n
                    succeeded = True
                except Exception as exc:
                    warnings.append(f"conv={cid}: write failed: {exc}")
                break

            total_input += conv_input
            total_output += conv_output
            total_retries += conv_retries

        status = "partial" if warnings else "ok"
        db.finish_extraction_run(
            run_id,
            completed_at=_now(),
            status=status,
            input_token_count=total_input,
            output_token_count=total_output,
            retries=total_retries,
            warnings=warnings[:200],
        )
        log.info(
            "segment extraction done: convs=%d segs=%d retries=%d warnings=%d",
            conv_count, seg_count, total_retries, len(warnings),
        )

    except Exception as exc:
        db.finish_extraction_run(
            run_id, completed_at=_now(), status="failed", error=str(exc)
        )
        raise

    return {
        "conversations": conv_count,
        "segments": seg_count,
        "retries": total_retries,
        "warnings": len(warnings),
        "run_id": run_id,
    }


def _write_segments(
    conversation_id: int,
    raw_segments: list[dict],
    run_id: int,
    generated_by: str,
    force: bool,
) -> int:
    """Validate segment list, delete old unlocked rows if force, then insert.

    Returns number of segments written.
    """
    msg_ids = db.get_message_ids_for_conversation(conversation_id)
    msg_id_set = set(msg_ids)

    # Additional structural validation beyond JSON schema.
    _validate_segment_list(raw_segments, msg_id_set, msg_ids)

    if force:
        db.delete_unlocked_segments(conversation_id)

    ts = _now()
    written = 0
    for idx, seg in enumerate(raw_segments):
        try:
            db.insert_segment(
                conversation_id=conversation_id,
                idx=idx,
                start_message_id=seg["start_message_id"],
                end_message_id=seg["end_message_id"],
                title=seg["title"][:500],
                summary=seg["summary"][:5000],
                topics=json.dumps(seg.get("topics", []), ensure_ascii=False),
                generated_by=generated_by,
                prompt_version=PROMPT_VERSION,
                extraction_run_id=run_id,
                created_at=ts,
            )
            written += 1
        except Exception as exc:
            log.warning("insert_segment failed conv=%d idx=%d: %s", conversation_id, idx, exc)

    return written


def _validate_segment_list(
    segments: list[dict], msg_id_set: set[int], ordered_ids: list[int]
) -> None:
    """Raise ValidationError if segments fail structural checks."""
    if not segments:
        raise ValidationError("segments list is empty")

    id_to_pos = {mid: i for i, mid in enumerate(ordered_ids)}

    for i, seg in enumerate(segments):
        s = seg.get("start_message_id")
        e = seg.get("end_message_id")
        if s not in msg_id_set:
            raise ValidationError(f"segment[{i}].start_message_id {s} not in conversation")
        if e not in msg_id_set:
            raise ValidationError(f"segment[{i}].end_message_id {e} not in conversation")
        if id_to_pos[s] > id_to_pos[e]:
            raise ValidationError(
                f"segment[{i}] start {s} comes after end {e}"
            )
        if not seg.get("title", "").strip():
            raise ValidationError(f"segment[{i}] title is empty")
        if not seg.get("summary", "").strip():
            raise ValidationError(f"segment[{i}] summary is empty")

    # Check no overlaps: sort by start position and verify ranges don't cross.
    sorted_segs = sorted(segments, key=lambda s: id_to_pos[s["start_message_id"]])
    prev_end_pos = -1
    for seg in sorted_segs:
        start_pos = id_to_pos[seg["start_message_id"]]
        if start_pos <= prev_end_pos:
            raise ValidationError(
                f"segments overlap at message id {seg['start_message_id']}"
            )
        prev_end_pos = id_to_pos[seg["end_message_id"]]


def _load_conversations(
    conversation_id: int | None,
    since: str | None,
    limit: int | None,
    force: bool,
) -> list[dict]:
    if conversation_id:
        conn = db.connect()
        if not force:
            # Skip if already has segments.
            existing = conn.execute(
                "SELECT COUNT(*) FROM segments WHERE conversation_id=?", (conversation_id,)
            ).fetchone()[0]
            if existing:
                return []
        rows = conn.execute(
            "SELECT id FROM conversations WHERE id=?", (conversation_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    if force:
        conn = db.connect()
        conds, params = [], []
        if since:
            conds.append("updated_at >= ?")
            params.append(since)
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        q = f"SELECT id FROM conversations {where} ORDER BY id"
        if limit:
            q += f" LIMIT {int(limit)}"
        rows = conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]
    return db.conversations_without_segments(since=since, limit=limit)


def _load_messages_for_prompt(conversation_id: int) -> list[dict]:
    conn = db.connect()
    rows = conn.execute(
        "SELECT id, role, text FROM messages WHERE conversation_id=? ORDER BY id",
        (conversation_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _format_messages(messages: list[dict]) -> str:
    lines = []
    for m in messages:
        role = m.get("role", "?")
        text = (m.get("text") or "").replace("\n", " ")[:2000]
        lines.append(f"[id={m['id']} role={role}] {text}")
    return "\n".join(lines)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
