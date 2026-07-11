"""Versioned metric catalog: definitions and alias mapping (H-D3/H-D4).

Definitions live in YAML next to this module (``catalog/``) so the mapping
is data, not code. Tests point ``load()`` at a synthetic catalog directory —
the packaged catalog contains only public medical vocabulary (metric names,
units, one well-known LOINC code), never personal data.

H1 normalization policy: identity units only. A source unit (after
``units.yml`` spelling normalization) that does not equal the metric's
canonical unit yields NO converted value — the original is preserved and the
observation is marked provisional. Unit conversion tables are a later phase.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DIR = Path(__file__).parent / "catalog"
NAMESPACE_LAB = "lab_sheet"


@dataclass(frozen=True)
class Metric:
    metric_id: str
    label_ja: str
    quantity_kind: str
    label_en: str | None = None
    canonical_unit: str | None = None
    loinc: str | None = None
    healthkit_identifier: str | None = None


@dataclass(frozen=True)
class Catalog:
    catalog_version: str
    mapping_version: str
    metrics: dict[str, Metric]
    aliases: dict[str, str]            # source spelling -> metric_id
    unit_spellings: dict[str, str] = field(default_factory=dict)

    def resolve_metric(self, source_name: str) -> Metric | None:
        metric_id = self.aliases.get(source_name.strip())
        if metric_id is None:
            return None
        return self.metrics.get(metric_id)

    def canonical_unit_for(self, raw_unit: str | None) -> str | None:
        """Normalize a unit spelling; None when the unit is unknown."""
        if raw_unit is None:
            return None
        cleaned = raw_unit.strip()
        return self.unit_spellings.get(cleaned)


def load(directory: Path | None = None) -> Catalog:
    import yaml

    directory = directory or DEFAULT_DIR
    metrics_doc = yaml.safe_load((directory / "metrics.yml").read_text("utf-8"))
    aliases_doc = yaml.safe_load((directory / "lab_aliases.yml").read_text("utf-8"))
    units_doc = yaml.safe_load((directory / "units.yml").read_text("utf-8"))

    metrics: dict[str, Metric] = {}
    for metric_id, spec in (metrics_doc.get("metrics") or {}).items():
        metrics[metric_id] = Metric(
            metric_id=metric_id,
            label_ja=spec["label_ja"],
            label_en=spec.get("label_en"),
            quantity_kind=spec["quantity_kind"],
            canonical_unit=spec.get("canonical_unit"),
            loinc=spec.get("loinc"),
            healthkit_identifier=spec.get("healthkit_identifier"),
        )

    aliases: dict[str, str] = {}
    for source_name, metric_id in (aliases_doc.get("aliases") or {}).items():
        if metric_id not in metrics:
            raise ValueError(
                f"alias {source_name!r} maps to unknown metric {metric_id!r}"
            )
        aliases[str(source_name).strip()] = metric_id
    # Every canonical metric id also resolves to itself.
    for metric_id in metrics:
        aliases.setdefault(metric_id, metric_id)

    unit_spellings: dict[str, str] = {}
    for canonical in units_doc.get("canonical") or []:
        unit_spellings[str(canonical)] = str(canonical)
    for spelling, canonical in (units_doc.get("spellings") or {}).items():
        unit_spellings[str(spelling)] = str(canonical)

    return Catalog(
        catalog_version=str(metrics_doc["catalog_version"]),
        mapping_version=str(aliases_doc["mapping_version"]),
        metrics=metrics,
        aliases=aliases,
        unit_spellings=unit_spellings,
    )


def refresh_store(conn, cat: Catalog) -> None:
    """Mirror the catalog into the store so the DB is self-describing.

    Wholesale replace: the YAML is the source of truth and versions are
    stamped on every row (and on every observation via mapping_version).
    """
    conn.execute("DELETE FROM metric_catalog")
    conn.execute("DELETE FROM metric_aliases")
    for m in cat.metrics.values():
        conn.execute(
            "INSERT INTO metric_catalog (metric_id, label_ja, label_en,"
            " quantity_kind, canonical_unit, loinc_code, healthkit_identifier,"
            " catalog_version, active) VALUES (?,?,?,?,?,?,?,?,TRUE)",
            [m.metric_id, m.label_ja, m.label_en, m.quantity_kind,
             m.canonical_unit, m.loinc, m.healthkit_identifier,
             cat.catalog_version],
        )
    for source_name, metric_id in cat.aliases.items():
        conn.execute(
            "INSERT INTO metric_aliases (source_namespace, source_name,"
            " metric_id, mapping_version, confidence) VALUES (?,?,?,?,?)",
            [NAMESPACE_LAB, source_name, metric_id, cat.mapping_version,
             "confirmed"],
        )
