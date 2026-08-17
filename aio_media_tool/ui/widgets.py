from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


class Card(QFrame):
    def __init__(self, parent: QWidget | None = None, padding: int = 18) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(padding, padding, padding, padding)
        self.layout.setSpacing(12)


class PageHeader(QWidget):
    def __init__(self, title: str, description: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 6)
        layout.setSpacing(4)
        heading = QLabel(title)
        heading.setObjectName("PageTitle")
        subtitle = QLabel(description)
        subtitle.setObjectName("PageDescription")
        subtitle.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(subtitle)


class PathPicker(QWidget):
    changed = Signal(str)

    def __init__(
        self,
        value: str = "",
        mode: str = "directory",
        file_filter: str = "Alle Dateien (*)",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.mode = mode
        self.file_filter = file_filter
        self.setAcceptDrops(True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.edit = QLineEdit(value)
        self.edit.setAcceptDrops(False)
        self.button = QPushButton("Auswählen")
        self.button.clicked.connect(self.choose)
        self.edit.textChanged.connect(self.changed.emit)
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.button)

    def text(self) -> str:
        return self.edit.text().strip()

    def setText(self, value: str) -> None:
        self.edit.setText(value)

    def choose(self) -> None:
        current = self.text() or str(Path.home())
        if self.mode == "directory":
            selected = QFileDialog.getExistingDirectory(self, "Ordner auswählen", current)
        elif self.mode == "save":
            selected, _ = QFileDialog.getSaveFileName(
                self, "Datei speichern", current, self.file_filter
            )
        else:
            selected, _ = QFileDialog.getOpenFileName(
                self, "Datei auswählen", current, self.file_filter
            )
        if selected:
            self.edit.setText(selected)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            candidates = [
                Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()
            ]
            valid = any(
                path.is_dir() if self.mode == "directory" else path.is_file() for path in candidates
            )
            if valid:
                event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        candidates = [
            Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()
        ]
        for path in candidates:
            if (self.mode == "directory" and path.is_dir()) or (
                self.mode != "directory" and path.is_file()
            ):
                self.setText(str(path.resolve()))
                event.acceptProposedAction()
                return


class DropListWidget(QListWidget):
    external_files_dropped = Signal(object)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        if event.mimeData().hasUrls():
            paths = [
                Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()
            ]
            self.external_files_dropped.emit(paths)
            event.acceptProposedAction()
            return
        super().dropEvent(event)


class FileDropList(QFrame):
    files_changed = Signal()

    def __init__(
        self,
        file_filter: str,
        extensions: set[str],
        multiple: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("DropZone")
        self.setAcceptDrops(True)
        self.file_filter = file_filter
        self.extensions = {value.casefold() for value in extensions}
        self.multiple = multiple
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)
        hint = QLabel("Dateien hier ablegen oder auswählen")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setObjectName("Muted")
        self.list = DropListWidget()
        self.list.setMinimumHeight(110)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list.external_files_dropped.connect(self.add_paths)
        buttons = QHBoxLayout()
        add = QPushButton("Dateien hinzufügen")
        remove = QPushButton("Auswahl entfernen")
        clear = QPushButton("Leeren")
        add.clicked.connect(self.choose)
        remove.clicked.connect(self.remove_selected)
        clear.clicked.connect(self.clear)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addStretch()
        buttons.addWidget(clear)
        layout.addWidget(hint)
        layout.addWidget(self.list)
        layout.addLayout(buttons)

    def paths(self) -> list[Path]:
        return [
            Path(self.list.item(index).data(Qt.ItemDataRole.UserRole))
            for index in range(self.list.count())
        ]

    def add_paths(self, paths: list[Path]) -> None:
        existing = {str(path.resolve()) for path in self.paths()}
        for path in paths:
            if not path.is_file() or (
                self.extensions and path.suffix.casefold() not in self.extensions
            ):
                continue
            resolved = str(path.resolve())
            if resolved in existing:
                continue
            if not self.multiple:
                self.list.clear()
                existing.clear()
            self.list.addItem(path.name)
            item = self.list.item(self.list.count() - 1)
            item.setData(Qt.ItemDataRole.UserRole, resolved)
            item.setToolTip(resolved)
            existing.add(resolved)
        self.files_changed.emit()

    def choose(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Dateien auswählen", str(Path.home()), self.file_filter
        )
        self.add_paths([Path(value) for value in paths])

    def remove_selected(self) -> None:
        for item in self.list.selectedItems():
            self.list.takeItem(self.list.row(item))
        self.files_changed.emit()

    def clear(self) -> None:
        self.list.clear()
        self.files_changed.emit()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        self.add_paths(
            [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        )
        event.acceptProposedAction()


class PlaylistPreview(QFrame):
    """Reusable playlist table whose removed rows become the download selection."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("DropZone")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        self.summary = QLabel("Noch keine Vorschau geladen")
        self.summary.setObjectName("Muted")
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["#", "Titel", "Kanal / Interpret", "Dauer"])
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setMinimumHeight(190)
        actions = QHBoxLayout()
        select_all = QPushButton("Alle auswählen")
        select_all.clicked.connect(self.table.selectAll)
        remove = QPushButton("Auswahl entfernen")
        remove.clicked.connect(self.remove_selected)
        clear = QPushButton("Vorschau leeren")
        clear.clicked.connect(self.clear)
        actions.addWidget(select_all)
        actions.addWidget(remove)
        actions.addStretch()
        actions.addWidget(clear)
        layout.addWidget(self.summary)
        layout.addWidget(self.table)
        layout.addLayout(actions)
        self.setVisible(False)

    @staticmethod
    def _duration(value: object) -> str:
        if value in (None, ""):
            return "–"
        try:
            seconds = max(0, int(float(value)))
        except (TypeError, ValueError):
            return "–"
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"

    def set_collection(self, collection: dict) -> None:
        entries = collection.get("entries", [])
        self.table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            index = QTableWidgetItem(str(entry.get("index") or row + 1))
            index.setData(Qt.ItemDataRole.UserRole, int(entry.get("index") or row + 1))
            self.table.setItem(row, 0, index)
            self.table.setItem(row, 1, QTableWidgetItem(str(entry.get("title") or "Unbekannt")))
            self.table.setItem(row, 2, QTableWidgetItem(str(entry.get("uploader") or "Unbekannt")))
            self.table.setItem(row, 3, QTableWidgetItem(self._duration(entry.get("duration"))))
        kind = "Playlist" if collection.get("is_playlist") else "Einzelvideo"
        self.summary.setText(
            f"{kind}: {collection.get('title') or 'Unbekannt'} · {len(entries)} Eintrag/Einträge"
        )
        self.setVisible(True)

    def selected_indices(self) -> list[int]:
        values: list[int] = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item:
                values.append(int(item.data(Qt.ItemDataRole.UserRole)))
        return values

    def remove_selected(self) -> None:
        rows = sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True)
        for row in rows:
            self.table.removeRow(row)
        self.summary.setText(f"{self.table.rowCount()} Eintrag/Einträge ausgewählt")

    def clear(self) -> None:
        self.table.setRowCount(0)
        self.summary.setText("Noch keine Vorschau geladen")
        self.setVisible(False)


def section_title(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("SectionTitle")
    return label


def muted(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("Muted")
    label.setWordWrap(True)
    return label
