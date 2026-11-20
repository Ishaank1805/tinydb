"""Token accounting and budgets (tokens, estimated USD, wall-clock)."""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Usage:
    input: int = 0          # uncached input tokens
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    def __add__(self, other: 'Usage') -> 'Usage':
        return Usage(self.input + other.input, self.output + other.output,
                     self.cache_read + other.cache_read, self.cache_write + other.cache_write)

    @property
    def billable(self) -> int:
        """What the token budget counts: cache reads are cheap and excluded."""
        return self.input + self.cache_write + self.output

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write

    def as_dict(self) -> dict:
        return {**asdict(self), 'total': self.total}


class BudgetExceeded(Exception):
    """Raised when a job runs out of tokens, money or time."""


class UsageLedger:
    def __init__(self, path: str | Path, pricing: dict | None = None, job: str = ''):
        self.path = Path(path)
        self.pricing = pricing or {}
        self.job = job
        self.records: list[dict] = []

    def add(self, role: str, stage: str, model: str, usage: Usage) -> None:
        self.records.append({'job': self.job, 'role': role, 'stage': stage, 'model': model,
                             **asdict(usage)})
        self.save()

    def totals(self) -> Usage:
        total = Usage()
        for rec in self.records:
            total = total + Usage(rec['input'], rec['output'], rec['cache_read'], rec['cache_write'])
        return total

    def cost(self) -> float:
        usd = 0.0
        for rec in self.records:
            price = self.pricing.get(rec['model'])
            if not price:
                continue
            usd += (rec['input'] * price.get('input', 0) + rec['output'] * price.get('output', 0)
                    + rec['cache_read'] * price.get('cache_read', 0)
                    + rec['cache_write'] * price.get('cache_write', 0)) / 1_000_000
        return round(usd, 4)

    def unpriced_models(self) -> list[str]:
        return sorted({r['model'] for r in self.records if r['model'] not in self.pricing})

    def summary(self) -> dict:
        by_role: dict[str, dict] = {}
        for rec in self.records:
            slot = by_role.setdefault(rec['role'], {'calls': 0, 'input': 0, 'output': 0,
                                                    'cache_read': 0, 'cache_write': 0,
                                                    'model': rec['model']})
            slot['calls'] += 1
            for key in ('input', 'output', 'cache_read', 'cache_write'):
                slot[key] += rec[key]
        return {'job': self.job, 'totals': self.totals().as_dict(), 'est_cost_usd': self.cost(),
                'unpriced_models': self.unpriced_models(), 'by_role': by_role,
                'calls': len(self.records)}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({'records': self.records, 'summary': self.summary()},
                                        indent=2), encoding='utf-8')


class Budget:
    def __init__(self, cfg, ledger: UsageLedger, started: float | None = None):
        self.cfg = cfg
        self.ledger = ledger
        self.started = started or time.time()

    def elapsed_minutes(self) -> float:
        return (time.time() - self.started) / 60

    def check(self) -> None:
        totals = self.ledger.totals()
        max_tokens = self.cfg.get('budgets.max_tokens_per_job')
        if max_tokens and totals.billable > max_tokens:
            raise BudgetExceeded(f'token budget exhausted ({totals.billable} > {max_tokens} '
                                 f'uncached-input + cache-write + output tokens)')
        max_usd = self.cfg.get('budgets.max_usd_per_job')
        if max_usd and self.ledger.cost() > max_usd:
            raise BudgetExceeded(f'cost budget exhausted (~${self.ledger.cost():.2f} > ${max_usd})')
        minutes = self.cfg.get('budgets.job_minutes')
        if minutes and self.elapsed_minutes() > minutes:
            raise BudgetExceeded(f'time budget exhausted ({self.elapsed_minutes():.0f} > {minutes} min)')
