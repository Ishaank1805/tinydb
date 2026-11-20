"""Configuration: ``autose.config.yml`` plus an optional experiment variant."""
from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any

import yaml

HARNESS_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = HARNESS_DIR / 'autose.config.yml'
_MISSING = object()

REQUIRED_KEYS = (
    'repository.allowed', 'repository.branch_prefix', 'trigger.label', 'trigger.command',
    'target.setup', 'target.build', 'target.test', 'target.collect', 'target.source_globs',
    'target.new_test_file', 'target.hidden_globs', 'target.protected_globs',
    'pipeline.max_impl_attempts', 'pipeline.max_redesigns', 'pipeline.max_change_requests',
    'agents', 'providers',
)


class ConfigError(Exception):
    """Raised for missing or invalid configuration."""


def deep_merge(base: dict, override: dict | None) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


class Config:
    """Read-only view of the merged configuration (base file + variant)."""

    def __init__(self, raw: dict, variant: str | None = None, path: Path | None = None):
        self.raw = raw or {}
        self.path = path
        self.variant = variant or 'baseline'
        variants = self.raw.get('variants') or {'baseline': {}}
        if self.variant not in variants:
            raise ConfigError(f'unknown variant {self.variant!r} (known: {", ".join(sorted(variants))})')
        base = {k: v for k, v in self.raw.items() if k != 'variants'}
        self.data = deep_merge(base, variants.get(self.variant) or {})

    @classmethod
    def load(cls, path: str | Path | None = None, variant: str | None = None) -> 'Config':
        path = Path(path or os.environ.get('AUTOSE_CONFIG') or DEFAULT_CONFIG)
        raw = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
        if variant is None:
            variant = os.environ.get('AUTOSE_VARIANT') or None
        return cls(raw, variant, path)

    def variants(self) -> list[str]:
        return sorted((self.raw.get('variants') or {'baseline': {}}).keys())

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in dotted.split('.'):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def req(self, dotted: str) -> Any:
        value = self.get(dotted, _MISSING)
        if value is _MISSING:
            raise ConfigError(f'missing configuration key: {dotted}')
        return value

    def agent(self, role: str) -> dict:
        spec = self.get(f'agents.{role}')
        if not isinstance(spec, dict):
            raise ConfigError(f'no agent configured for role {role!r}')
        return spec

    def provider(self, name: str) -> dict:
        spec = self.get(f'providers.{name}')
        if not isinstance(spec, dict):
            raise ConfigError(f'unknown provider {name!r}')
        return spec

    def roles_in_use(self) -> list[str]:
        roles = ['planner', 'implementer', 'test_author', 'reviewer']
        if self.get('judge.enabled', True):
            roles.append('judge')
        if self.get('pipeline.shared_context'):
            roles.append('generalist')
        return roles

    def validate(self) -> list[str]:
        problems: list[str] = []
        for key in REQUIRED_KEYS:
            if self.get(key, _MISSING) is _MISSING:
                problems.append(f'missing key {key}')
        allowed = self.get('repository.allowed') or []
        if not allowed:
            problems.append('repository.allowed is empty')
        if any(str(a).startswith('OWNER/') for a in allowed):
            problems.append('repository.allowed still contains the OWNER placeholder')
        if '{issue}' not in str(self.get('target.new_test_file', '')):
            problems.append('target.new_test_file must contain {issue}')
        if self.get('pipeline.max_change_requests') not in (0, 1, 2):
            problems.append('pipeline.max_change_requests must be 0, 1 or 2')
        if self.get('pipeline.fail_to_pass', 'soft') not in ('soft', 'hard', 'off'):
            problems.append('pipeline.fail_to_pass must be soft, hard or off')
        for role in self.roles_in_use():
            try:
                spec = self.agent(role)
                prov = self.provider(spec.get('provider', ''))
                if prov.get('kind') not in ('anthropic', 'openai'):
                    problems.append(f'provider {spec.get("provider")!r}: kind must be anthropic or openai')
                if not spec.get('model'):
                    problems.append(f'agents.{role}.model is empty')
                if int(spec.get('max_steps', 0)) < 1:
                    problems.append(f'agents.{role}.max_steps must be >= 1')
            except (ConfigError, ValueError, TypeError) as exc:
                problems.append(str(exc))
        for pattern in self.get('sandbox.deny_patterns') or []:
            try:
                re.compile(pattern)
            except re.error as exc:
                problems.append(f'bad sandbox.deny_patterns entry {pattern!r}: {exc}')
        return problems
