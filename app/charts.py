"""Deterministic chart selection from a result's shape. A chart is a small spec the UI renders; no model call."""
from __future__ import annotations

from .executor import QueryResult

MAX_BARS = 25
MAX_PIE = 6


def pick_chart(result: QueryResult, question: str = "") -> dict | None:
    cols, kinds, rows = result.columns, result.types, result.rows
    if len(rows) < 2 or len(cols) < 2:
        return None
    dates = [i for i, k in enumerate(kinds) if k == "date"]
    nums = [i for i, k in enumerate(kinds) if k == "number"]
    texts = [i for i, k in enumerate(kinds) if k == "text"]
    if not nums:
        return None
    # time series: a date column plus one or more numbers
    if dates:
        x = dates[0]
        ys = [i for i in nums if i != x][:3]
        if ys:
            return {"type": "line", "x": cols[x], "y": [cols[i] for i in ys], "title": " / ".join(cols[i] for i in ys) + f" by {cols[x]}"}
    # category + number
    if texts or len(nums) >= 2:
        if texts:
            x, y = texts[0], nums[0]
        else:
            x, y = nums[0], nums[1]
            if len(rows) > MAX_BARS:
                return None
        if len(rows) > MAX_BARS:
            return None
        share_like = any(w in cols[y].lower() for w in ("pct", "percent", "share", "ratio"))
        asks_share = any(w in question.lower() for w in ("share", "proportion", "percentage of", "breakdown", "split"))
        if len(rows) <= MAX_PIE and (share_like or asks_share) and all((r[y] or 0) >= 0 for r in rows):
            return {"type": "pie", "x": cols[x], "y": [cols[y]], "title": f"{cols[y]} by {cols[x]}"}
        long_labels = max((len(str(r[x])) for r in rows), default=0) > 14 or len(rows) > 8
        return {"type": "bar", "x": cols[x], "y": [cols[y]], "horizontal": long_labels, "title": f"{cols[y]} by {cols[x]}"}
    return None
