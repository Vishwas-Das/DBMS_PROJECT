"""BenchmarkRepo: persisted benchmark results."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select

from lae.db.models import BenchmarkRun
from lae.db.records import BenchmarkRunRecord
from lae.db.repositories._base import Repo


def _record(m: BenchmarkRun) -> BenchmarkRunRecord:
    return BenchmarkRunRecord(
        id=m.id,
        scenario=m.scenario,
        seed=m.seed,
        git_commit=m.git_commit,
        created_at=m.created_at,
        params=dict(m.params),
        environment=dict(m.environment),
        metrics=dict(m.metrics),
    )


class BenchmarkRepo(Repo):
    """Methods:
    add(scenario, *, params, seed, environment, metrics, git_commit=None) -> BenchmarkRunRecord
    list_runs(scenario=None, limit=100) -> list[BenchmarkRunRecord]          (newest first)
    """

    def add(
        self,
        scenario: str,
        *,
        params: Mapping[str, Any],
        seed: int,
        environment: Mapping[str, Any],
        metrics: Mapping[str, Any],
        git_commit: str | None = None,
    ) -> BenchmarkRunRecord:
        m = BenchmarkRun(
            scenario=scenario,
            params=dict(params),
            seed=seed,
            git_commit=git_commit,
            environment=dict(environment),
            metrics=dict(metrics),
        )
        self.session.add(m)
        self.session.flush()
        self.session.refresh(m)
        return _record(m)

    def list_runs(self, scenario: str | None = None, limit: int = 100) -> list[BenchmarkRunRecord]:
        q = select(BenchmarkRun).order_by(BenchmarkRun.created_at.desc(), BenchmarkRun.id.desc())
        if scenario is not None:
            q = q.where(BenchmarkRun.scenario == scenario)
        return [_record(m) for m in self.session.scalars(q.limit(limit))]
