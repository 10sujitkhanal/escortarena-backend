import csv
from pathlib import Path

from django.core.management.base import CommandError


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        raise CommandError(f"File not found: {path}")
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))
