"""Health importers. Common contract (docs/health/DESIGN.md §4.1): hash the
source, snapshot it immutably to raw/, register source_files + import_runs,
normalize inside one transaction, quarantine instead of guessing, redact
logs (counts and ids only — never values, metric names or absolute paths).
"""
