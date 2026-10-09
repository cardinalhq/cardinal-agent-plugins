"""Private atomic receipt files shared by authoring and execution transport."""
import json
import os
from pathlib import Path


def private_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(value)
    temporary.replace(path)


def private_json(path: Path, value: dict) -> None:
    private_text(path, json.dumps(value, ensure_ascii=False, indent=2))
