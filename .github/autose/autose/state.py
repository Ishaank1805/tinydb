"""Run state on disk: artifacts, their history, structured data, transcripts.

Layout (one directory, uploaded as a workflow artifact by every job):
    state.json  artifacts/  history/  data/  transcripts/  events/<job>.jsonl  usage/<job>.json
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any


class State:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        for sub in ('artifacts', 'history', 'data', 'transcripts', 'events', 'usage'):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'state.json'
        self.data: dict[str, Any] = json.loads(self.path.read_text()) if self.path.exists() else {}

    @classmethod
    def init_from(cls, root: str | Path, previous: str | Path | None) -> 'State':
        root = Path(root)
        if previous and Path(previous).is_dir() and any(Path(previous).iterdir()):
            shutil.copytree(previous, root, dirs_exist_ok=True)
        return cls(root)

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=2, default=str), encoding='utf-8')

    def artifact_path(self, name: str) -> Path:
        return self.root / 'artifacts' / name

    def write_artifact(self, name: str, text: str, tag: str = '') -> Path:
        path = self.artifact_path(name)
        if path.exists() and path.read_text(encoding='utf-8') != text:
            n = len(list((self.root / 'history').glob(f'{path.stem}.*'))) + 1
            suffix = f'-{tag}' if tag else ''
            shutil.copyfile(path, self.root / 'history' / f'{path.stem}.{n:02d}{suffix}{path.suffix}')
        path.write_text(text, encoding='utf-8')
        return path

    def read_artifact(self, name: str) -> str | None:
        path = self.artifact_path(name)
        return path.read_text(encoding='utf-8') if path.exists() else None

    def write_data(self, name: str, value: Any) -> None:
        (self.root / 'data' / f'{name}.json').write_text(json.dumps(value, indent=2, default=str),
                                                         encoding='utf-8')

    def read_data(self, name: str, default: Any = None) -> Any:
        path = self.root / 'data' / f'{name}.json'
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default

    def transcript(self, role: str, stage: str) -> Path:
        n = len(list((self.root / 'transcripts').glob('*.jsonl'))) + 1
        return self.root / 'transcripts' / f'{n:02d}-{stage}-{role}.jsonl'
