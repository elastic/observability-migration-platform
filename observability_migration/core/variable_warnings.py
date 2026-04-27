"""Structured warning catalog for variable-control translation.

See docs/roadmap/2026-04-27-kibana-variable-controls-design.md §10.

Phase B introduces this catalog as the canonical source of warning text
for variable-control translation. Adapter call sites that emit
"Variable-driven label filters …" / "Dropped variable-driven …" / etc.
will adopt these structured IDs in Task 18 and follow-ups so the
migration trace's "Top Warning Patterns" aggregator can group by a
stable ID instead of a brittle exact-string match.
"""
from __future__ import annotations

from typing import Final

WARNING_TEMPLATES: Final[dict[str, str]] = {
    "variable.bound":
        "filter applied via ES|QL parameter ?{var} (field={field}, kind={kind})",
    "variable.unbound.classic_only":
        "variable {var} not bound to translated ES|QL panel queries (reason: "
        "{reason}); the dashboard's classic control still applies to any "
        "KQL/Lens panels added manually",
    "variable.unbound.dropped":
        "variable {var} dropped during translation (reason: {reason}); "
        "no equivalent filter applied",
    "variable.verifier_downgraded":
        "variable {var} accepted by classifier but downgraded post-translation "
        "(verifier failure: {invariant}); falling back to classic control",
}


def render(warning_id: str, **kwargs: object) -> str:
    """Render a structured warning by ID with the given format kwargs.

    Raises KeyError if the ID is not in WARNING_TEMPLATES.
    """
    template = WARNING_TEMPLATES[warning_id]
    return template.format(**kwargs)


__all__ = ["WARNING_TEMPLATES", "render"]
