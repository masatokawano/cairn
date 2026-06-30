"""Assertion extraction runner (P3-D).

Calls an LLMProvider to extract claims, decisions, questions etc. from each
segment, validates actor/kind/status enums and grounding, and writes results
to the assertions table via extraction_runs.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from .. import db
from ..llm import LLMProvider, ValidationError
from .validate import GroundingContext, extract_with_validation

log = logging.getLogger(__name__)

PROMPT_VERSION = "assertion-v1"

_PROMPT_PATH = Path(__file__).parent / "prompts" / "assertion_v1.txt"

VALID_ACTORS = frozenset({"user", "assistant", "shared"})
VALID_KINDS = frozenset({
    "claim", "hypothesis", "conclusion", "decision",
    "rejected_idea", "question", "todo",
})
VALID_STATUSES = frozenset({
    "tentative", "accepted", "rejected", "superseded", "unresolved", "completed",
})

ASSERTION_SCHEMA = {
    "type": "object",
    "properties": {
        "assertions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "actor": {"type": "string", "enum": list(VALID_ACTORS)},
                    "kind": {"type": "string", "enum": list(VALID_KINDS)},
                    "status": {"type": "string", "enum": list(VALID_STATUSES)},
                    "confidence": {"type": "number"},
                    "supporting_message_ids": {
                        "type": "array", "items": {"type": "integer"}
                    },
                },
                "required": ["text", "actor", "kind", "status",
                             "confidence", "supporting_message_ids"],
            },
            "minItems": 1,
        }
    },
    "required": ["assertions"],
}

_SYSTEM = (
    "You are a knowledge extraction specialist. Extract structured assertions exactly "
    "as requested. Output ONLY the JSON object — no prose, no markdown fences."
)


def run_assertion_extraction(
    provider: LLMProvider,
    *,
    segment_id: int | None = None,
    since: str | None = None,
    limit: int | None = None,
    force: bool = False,
    max_retries: int = 3,
) -> dict:
    """Extract assertions from segments that don't have them yet (or all if force).

    Args:
        provider: LLMProvider instance.
        segment_id: restrict to a single segment.
        since: ISO date; skip segments created before this date.
        limit: cap the number of segments processed.
        force: regenerate even if assertions already exist (preserves locked rows).
        max_retries: retry budget per segment.

    Returns summary dict.
    """
    started_at = _now()
    scope = f"segment:{segment_id}" if segment_id else "all"

    run_id = db.start_extraction_run(
        kind="assertion",
        scope=scope,
        provider=provider.name,
        model=provider.model,
        prompt_version=PROMPT_VERSION,
        started_at=started_at,
    )

    prompt_template = _PROMPT_PATH.read_text(encoding="utf-8")
    generated_by = f"{provider.name}:{provider.model or 'unknown'}"

    total_input = total_output = total_retries = 0
    warnings: list[str] = []
    seg_count = assertion_count = 0

    try:
        segments = _load_segments(segment_id, since, limit, force)

        for seg in segments:
            sid = seg["id"]
            seg_count += 1

            messages = _load_messages_for_segment(sid)
            if not messages:
                log.debug("segment %d has no messages, skipping", sid)
                continue

            msg_ids = [m["id"] for m in messages]
            msg_id_set = set(msg_ids)

            prompt = (
                prompt_template
                .replace("{title}", seg.get("title", ""))
                .replace("{summary}", seg.get("summary", ""))
                .replace("{messages}", _format_messages(messages))
            )
            grounding = GroundingContext(valid_message_ids=msg_id_set)

            # Retry loop including grounding validation.
            feedback: list[str] = []
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
                        ASSERTION_SCHEMA,
                        grounding=grounding,
                        system=_SYSTEM,
                        max_tokens=4096,
                        max_retries=0,
                    )
                except ValidationError as exc:
                    if attempt >= max_retries:
                        warnings.append(f"seg={sid}: validation exhausted: {exc}")
                        break
                    feedback.append(str(exc))
                    conv_retries += 1
                    continue

                conv_input += result.input_tokens
                conv_output += result.output_tokens
                conv_retries += result.retries

                # Enum validation (actor/kind/status per item).
                enum_err = _validate_enum_fields(result.data["assertions"])
                if enum_err:
                    if attempt >= max_retries:
                        warnings.append(f"seg={sid}: enum error: {enum_err}")
                        break
                    feedback.append(
                        f"Enum error: {enum_err}. "
                        f"Valid actors: {sorted(VALID_ACTORS)}. "
                        f"Valid kinds: {sorted(VALID_KINDS)}. "
                        f"Valid statuses: {sorted(VALID_STATUSES)}."
                    )
                    conv_retries += 1
                    continue

                # Per-item grounding: supporting_message_ids must be within segment.
                ground_err = _validate_assertion_grounding(
                    result.data["assertions"], msg_id_set
                )
                if ground_err:
                    if attempt >= max_retries:
                        warnings.append(f"seg={sid}: grounding failed: {ground_err}")
                        break
                    feedback.append(
                        f"Grounding error: {ground_err}. "
                        f"Valid message ids are: {sorted(msg_id_set)}"
                    )
                    conv_retries += 1
                    continue

                # Write.
                try:
                    n = _write_assertions(
                        sid, seg["conversation_id"],
                        result.data["assertions"],
                        run_id, generated_by, force,
                    )
                    assertion_count += n
                except Exception as exc:
                    warnings.append(f"seg={sid}: write failed: {exc}")
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
            "assertion extraction done: segs=%d assertions=%d retries=%d warnings=%d",
            seg_count, assertion_count, total_retries, len(warnings),
        )

    except Exception as exc:
        db.finish_extraction_run(
            run_id, completed_at=_now(), status="failed", error=str(exc)
        )
        raise

    return {
        "segments": seg_count,
        "assertions": assertion_count,
        "retries": total_retries,
        "warnings": len(warnings),
        "run_id": run_id,
    }


def _write_assertions(
    segment_id: int,
    conversation_id: int,
    raw: list[dict],
    run_id: int,
    generated_by: str,
    force: bool,
) -> int:
    if force:
        db.delete_unlocked_assertions(segment_id)
    ts = _now()
    written = 0
    for item in raw:
        try:
            db.insert_assertion(
                segment_id=segment_id,
                conversation_id=conversation_id,
                text=item["text"][:2000],
                actor=item["actor"],
                kind=item["kind"],
                status=item.get("status", "tentative"),
                confidence=item.get("confidence"),
                supporting_message_ids=json.dumps(
                    item.get("supporting_message_ids", [])
                ),
                generated_by=generated_by,
                prompt_version=PROMPT_VERSION,
                extraction_run_id=run_id,
                created_at=ts,
            )
            written += 1
        except Exception as exc:
            log.warning("insert_assertion failed seg=%d: %s", segment_id, exc)
    return written


def _validate_assertion_grounding(
    assertions: list[dict], valid_ids: set[int]
) -> str | None:
    """Return error string if any supporting_message_ids are not in the segment."""
    for i, a in enumerate(assertions):
        ids = a.get("supporting_message_ids", [])
        invalid = [mid for mid in ids if mid not in valid_ids]
        if invalid:
            return (
                f"assertions[{i}].supporting_message_ids contains ids "
                f"not in segment: {invalid}"
            )
    return None


def _validate_enum_fields(assertions: list[dict]) -> str | None:
    for i, a in enumerate(assertions):
        if a.get("actor") not in VALID_ACTORS:
            return f"assertions[{i}].actor={a.get('actor')!r} not in {sorted(VALID_ACTORS)}"
        if a.get("kind") not in VALID_KINDS:
            return f"assertions[{i}].kind={a.get('kind')!r} not in {sorted(VALID_KINDS)}"
        if a.get("status") not in VALID_STATUSES:
            return f"assertions[{i}].status={a.get('status')!r} not in {sorted(VALID_STATUSES)}"
        conf = a.get("confidence")
        if conf is not None and not (0.0 <= float(conf) <= 1.0):
            return f"assertions[{i}].confidence={conf} out of range 0.0–1.0"
        if not a.get("text", "").strip():
            return f"assertions[{i}].text is empty"
    return None


def _load_segments(
    segment_id: int | None,
    since: str | None,
    limit: int | None,
    force: bool,
) -> list[dict]:
    conn = db.connect()
    if segment_id:
        if not force:
            existing = conn.execute(
                "SELECT COUNT(*) FROM assertions WHERE segment_id=?", (segment_id,)
            ).fetchone()[0]
            if existing:
                return []
        rows = conn.execute(
            "SELECT id, conversation_id, title, summary FROM segments WHERE id=?",
            (segment_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    if force:
        conds, params = [], []
        if since:
            conds.append("created_at >= ?")
            params.append(since)
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        q = f"SELECT id, conversation_id, title, summary FROM segments {where} ORDER BY id"
        if limit:
            q += f" LIMIT {int(limit)}"
        rows = conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]
    return db.segments_without_assertions(since=since, limit=limit)


def _load_messages_for_segment(segment_id: int) -> list[dict]:
    """Load messages that fall within the segment's start/end range."""
    conn = db.connect()
    seg = conn.execute(
        "SELECT start_message_id, end_message_id, conversation_id FROM segments WHERE id=?",
        (segment_id,),
    ).fetchone()
    if not seg:
        return []
    # Fetch all messages in conversation; filter by position between start and end.
    all_ids = conn.execute(
        "SELECT id FROM messages WHERE conversation_id=? ORDER BY id",
        (seg["conversation_id"],),
    ).fetchall()
    id_list = [r[0] for r in all_ids]
    try:
        s_pos = id_list.index(seg["start_message_id"])
        e_pos = id_list.index(seg["end_message_id"])
    except ValueError:
        return []
    segment_ids = id_list[s_pos: e_pos + 1]
    if not segment_ids:
        return []
    placeholders = ",".join("?" * len(segment_ids))
    rows = conn.execute(
        f"SELECT id, role, text FROM messages WHERE id IN ({placeholders}) ORDER BY id",
        segment_ids,
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
