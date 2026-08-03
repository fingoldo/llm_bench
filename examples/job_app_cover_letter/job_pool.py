"""JobPool - TaskPool reading scraped JobApp jobs from a CSV.

Demonstrates the no-DB / file-only path for the framework: the
benchmark's task units come from a CSV the operator dropped into
``examples/job_app_cover_letter/data/jobs.csv`` (or a path the operator
passes on the CLI).

Stratification key is ``industry`` - benchmark scoring then splits
naturally per vertical (a model that nails saas but bombs fintech is
still preserved as a per-stratum specialist via the framework's
per-stage-winner force-promote rule).
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path

from llm_bench import TaskUnit

logger = logging.getLogger(__name__)


@dataclass
class JobPool:
    """CSV-backed task pool for the JobApp cover-letter benchmark.

    The CSV must have columns:
        job_id, industry, company, title, description, required_skills, budget_usd

    All non-empty rows become TaskUnits. ``required_skills`` is split on
    commas and dropped into metadata as a list. ``industry`` is the
    stratum.
    """
    jobs_csv: str | Path

    _units: list[TaskUnit] | None = None

    def _load(self) -> list[TaskUnit]:
        if self._units is not None:
            return self._units
        path = Path(self.jobs_csv)
        if not path.exists():
            raise FileNotFoundError(f"Job CSV not found: {path}")
        units: list[TaskUnit] = []
        with path.open(encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                jid = (row.get("job_id") or "").strip()
                if not jid:
                    continue
                metadata = {
                    "company": row.get("company", "").strip(),
                    "title": row.get("title", "").strip(),
                    "description": row.get("description", "").strip(),
                    "required_skills": [s.strip() for s in row.get("required_skills", "").split(",") if s.strip()],
                    "budget_usd": _parse_float(row.get("budget_usd")),
                }
                units.append(TaskUnit(
                    id=jid,
                    metadata=metadata,
                    stratum=row.get("industry", "").strip() or None,
                ))
        logger.info("[job_app] loaded %d jobs from %s", len(units), path)
        self._units = units
        return units

    def sample(self, n: int) -> list[TaskUnit]:
        return list(self._load()[:n])

    def total_size(self) -> int:
        return len(self._load())


def _parse_float(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None
