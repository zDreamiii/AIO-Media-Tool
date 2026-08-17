from __future__ import annotations

from pathlib import Path

import pytest

from aio_media_tool.services.files import BulkRenameService, RenameOptions
from aio_media_tool.services.workspace import (
    BoardCategory,
    BoardItem,
    WorkspaceData,
    WorkspaceStore,
)


def test_bulk_rename_preview_and_two_phase_apply(tmp_path: Path) -> None:
    sources = [tmp_path / "IMG_001_alpha.txt", tmp_path / "IMG_002_beta.txt"]
    for index, source in enumerate(sources):
        source.write_text(f"payload-{index}", encoding="utf-8")
    options = RenameOptions(
        template="guide_{name}_{n}",
        regex_pattern=r"^IMG_\d+_",
        regex_replacement="",
        start=7,
        padding=2,
        extensions="txt",
    )
    preview = BulkRenameService().preview(tmp_path, options)
    assert [row.destination.name for row in preview] == ["guide_alpha_07.txt", "guide_beta_08.txt"]
    outputs = BulkRenameService().apply(tmp_path, options)
    assert [path.name for path in outputs] == ["guide_alpha_07.txt", "guide_beta_08.txt"]
    assert [path.read_text(encoding="utf-8") for path in outputs] == ["payload-0", "payload-1"]
    assert not any(source.exists() for source in sources)


def test_bulk_rename_refuses_collisions(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    options = RenameOptions(template="same")
    preview = BulkRenameService().preview(tmp_path, options)
    assert all(row.error for row in preview)
    with pytest.raises(ValueError, match="abgebrochen"):
        BulkRenameService().apply(tmp_path, options)


def test_bulk_rename_does_not_overwrite_unchanged_source(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "same.txt").write_text("keep", encoding="utf-8")
    options = RenameOptions(template="{name}", regex_pattern=r"^a$", regex_replacement="same")
    # The unchanged same.txt must not be overwritten by a.txt.
    preview = BulkRenameService().preview(tmp_path, options)
    assert all(row.error for row in preview)
    assert (tmp_path / "same.txt").read_text(encoding="utf-8") == "keep"


def test_workspace_roundtrip(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path / "workspace.json")
    root = BoardCategory(name="BO7")
    child = BoardCategory(name="Map 1", parent_id=root.id)
    item = BoardItem(
        category_id=child.id,
        kind="video",
        title="Easter Egg Schritt",
        content="dQw4w9WgXcQ",
        notes="Hebel zuerst aktivieren",
        x=50,
        y=75,
        width=480,
        height=320,
    )
    store.save(WorkspaceData(categories=[root, child], items=[item]))
    loaded = store.load()
    assert [category.name for category in loaded.categories] == ["BO7", "Map 1"]
    assert loaded.items[0] == item
