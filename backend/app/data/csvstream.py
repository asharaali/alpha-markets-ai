"""Column-selective streaming CSV reader.

nflverse play-by-play is ~380 columns wide and ~50k rows a season. Building a 380-key dict
per row costs about 19 million dict entries per season, which is both slow and needlessly
heavy — so we resolve the handful of columns we actually want to positional indices once,
then read with csv.reader and pull by index.

Transparently handles .gz. Missing columns are reported by name up front rather than
producing silent Nones halfway through an aggregation.
"""
from __future__ import annotations

import csv
import gzip
import io
import sys
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Sequence

from app.core.errors import UpstreamError
from app.core.logging import get_logger

log = get_logger(__name__)

# Play-by-play has single fields (e.g. play descriptions) that exceed the default limit.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


def _open(path: Path) -> io.TextIOBase:
    if path.suffix == ".gz":
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8", newline="")
    return path.open("r", encoding="utf-8", newline="")


def header(path: Path) -> List[str]:
    with _open(path) as fh:
        return next(csv.reader(fh), [])


def stream(path: Path, columns: Sequence[str], *,
           where: Optional[Callable[[Dict[str, str]], bool]] = None,
           strict: bool = False) -> Iterator[Dict[str, str]]:
    """Yield one dict per row containing only `columns`.

    `strict=True` raises when a requested column is absent (use it for columns the caller
    genuinely cannot work without); otherwise the missing column is simply omitted from
    every row and logged once, which lets the app survive an upstream schema addition.
    """
    with _open(path) as fh:
        reader = csv.reader(fh)
        head = next(reader, None)
        if not head:
            raise UpstreamError(f"{path.name} is empty")
        index = {name: i for i, name in enumerate(head)}
        missing = [c for c in columns if c not in index]
        if missing:
            msg = f"{path.name} is missing columns: {', '.join(missing)}"
            if strict:
                raise UpstreamError(msg)
            log.warning(msg)
        picks = [(c, index[c]) for c in columns if c in index]
        width = len(head)
        for row in reader:
            if len(row) < width:
                continue  # truncated final line
            record = {name: row[i] for name, i in picks}
            if where is None or where(record):
                yield record


def num(value: Optional[str], default: Optional[float] = None) -> Optional[float]:
    """Parse an nflverse numeric cell. Empty and 'NA' both mean missing, not zero."""
    if value is None:
        return default
    v = value.strip()
    if not v or v in {"NA", "NaN", "nan", "None", "null"}:
        return default
    try:
        return float(v)
    except ValueError:
        return default


def integer(value: Optional[str], default: Optional[int] = None) -> Optional[int]:
    f = num(value)
    return default if f is None else int(f)


def flag(value: Optional[str]) -> bool:
    return (value or "").strip() in {"1", "1.0", "TRUE", "True", "true", "T"}


def text(value: Optional[str]) -> Optional[str]:
    v = (value or "").strip()
    return v if v and v not in {"NA", "None", "null"} else None
