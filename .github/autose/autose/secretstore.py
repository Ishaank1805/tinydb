"""Credentials never live in the environment of agent processes.

The workflow writes each secret into its own file in a private temp
directory in a short step that exits immediately. ``Secrets.load()`` reads
those files once, deletes the directory and scrubs credential-looking
variables from ``os.environ`` before any agent or tool subprocess starts.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

KNOWN = ('ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'GEMINI_API_KEY', 'GEMINI_API_KEY_2', 'OPENROUTER_API_KEY',
         'AUTOSE_GITHUB_TOKEN', 'GITHUB_TOKEN', 'GH_TOKEN')
_SENSITIVE = ('TOKEN', 'SECRET', 'API_KEY', 'APIKEY', 'PASSWORD', 'CREDENTIAL')


class Secrets:
    def __init__(self, values: dict[str, str] | None = None):
        self._values = {k: v for k, v in (values or {}).items() if v}

    @classmethod
    def load(cls, directory: str | None = None) -> 'Secrets':
        values: dict[str, str] = {}
        folder = directory or os.environ.get('AUTOSE_SECRETS_DIR')
        if folder and Path(folder).is_dir():
            for item in sorted(Path(folder).iterdir()):
                if item.is_file():
                    value = item.read_text(encoding='utf-8').strip()
                    if value:
                        values[item.name] = value
            shutil.rmtree(folder, ignore_errors=True)
        for name in KNOWN:  # local runs: fall back to the environment
            value = os.environ.get(name)
            if value and name not in values:
                values[name] = value
        if 'AUTOSE_GITHUB_TOKEN' not in values:
            for alt in ('GITHUB_TOKEN', 'GH_TOKEN'):
                if values.get(alt):
                    values['AUTOSE_GITHUB_TOKEN'] = values[alt]
                    break
        scrub_environment()
        return cls(values)

    def get(self, name: str) -> str | None:
        return self._values.get(name)

    @property
    def github_token(self) -> str | None:
        return self._values.get('AUTOSE_GITHUB_TOKEN')

    def found_in(self, text: str) -> list[str]:
        return [name for name, value in self._values.items() if len(value) >= 8 and value in text]

    def redact(self, text: str) -> str:
        for value in self._values.values():
            if len(value) >= 8 and value in text:
                text = text.replace(value, '***')
        return text


def scrub_environment() -> None:
    for key in list(os.environ):
        if any(marker in key.upper() for marker in _SENSITIVE):
            os.environ.pop(key, None)
