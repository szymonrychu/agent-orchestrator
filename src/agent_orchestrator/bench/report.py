"""Per-arm aggregation and the summary table."""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Mapping
from typing import Any

_COLUMNS = ("arm", "runs", "mean_wall_s", "p50_wall_s", "failures", "mean_tokens", "verify_pass")


def summarize(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One row per arm, in first-seen order.

    failures counts runs whose turn did not complete; wall figures cover completed runs only.
    """
    arms: dict[str, list[Mapping[str, Any]]] = {}
    for rec in records:
        arms.setdefault(rec["arm"], []).append(rec)
    rows = []
    for arm, recs in arms.items():
        walls = [r["wall_seconds"] for r in recs if r["turn_status"] == "completed"]
        tokens = [(r["input_tokens"] or 0) + (r["output_tokens"] or 0) for r in recs]
        rows.append(
            {
                "arm": arm,
                "runs": len(recs),
                "mean_wall_s": statistics.fmean(walls) if walls else None,
                "p50_wall_s": statistics.median(walls) if walls else None,
                "failures": sum(1 for r in recs if r["turn_status"] != "completed"),
                "mean_tokens": statistics.fmean(tokens),
                "verify_pass": sum(1 for r in recs if r["verify_passed"]) / len(recs),
            }
        )
    return rows


def _cell(key: str, value: Any) -> str:
    if value is None:
        return "-"
    if key == "verify_pass":
        return f"{value * 100:.0f}%"
    if key in ("mean_wall_s", "p50_wall_s"):
        return f"{value:.1f}"
    if key == "mean_tokens":
        return f"{value:.0f}"
    return str(value)


def format_table(rows: list[dict[str, Any]]) -> str:
    cells = [[_cell(c, row[c]) for c in _COLUMNS] for row in rows]
    widths = [max(len(c), *(len(r[i]) for r in cells)) for i, c in enumerate(_COLUMNS)]

    def line(values: list[str]) -> str:
        padded = [
            v.ljust(w) if i == 0 else v.rjust(w)
            for i, (v, w) in enumerate(zip(values, widths, strict=True))
        ]
        return "  ".join(padded).rstrip()

    return "\n".join([line(list(_COLUMNS)), line(["-" * w for w in widths]), *map(line, cells)])
