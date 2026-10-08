"""Data and analytics: clean, pivot, compare, statistics, plotting.

Works on CSV files with the standard library where that is enough, and names
the package to install when a real analysis needs one. It never invents numbers:
a statistic it cannot compute is reported as unavailable.
"""
from __future__ import annotations

import csv
import json
import statistics
from pathlib import Path

from ..errors import ActionError
from ..logging_setup import get_logger

from . import filesystem, shell

log = get_logger(__name__)


def _rows(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _path(target: str, args: str) -> Path:
    candidate = target if target.endswith(".csv") else args
    return Path(filesystem.expand(candidate or ""))


def clean_csv(target: str = "", args: str = "") -> str:
    """Strip empty rows, trim whitespace, and report what changed."""
    path = _path(target, args)
    if not path.is_file():
        return f"no such file: {path}"
    rows = _rows(path)
    cleaned, dropped = [], 0
    for row in rows:
        if not any(str(v).strip() for v in row.values()):
            dropped += 1
            continue
        cleaned.append({(k or "").strip(): (v or "").strip() for k, v in row.items()})
    out = path.with_name(f"{path.stem}_clean.csv")
    with open(out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(cleaned[0]) if cleaned else [])
        writer.writeheader()
        writer.writerows(cleaned)
    return f"{out.name}: {len(cleaned)} row(s) kept, {dropped} empty row(s) dropped"


def pivot(target: str = "", args: str = "") -> str:
    """Sum a numeric column, grouped by another. args is 'group|value'."""
    path = _path(target, args)
    if not path.is_file():
        return f"no such file: {path}"
    group_col, _, value_col = args.partition("|")
    if not value_col:
        return 'say which columns, e.g. "sales.csv region|amount"'
    rows = _rows(path)
    if not rows or group_col not in rows[0] or value_col not in rows[0]:
        return (f"{path.name} has no {group_col!r} and {value_col!r} columns. "
                f"It has: {', '.join(rows[0]) if rows else 'nothing'}")
    totals: dict[str, float] = {}
    for row in rows:
        try:
            amount = float(str(row[value_col]).replace(",", "").replace("$", ""))
        except (TypeError, ValueError):
            continue
        totals[row[group_col]] = totals.get(row[group_col], 0.0) + amount
    width = max((len(k) for k in totals), default=4)
    lines = [f"{k:<{width}}  {v:>14,.2f}" for k, v in sorted(totals.items())]
    lines.append(f"{'TOTAL':<{width}}  {sum(totals.values()):>14,.2f}")
    return "\n".join(lines)


def plot(target: str = "", args: str = "") -> str:
    """Bar chart of a pivot. Needs matplotlib."""
    path = _path(target, args)
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        raise ActionError("plotting needs matplotlib: `pip install matplotlib`")
    group_col, _, value_col = args.partition("|")
    rows = _rows(path)
    totals: dict[str, float] = {}
    for row in rows:
        try:
            totals[row[group_col]] = totals.get(row[group_col], 0.0) + float(
                str(row[value_col]).replace(",", "").replace("$", ""))
        except (KeyError, TypeError, ValueError):
            continue
    if not totals:
        return f"no numeric {value_col!r} values in {path.name}"
    figure, axes = plt.subplots(figsize=(8, 4.5))
    axes.bar(list(totals), list(totals.values()))
    axes.set_title(f"{value_col} by {group_col}")
    axes.tick_params(axis="x", rotation=30)
    figure.tight_layout()
    out = path.with_suffix(".png")
    figure.savefig(out, dpi=120)
    plt.close(figure)
    return f"chart written to {out}"


def compare(target: str = "", args: str = "") -> str:
    """Compare two CSVs by their common numeric columns."""
    first, _, second = f"{target} {args}".partition("|")
    left, right = Path(filesystem.expand(first.strip())), Path(filesystem.expand(second.strip()))
    if not (left.is_file() and right.is_file()):
        return "give me two CSV paths: 'a.csv|b.csv'"
    left_rows, right_rows = _rows(left), _rows(right)
    keys = [c for c in (left_rows[0] if left_rows else {})
            if c in (right_rows[0] if right_rows else {})]
    numeric = [c for c in keys
               if all(_is_number(r.get(c, "")) for r in (left_rows + right_rows))]
    if not numeric:
        return f"no numeric columns shared by both files: {', '.join(keys) or 'none'}"
    lines = [f"{'column':<20} {left.name:<14} {right.name:<14} change"]
    for column in numeric:
        a = statistics.fmean(float(r[column]) for r in left_rows)
        b = statistics.fmean(float(r[column]) for r in right_rows)
        change = f"{(b - a) / a * 100:+.1f}%" if a else "n/a"
        lines.append(f"{column:<20} {a:>14,.2f} {b:>14,.2f} {change}")
    return "\n".join(lines)


def _is_number(value) -> bool:
    try:
        float(str(value).replace(",", "").replace("$", ""))
        return True
    except (TypeError, ValueError):
        return False


def outliers(target: str = "", args: str = "") -> str:
    """Values more than two standard deviations from the mean."""
    path = _path(target, args)
    column = args.strip() if path.is_file() else target.strip()
    if not path.is_file():
        return f"no such file: {path}"
    rows = _rows(path)
    values = []
    for row in rows:
        for name, raw in row.items():
            if not column or name == column:
                try:
                    values.append((float(str(raw).replace(",", "")), name))
                except (TypeError, ValueError):
                    continue
    if len(values) < 3:
        return "not enough numeric values to judge outliers"
    numbers = [v for v, _ in values]
    mean, deviation = statistics.fmean(numbers), statistics.pstdev(numbers)
    if not deviation:
        return "every value is the same"
    flagged = [(v, c) for v, c in values if abs(v - mean) > 2 * deviation]
    if not flagged:
        return f"nothing stands out in {path.name} (mean {mean:,.2f})"
    return "\n".join(f"{name}: {v:,.2f}" for v, name in sorted(flagged, reverse=True))


def merge(target: str = "", args: str = "") -> str:
    """Stack several CSVs with the same columns into one."""
    parts = f"{target} {args}".split("|")
    files = [Path(filesystem.expand(p.strip())) for p in parts]
    files = [f for f in files if f.is_file()]
    if len(files) < 2:
        return "give me the CSVs to merge: 'a.csv|b.csv|c.csv'"
    merged: list[dict] = []
    for item in files:
        merged.extend(_rows(item))
    if not merged:
        return "those files have no rows"
    out = files[0].parent / "merged.csv"
    with open(out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(merged[0]))
        writer.writeheader()
        writer.writerows(merged)
    return f"merged {len(files)} file(s), {len(merged)} row(s) into {out.name}"


def describe(target: str = "", args: str = "") -> str:
    """Column summary: count, mean, min, max."""
    path = _path(target, args)
    if not path.is_file():
        return f"no such file: {path}"
    rows = _rows(path)
    if not rows:
        return f"{path.name} is empty"
    lines = [f"{path.name}: {len(rows)} row(s), {len(rows[0])} column(s)", ""]
    for column in rows[0]:
        numbers = []
        for row in rows:
            try:
                numbers.append(float(str(row[column]).replace(",", "")))
            except (TypeError, ValueError):
                continue
        if numbers:
            lines.append(f"  {column:<20} mean {statistics.fmean(numbers):>12,.2f}  "
                         f"min {min(numbers):>12,.2f}  max {max(numbers):>12,.2f}")
        else:
            lines.append(f"  {column:<20} text")
    return "\n".join(lines)


def to_json(target: str = "", args: str = "") -> str:
    path = _path(target, args)
    if not path.is_file():
        return f"no such file: {path}"
    out = path.with_suffix(".json")
    out.write_text(json.dumps(_rows(path), indent=2, ensure_ascii=False),
                   encoding="utf-8")
    return f"wrote {out.name}"