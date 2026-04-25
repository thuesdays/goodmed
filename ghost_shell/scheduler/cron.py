"""
cron.py — Minimal 5-field cron parser.

Fields (in order):
    minute        0-59
    hour          0-23
    day-of-month  1-31
    month         1-12
    day-of-week   0-6      (0 = Sunday)

Syntax per field:
    *              any value
    *​/N            every N (stepping)
    N              literal
    N-M            range inclusive
    N,M,P          list of literals
    N-M/S          range with step

Not supported (keep it lean):
    @hourly / @daily / @reboot shortcuts (handle at the UI layer)
    Names (mon / jan / etc) — always numeric
    Seconds field (5-field only)

Public API:
    parse(expr)           → Parsed (raises ValueError on bad input)
    next_fire(expr, now)  → datetime of next matching minute
    next_n(expr, n, now)  → list of datetimes
    describe(expr)        → short human string for the UI preview

The "increment minute-by-minute until a match" loop is dumb but
cron expressions only permit up to 366 * 24 * 60 ≈ 526k minutes
in a year, and even naive iteration clears 100k/s — we cap at 1
year lookahead which means worst-case ~5s blocking. That's fine
given we call this at startup + after each run, not per-frame.
"""

from __future__ import annotations

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

from datetime import datetime, timedelta

FIELD_BOUNDS = [
    (0, 59),   # minute
    (0, 23),   # hour
    (1, 31),   # day-of-month
    (1, 12),   # month
    (0, 6),    # day-of-week
]
FIELD_NAMES = ["minute", "hour", "dom", "month", "dow"]


class Parsed:
    """One cron field per list in the `sets` attribute."""
    def __init__(self, expr: str, sets: list[set[int]]):
        self.expr = expr
        self.sets = sets   # [minute_set, hour_set, dom_set, month_set, dow_set]

    def matches(self, dt: datetime) -> bool:
        mi, hr, dm, mo, dw = self.sets
        # Python's weekday(): Mon=0..Sun=6. Cron's DOW: Sun=0..Sat=6.
        # Convert: (python_weekday + 1) % 7 == cron_dow
        cron_dow = (dt.weekday() + 1) % 7
        return (dt.minute  in mi
            and dt.hour    in hr
            and dt.day     in dm
            and dt.month   in mo
            and cron_dow   in dw)


def _parse_field(raw: str, lo: int, hi: int, name: str) -> set[int]:
    """Expand a single field into its set of matching values."""
    raw = raw.strip()
    if not raw:
        raise ValueError(f"empty {name} field")
    out: set[int] = set()

    for part in raw.split(","):
        part = part.strip()
        step = 1
        if "/" in part:
            base, step_str = part.split("/", 1)
            try:
                step = int(step_str)
                if step <= 0: raise ValueError
            except ValueError:
                raise ValueError(f"bad step in {name}: {part!r}")
        else:
            base = part

        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a, b = base.split("-", 1)
            try:
                start, end = int(a), int(b)
            except ValueError:
                raise ValueError(f"bad range in {name}: {part!r}")
            if start < lo or end > hi or start > end:
                raise ValueError(f"range {start}-{end} out of bounds for {name} ({lo}-{hi})")
        else:
            try:
                start = end = int(base)
            except ValueError:
                raise ValueError(f"bad number in {name}: {part!r}")
            if start < lo or start > hi:
                raise ValueError(f"{name} value {start} out of bounds ({lo}-{hi})")

        for v in range(start, end + 1, step):
            out.add(v)

    if not out:
        raise ValueError(f"{name} field matches nothing: {raw!r}")
    return out


def parse(expr: str) -> Parsed:
    """Parse a full 5-field cron expression. Raises ValueError on any issue."""
    if not expr or not expr.strip():
        raise ValueError("empty cron expression")
    fields = expr.strip().split()
    if len(fields) != 5:
        raise ValueError(f"expected 5 fields, got {len(fields)}: {expr!r}")
    sets = [
        _parse_field(fields[i], *FIELD_BOUNDS[i], name=FIELD_NAMES[i])
        for i in range(5)
    ]
    return Parsed(expr, sets)


def next_fire(expr: str | Parsed, start: datetime = None) -> datetime | None:
    """Return the first datetime >= start (truncated to minute) that matches.

    Returns None if nothing matches within 1 year (shouldn't happen for
    well-formed expressions, but bail rather than loop forever if an
    impossible combo like "Feb 31" slips through).
    """
    p = expr if isinstance(expr, Parsed) else parse(expr)
    dt = (start or datetime.now()).replace(second=0, microsecond=0) + timedelta(minutes=1)
    horizon = dt + timedelta(days=366)
    while dt < horizon:
        if p.matches(dt):
            return dt
        dt += timedelta(minutes=1)
    return None


def next_n(expr: str | Parsed, n: int = 5,
           start: datetime = None) -> list[datetime]:
    """Return the next N matching datetimes (oldest → newest)."""
    p = expr if isinstance(expr, Parsed) else parse(expr)
    out: list[datetime] = []
    cur = start or datetime.now()
    for _ in range(n):
        nxt = next_fire(p, cur)
        if nxt is None:
            break
        out.append(nxt)
        cur = nxt + timedelta(seconds=1)
    return out


def describe(expr: str) -> str:
    """Short English-ish summary for UI tooltip / preview. Not i18n."""
    try:
        p = parse(expr)
    except ValueError as e:
        return f"invalid: {e}"
    mi, hr, dm, mo, dw = p.sets
    parts = []
    mi_count = len(mi); hr_count = len(hr)
    if mi_count == 60 and hr_count == 24:
        parts.append("every minute")
    elif mi_count == 1 and hr_count == 24:
        parts.append(f"every hour at :{next(iter(mi)):02d}")
    elif hr_count == 24 and len(mi) < 60:
        parts.append(f"every hour at minutes {sorted(mi)}")
    elif len(hr) == 1 and len(mi) == 1:
        parts.append(f"at {next(iter(hr)):02d}:{next(iter(mi)):02d}")
    else:
        parts.append(f"{len(mi)}×{len(hr)} min/hour combos")

    if len(dw) < 7:
        day_names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
        parts.append("on " + ", ".join(day_names[d] for d in sorted(dw)))
    if len(mo) < 12:
        parts.append(f"in months {sorted(mo)}")
    if len(dm) < 31:
        parts.append(f"on days {sorted(dm)}")
    return "; ".join(parts)
