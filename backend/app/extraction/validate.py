"""Validation layer for LLM-based extraction (Phase 3, P3-A).

extract_with_validation() wraps a LLMProvider call with:
  1. JSON schema validation (jsonschema if available, else lightweight check)
  2. Grounding validation — supporting_message_ids must be real DB ids
  3. Retry loop — up to max_retries attempts with feedback in the prompt
  4. Partial-failure tracking — failed items accumulate warnings rather than
     aborting the whole batch

This layer is provider-agnostic; it works identically with OllamaProvider
and FixtureProvider.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from ..llm import LLMProvider, ValidationError

_MAX_RETRIES_DEFAULT = int(os.environ.get("CAIRN_EXTRACT_MAX_RETRIES", "3"))

try:
    import jsonschema as _jsonschema
    _HAS_JSONSCHEMA = True
except ImportError:
    _HAS_JSONSCHEMA = False


@dataclass
class GroundingContext:
    """Constraints for grounding validation.

    valid_message_ids: set of message.id values that exist in the conversation
                       being processed. Any id in supporting_message_ids that
                       is not in this set triggers a retry.
    """
    valid_message_ids: set[int] = field(default_factory=set)


@dataclass
class ExtractionResult:
    data: dict
    retries: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    warnings: list[str] = field(default_factory=list)


def extract_with_validation(
    provider: LLMProvider,
    prompt: str,
    schema: dict,
    *,
    grounding: GroundingContext | None = None,
    max_retries: int = _MAX_RETRIES_DEFAULT,
    system: str | None = None,
    max_tokens: int = 2048,
) -> ExtractionResult:
    """Call *provider* and validate output, retrying up to *max_retries* times.

    On each failure the validation error is appended to the prompt as feedback
    so the model can self-correct. Raises ``ValidationError`` after exhausting
    all retries.
    """
    feedback: list[str] = []
    retries = 0
    input_tokens = provider.estimate_tokens((system or "") + prompt)

    for attempt in range(max_retries + 1):
        retry_prompt = prompt
        if feedback:
            retry_prompt += "\n\nPREVIOUS ATTEMPT FAILED:\n" + "\n".join(feedback)
            retry_prompt += "\nPlease fix the issues and try again."

        try:
            result = provider.complete_structured(
                retry_prompt,
                schema=schema,
                system=system,
                max_tokens=max_tokens,
            )
        except ValidationError as exc:
            if attempt >= max_retries:
                raise
            feedback.append(str(exc))
            retries += 1
            continue

        # JSON schema validation
        schema_error = _validate_schema(result, schema)
        if schema_error:
            if attempt >= max_retries:
                raise ValidationError(f"schema validation failed after {attempt + 1} attempts: {schema_error}")
            feedback.append(f"JSON schema error: {schema_error}")
            retries += 1
            continue

        # Grounding validation
        if grounding and grounding.valid_message_ids:
            ground_error = _validate_grounding(result, grounding)
            if ground_error:
                if attempt >= max_retries:
                    raise ValidationError(f"grounding failed after {attempt + 1} attempts: {ground_error}")
                allowed = sorted(grounding.valid_message_ids)
                feedback.append(
                    f"Grounding error: {ground_error}. "
                    f"Allowed message ids: {allowed}"
                )
                retries += 1
                continue

        output_tokens = provider.estimate_tokens(str(result))
        return ExtractionResult(
            data=result,
            retries=retries,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    # unreachable — loop always raises or returns
    raise ValidationError("extract_with_validation: unexpected exit")  # pragma: no cover


def _validate_schema(result: dict, schema: dict) -> str | None:
    """Return an error string if *result* does not conform to *schema*, else None."""
    if _HAS_JSONSCHEMA:
        try:
            _jsonschema.validate(result, schema)
            return None
        except _jsonschema.ValidationError as exc:
            return exc.message
    # Lightweight fallback: check required fields and basic types
    required = schema.get("required", [])
    for key in required:
        if key not in result:
            return f"missing required field: '{key}'"
    properties = schema.get("properties", {})
    for key, prop in properties.items():
        if key not in result:
            continue
        expected_type = prop.get("type")
        val = result[key]
        if expected_type == "string" and not isinstance(val, str):
            return f"field '{key}' must be a string, got {type(val).__name__}"
        if expected_type == "integer" and not isinstance(val, int):
            return f"field '{key}' must be an integer, got {type(val).__name__}"
        if expected_type == "array" and not isinstance(val, list):
            return f"field '{key}' must be an array, got {type(val).__name__}"
        enum = prop.get("enum")
        if enum and val not in enum:
            return f"field '{key}' value {val!r} not in enum {enum}"
    return None


def _validate_grounding(result: dict, grounding: GroundingContext) -> str | None:
    """Return error string if any supporting_message_ids are not in grounding."""
    ids = result.get("supporting_message_ids")
    if ids is None:
        return None
    if not isinstance(ids, list):
        return "supporting_message_ids must be an array"
    invalid = [i for i in ids if i not in grounding.valid_message_ids]
    if invalid:
        return f"message ids not in conversation: {invalid}"
    return None
