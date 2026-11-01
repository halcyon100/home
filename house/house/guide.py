"""The house guide: how the house works and why, for people and for AI.

One markdown file, kept on the Home Assistant box (in this app's /data, so it's in every backup) and
never in the public repo: it's full of house data. The Guide page in House reads it; any Claude chat
with the house tools can read it too, as plain markdown, through ingress.

It's written to through ingress only (Home Assistant signs the request in), and each save keeps the
previous version, so a bad edit can be undone.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

KEEP = 30                       # earlier versions kept
MAX_BYTES = 512 * 1024


class Guide:
    def __init__(self, root: str | Path = "/data/guide"):
        self.root = Path(root)
        self.file = self.root / "guide.md"
        self.old = self.root / "versions"

    def read(self) -> str | None:
        try:
            return self.file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def updated(self) -> float | None:
        try:
            return self.file.stat().st_mtime
        except FileNotFoundError:
            return None

    def versions(self) -> list[str]:
        if not self.old.is_dir():
            return []
        return sorted((p.name for p in self.old.glob("*.md")), reverse=True)

    def save(self, text: str) -> dict:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("the guide can't be empty")
        data = text.encode("utf-8")
        if len(data) > MAX_BYTES:
            raise ValueError(f"the guide is over {MAX_BYTES // 1024} KB")
        self.root.mkdir(parents=True, exist_ok=True)
        before = self.read()
        if before == text:
            return {"ok": True, "changed": False, "bytes": len(data)}
        if before is not None:
            self.old.mkdir(exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.updated() or time.time()))
            (self.old / f"{stamp}.md").write_text(before, encoding="utf-8")
            for stale in self.versions()[KEEP:]:
                (self.old / stale).unlink(missing_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, self.file)
        return {"ok": True, "changed": True, "bytes": len(data), "versions": len(self.versions())}
