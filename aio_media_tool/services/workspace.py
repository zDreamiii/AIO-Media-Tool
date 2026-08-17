from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from uuid import uuid4


@dataclass(slots=True)
class BoardCategory:
    name: str
    id: str = field(default_factory=lambda: uuid4().hex)
    parent_id: str = ""


@dataclass(slots=True)
class BoardItem:
    category_id: str
    kind: str
    title: str
    content: str
    notes: str = ""
    x: int = 24
    y: int = 24
    width: int = 360
    height: int = 260
    embedded: bool = True
    id: str = field(default_factory=lambda: uuid4().hex)


@dataclass(slots=True)
class WorkspaceData:
    categories: list[BoardCategory] = field(default_factory=list)
    items: list[BoardItem] = field(default_factory=list)


class WorkspaceStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> WorkspaceData:
        if not self.path.exists():
            return WorkspaceData(categories=[BoardCategory(name="Start")])
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            categories = [BoardCategory(**value) for value in raw.get("categories", [])]
            items = [
                BoardItem(**value)
                for value in raw.get("items", [])
                if isinstance(value, dict) and value.get("kind") in {"note", "image", "video"}
            ]
            data = WorkspaceData(categories=categories, items=items)
            if not data.categories:
                data.categories.append(BoardCategory(name="Start"))
            category_ids = {category.id for category in data.categories}
            data.items = [item for item in data.items if item.category_id in category_ids]
            for item in data.items:
                item.x = max(0, int(item.x))
                item.y = max(0, int(item.y))
                item.width = max(220, min(2400, int(item.width)))
                item.height = max(150, min(1600, int(item.height)))
            return data
        except (OSError, ValueError, TypeError):
            return WorkspaceData(categories=[BoardCategory(name="Start")])

    def save(self, data: WorkspaceData) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(
            prefix="workspace-", suffix=".json", dir=self.path.parent
        )
        try:
            payload = {
                "version": 1,
                "categories": [asdict(value) for value in data.categories],
                "items": [asdict(value) for value in data.items],
            }
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, self.path)
        finally:
            Path(temp_name).unlink(missing_ok=True)
