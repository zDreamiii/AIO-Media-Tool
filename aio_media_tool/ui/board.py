from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from PySide6.QtCore import QEvent, QPoint, QRect, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QMouseEvent, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from aio_media_tool.config import SettingsStore
from aio_media_tool.jobs import JobManager
from aio_media_tool.models import AppSettings, JobKind
from aio_media_tool.services.downloads import DownloadOptions, DownloadService
from aio_media_tool.services.workspace import BoardCategory, BoardItem, WorkspaceStore
from aio_media_tool.ui.widgets import Card, PageHeader, muted, section_title

try:  # The native web engine is optional; the board has a link fallback.
    from PySide6.QtWebEngineWidgets import QWebEngineView
except ImportError:  # pragma: no cover - depends on platform system libraries
    QWebEngineView = None  # type: ignore[assignment,misc]


YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def youtube_video_id(value: str) -> str:
    value = value.strip()
    if YOUTUBE_ID.fullmatch(value):
        return value
    parsed = urlparse(value)
    host = (parsed.hostname or "").casefold()
    if host in {"youtu.be", "www.youtu.be"}:
        candidate = parsed.path.strip("/").split("/")[0]
    elif host == "youtube.com" or host.endswith(".youtube.com"):
        candidate = parse_qs(parsed.query).get("v", [""])[0]
        if not candidate:
            parts = [part for part in parsed.path.split("/") if part]
            candidate = parts[1] if len(parts) > 1 and parts[0] in {"embed", "shorts"} else ""
    else:
        return ""
    return candidate if YOUTUBE_ID.fullmatch(candidate) else ""


class BoardItemDialog(QDialog):
    def __init__(self, item: BoardItem | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Block bearbeiten" if item else "Block hinzufügen")
        self.resize(560, 430)
        root = QVBoxLayout(self)
        form = QFormLayout()
        self.kind = QComboBox()
        self.kind.addItem("Notiz / Schritte", "note")
        self.kind.addItem("Bild", "image")
        self.kind.addItem("YouTube-Video", "video")
        self.title = QLineEdit()
        self.content = QPlainTextEdit()
        self.content.setMaximumHeight(110)
        self.notes = QPlainTextEdit()
        self.notes.setPlaceholderText("Optionale kleine Notiz unter Bild oder Video")
        self.notes.setMaximumHeight(110)
        content_widget = QWidget()
        content_layout = QHBoxLayout(content_widget)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.addWidget(self.content, 1)
        self.browse = QPushButton("Bild wählen")
        self.browse.clicked.connect(self._choose_image)
        content_layout.addWidget(self.browse)
        self.embedded = QCheckBox("Video direkt einbetten, wenn Qt WebEngine verfügbar ist")
        self.embedded.setChecked(True)
        form.addRow("Typ:", self.kind)
        form.addRow("Titel:", self.title)
        form.addRow("Inhalt / URL:", content_widget)
        form.addRow("Notiz darunter:", self.notes)
        form.addRow("Video:", self.embedded)
        root.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._validate)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self.kind.currentIndexChanged.connect(self._kind_changed)
        if item:
            self.kind.setCurrentIndex(max(0, self.kind.findData(item.kind)))
            self.title.setText(item.title)
            self.content.setPlainText(item.content)
            self.notes.setPlainText(item.notes)
            self.embedded.setChecked(item.embedded)
        self._kind_changed()

    def _kind_changed(self) -> None:
        kind = self.kind.currentData()
        self.browse.setVisible(kind == "image")
        self.embedded.setVisible(kind == "video")
        placeholders = {
            "note": "Schritte, Codes, Hinweise oder andere Notizen …",
            "image": "Lokaler Bildpfad",
            "video": "YouTube-ID oder vollständige YouTube-URL",
        }
        self.content.setPlaceholderText(placeholders[kind])

    def _choose_image(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self, "Bild auswählen", str(Path.home()), "Bilder (*.jpg *.jpeg *.png *.webp *.gif)"
        )
        if selected:
            self.content.setPlainText(selected)

    def _validate(self) -> None:
        kind = self.kind.currentData()
        content = self.content.toPlainText().strip()
        if not self.title.text().strip():
            QMessageBox.warning(self, "Eingabe prüfen", "Bitte einen Blocktitel angeben.")
            return
        if not content:
            QMessageBox.warning(self, "Eingabe prüfen", "Bitte einen Inhalt angeben.")
            return
        if kind == "image" and not Path(content).expanduser().is_file():
            QMessageBox.warning(self, "Eingabe prüfen", "Das ausgewählte Bild existiert nicht.")
            return
        if kind == "video" and not youtube_video_id(content):
            QMessageBox.warning(
                self, "Eingabe prüfen", "Bitte eine gültige YouTube-ID oder YouTube-URL angeben."
            )
            return
        self.accept()

    def values(self) -> dict[str, object]:
        return {
            "kind": self.kind.currentData(),
            "title": self.title.text().strip(),
            "content": self.content.toPlainText().strip(),
            "notes": self.notes.toPlainText().strip(),
            "embedded": self.embedded.isChecked(),
        }


class BoardBlock(QFrame):
    edit_requested = Signal(str)
    delete_requested = Signal(str)
    geometry_changed = Signal(str, object)
    download_requested = Signal(str)

    EDGE = 8

    def __init__(self, item: BoardItem, canvas: BoardCanvas) -> None:
        super().__init__(canvas)
        self.item = item
        self.canvas = canvas
        self.setObjectName("BoardBlock")
        self.setMouseTracking(True)
        self.setMinimumSize(220, 150)
        self._mode = ""
        self._press_global = QPoint()
        self._start_geometry = QRect()
        self._image_source: QPixmap | None = None
        root = QVBoxLayout(self)
        root.setContentsMargins(9, 9, 9, 9)
        root.setSpacing(7)
        self.header = QFrame()
        header_layout = QHBoxLayout(self.header)
        header_layout.setContentsMargins(2, 0, 0, 0)
        header_layout.setSpacing(5)
        self.title_label = QLabel(item.title)
        self.title_label.setObjectName("CardTitle")
        kind_label = QLabel({"note": "NOTIZ", "image": "BILD", "video": "VIDEO"}[item.kind])
        kind_label.setObjectName("Badge")
        edit = QPushButton("Bearbeiten")
        edit.setFixedHeight(28)
        edit.clicked.connect(lambda: self.edit_requested.emit(item.id))
        remove = QPushButton("×")
        remove.setObjectName("Danger")
        remove.setFixedWidth(40)
        remove.clicked.connect(lambda: self.delete_requested.emit(item.id))
        header_layout.addWidget(self.title_label, 1)
        header_layout.addWidget(kind_label)
        header_layout.addWidget(edit)
        header_layout.addWidget(remove)
        root.addWidget(self.header)
        self.media_widget = self._build_content()
        root.addWidget(self.media_widget, 1)
        if item.notes:
            note = QTextBrowser()
            note.setObjectName("BoardNote")
            note.setPlainText(item.notes)
            note.setMaximumHeight(64)
            root.addWidget(note)
        self.header.installEventFilter(self)
        self.title_label.installEventFilter(self)
        self._set_cursor("")

    def _build_content(self) -> QWidget:
        if self.item.kind == "image":
            label = QLabel()
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setObjectName("BoardMedia")
            self._image_source = QPixmap(str(Path(self.item.content).expanduser()))
            return label
        if self.item.kind == "video":
            wrapper = QWidget()
            layout = QVBoxLayout(wrapper)
            layout.setContentsMargins(0, 0, 0, 0)
            video_id = youtube_video_id(self.item.content)
            if self.item.embedded and QWebEngineView is not None:
                view = QWebEngineView()
                view.setUrl(QUrl(f"https://www.youtube.com/embed/{video_id}?rel=0"))
                layout.addWidget(view, 1)
            else:
                fallback = QLabel(
                    f"YouTube · {video_id}\n"
                    + (
                        "Einbettung ist auf diesem System nicht verfügbar."
                        if self.item.embedded
                        else ""
                    )
                )
                fallback.setAlignment(Qt.AlignmentFlag.AlignCenter)
                fallback.setObjectName("BoardMedia")
                fallback.setWordWrap(True)
                layout.addWidget(fallback, 1)
            actions = QHBoxLayout()
            open_video = QPushButton("YouTube öffnen")
            open_video.clicked.connect(
                lambda: QDesktopServices.openUrl(
                    QUrl(f"https://www.youtube.com/watch?v={video_id}")
                )
            )
            download = QPushButton("Optional herunterladen")
            download.clicked.connect(lambda: self.download_requested.emit(self.item.id))
            actions.addWidget(open_video)
            actions.addWidget(download)
            actions.addStretch()
            layout.addLayout(actions)
            return wrapper
        browser = QTextBrowser()
        browser.setObjectName("BoardNote")
        browser.setPlainText(self.item.content)
        return browser

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if (
            self._image_source
            and not self._image_source.isNull()
            and isinstance(self.media_widget, QLabel)
        ):
            self.media_widget.setPixmap(
                self._image_source.scaled(
                    self.media_widget.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )

    def _hit_mode(self, point: QPoint) -> str:
        left = point.x() <= self.EDGE
        right = point.x() >= self.width() - self.EDGE
        top = point.y() <= self.EDGE
        bottom = point.y() >= self.height() - self.EDGE
        if top and left:
            return "top-left"
        if top and right:
            return "top-right"
        if bottom and left:
            return "bottom-left"
        if bottom and right:
            return "bottom-right"
        if left:
            return "left"
        if right:
            return "right"
        if top:
            return "top"
        if bottom:
            return "bottom"
        return ""

    def _set_cursor(self, mode: str) -> None:
        if mode in {"top-left", "bottom-right"}:
            cursor = Qt.CursorShape.SizeFDiagCursor
        elif mode in {"top-right", "bottom-left"}:
            cursor = Qt.CursorShape.SizeBDiagCursor
        elif mode in {"left", "right"}:
            cursor = Qt.CursorShape.SizeHorCursor
        elif mode in {"top", "bottom"}:
            cursor = Qt.CursorShape.SizeVerCursor
        elif mode == "move":
            cursor = Qt.CursorShape.SizeAllCursor
        else:
            cursor = Qt.CursorShape.ArrowCursor
        self.setCursor(cursor)

    def _start_drag(self, mode: str, global_point: QPoint) -> None:
        self._mode = mode
        self._press_global = global_point
        self._start_geometry = self.geometry()
        self.raise_()
        self.grabMouse()
        self._set_cursor(mode)

    def _drag_to(self, global_point: QPoint) -> None:
        if not self._mode:
            return
        delta = global_point - self._press_global
        rectangle = QRect(self._start_geometry)
        if self._mode == "move":
            rectangle.translate(delta)
            rectangle = self.canvas.snap_rect(rectangle, self)
        else:
            if "left" in self._mode:
                rectangle.setLeft(
                    min(rectangle.right() - self.minimumWidth(), rectangle.left() + delta.x())
                )
            if "right" in self._mode:
                rectangle.setRight(
                    max(rectangle.left() + self.minimumWidth(), rectangle.right() + delta.x())
                )
            if "top" in self._mode:
                rectangle.setTop(
                    min(rectangle.bottom() - self.minimumHeight(), rectangle.top() + delta.y())
                )
            if "bottom" in self._mode:
                rectangle.setBottom(
                    max(rectangle.top() + self.minimumHeight(), rectangle.bottom() + delta.y())
                )
            rectangle = self.canvas.clamp_rect(rectangle)
        self.setGeometry(rectangle)

    def _finish_drag(self) -> None:
        if self._mode:
            self.geometry_changed.emit(self.item.id, self.geometry())
            self.releaseMouse()
        self._mode = ""
        self._set_cursor("")

    def eventFilter(self, watched, event) -> bool:
        if watched in (self.header, self.title_label):
            if (
                event.type() == QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.LeftButton
            ):
                self._start_drag("move", event.globalPosition().toPoint())
                return True
            if event.type() == QEvent.Type.MouseMove and self._mode:
                self._drag_to(event.globalPosition().toPoint())
                return True
            if event.type() == QEvent.Type.MouseButtonRelease and self._mode:
                self._finish_drag()
                return True
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        mode = self._hit_mode(event.position().toPoint())
        if event.button() == Qt.MouseButton.LeftButton and mode:
            self._start_drag(mode, event.globalPosition().toPoint())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._mode:
            self._drag_to(event.globalPosition().toPoint())
            event.accept()
            return
        self._set_cursor(self._hit_mode(event.position().toPoint()))
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._mode:
            self._finish_drag()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class BoardCanvas(QWidget):
    image_dropped = Signal(str, object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("BoardCanvas")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAcceptDrops(True)
        self.setMinimumSize(2400, 1600)

    def clamp_rect(self, rectangle: QRect) -> QRect:
        rectangle.setWidth(min(rectangle.width(), self.width()))
        rectangle.setHeight(min(rectangle.height(), self.height()))
        rectangle.moveLeft(max(0, min(rectangle.left(), self.width() - rectangle.width())))
        rectangle.moveTop(max(0, min(rectangle.top(), self.height() - rectangle.height())))
        return rectangle

    def snap_rect(self, rectangle: QRect, moving: BoardBlock) -> QRect:
        rectangle = self.clamp_rect(rectangle)
        gap = 8
        threshold = 14
        others = [child for child in self.findChildren(BoardBlock) if child is not moving]
        for other in others:
            target = other.geometry()
            x_candidates = (
                target.left(),
                target.right() + gap,
                target.left() - rectangle.width() - gap,
                target.right() - rectangle.width(),
            )
            y_candidates = (
                target.top(),
                target.bottom() + gap,
                target.top() - rectangle.height() - gap,
                target.bottom() - rectangle.height(),
            )
            for candidate in x_candidates:
                if abs(rectangle.left() - candidate) <= threshold:
                    rectangle.moveLeft(candidate)
                    break
            for candidate in y_candidates:
                if abs(rectangle.top() - candidate) <= threshold:
                    rectangle.moveTop(candidate)
                    break
            if rectangle.intersects(target):
                placements = [
                    (
                        abs(rectangle.right() - target.left()),
                        QPoint(target.left() - rectangle.width() - gap, rectangle.top()),
                    ),
                    (
                        abs(rectangle.left() - target.right()),
                        QPoint(target.right() + gap, rectangle.top()),
                    ),
                    (
                        abs(rectangle.bottom() - target.top()),
                        QPoint(rectangle.left(), target.top() - rectangle.height() - gap),
                    ),
                    (
                        abs(rectangle.top() - target.bottom()),
                        QPoint(rectangle.left(), target.bottom() + gap),
                    ),
                ]
                rectangle.moveTopLeft(min(placements, key=lambda value: value[0])[1])
        return self.clamp_rect(rectangle)

    def dragEnterEvent(self, event) -> None:
        if any(
            Path(url.toLocalFile()).suffix.casefold() in {".jpg", ".jpeg", ".png", ".webp", ".gif"}
            for url in event.mimeData().urls()
            if url.isLocalFile()
        ):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        position = event.position().toPoint()
        for url in event.mimeData().urls():
            path = Path(url.toLocalFile()) if url.isLocalFile() else None
            if (
                path
                and path.is_file()
                and path.suffix.casefold() in {".jpg", ".jpeg", ".png", ".webp", ".gif"}
            ):
                self.image_dropped.emit(str(path.resolve()), position)
                position += QPoint(24, 24)
        event.acceptProposedAction()


class BoardPage(QWidget):
    def __init__(
        self,
        settings: AppSettings,
        settings_store: SettingsStore,
        workspace_store: WorkspaceStore,
        jobs: JobManager,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.settings_store = settings_store
        self.workspace_store = workspace_store
        self.jobs = jobs
        self.data = workspace_store.load()
        self.current_category_id = self.data.categories[0].id
        root = QVBoxLayout(self)
        root.setContentsMargins(30, 26, 30, 28)
        root.setSpacing(14)
        root.addWidget(
            PageHeader(
                "Sammlungen",
                "Kategorien und Unterkategorien mit frei verschiebbaren Notiz-, Bild- und YouTube-Blöcken.",
            )
        )
        splitter = QSplitter(Qt.Orientation.Horizontal)
        category_card = Card()
        category_card.setMinimumWidth(230)
        category_card.setMaximumWidth(380)
        category_card.layout.addWidget(section_title("Kategorien"))
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemSelectionChanged.connect(self._category_selected)
        category_card.layout.addWidget(self.tree, 1)
        category_actions = QHBoxLayout()
        add_root = QPushButton("+ Kategorie")
        add_child = QPushButton("+ Unterkat.")
        add_root.clicked.connect(lambda: self.add_category(False))
        add_child.clicked.connect(lambda: self.add_category(True))
        category_actions.addWidget(add_root)
        category_actions.addWidget(add_child)
        category_card.layout.addLayout(category_actions)
        delete_category = QPushButton("Kategorie löschen")
        delete_category.setObjectName("Danger")
        delete_category.clicked.connect(self.delete_category)
        category_card.layout.addWidget(delete_category)
        category_card.layout.addWidget(
            muted("Bilder können direkt auf die Arbeitsfläche gezogen werden.")
        )
        splitter.addWidget(category_card)

        workspace = QWidget()
        workspace_layout = QVBoxLayout(workspace)
        workspace_layout.setContentsMargins(0, 0, 0, 0)
        toolbar = QHBoxLayout()
        self.category_title = QLabel()
        self.category_title.setObjectName("SectionTitle")
        add_block = QPushButton("+ Block")
        add_block.setObjectName("Primary")
        add_block.clicked.connect(self.add_item)
        self.zoom = QComboBox()
        for value in (75, 90, 100, 125, 150):
            self.zoom.addItem(f"{value} %", value)
        self.zoom.setCurrentIndex(max(0, self.zoom.findData(settings.board_zoom)))
        self.zoom.currentIndexChanged.connect(self._zoom_changed)
        toolbar.addWidget(self.category_title)
        toolbar.addStretch()
        toolbar.addWidget(QLabel("Zoom:"))
        toolbar.addWidget(self.zoom)
        toolbar.addWidget(add_block)
        workspace_layout.addLayout(toolbar)
        scroll = QScrollArea()
        scroll.setWidgetResizable(False)
        self.canvas = BoardCanvas()
        self.canvas.image_dropped.connect(self._image_dropped)
        scroll.setWidget(self.canvas)
        workspace_layout.addWidget(scroll, 1)
        splitter.addWidget(workspace)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, 1)
        self._reload_tree()
        self._apply_zoom_size()

    def _scale(self) -> float:
        return int(self.zoom.currentData() or 100) / 100

    def _apply_zoom_size(self) -> None:
        scale = self._scale()
        self.canvas.setMinimumSize(round(2400 * scale), round(1600 * scale))
        self.reload_items()

    def _reload_tree(self) -> None:
        self.tree.clear()
        by_parent: dict[str, list[BoardCategory]] = {}
        for category in self.data.categories:
            by_parent.setdefault(category.parent_id, []).append(category)

        def add_children(parent_item: QTreeWidgetItem | None, parent_id: str) -> None:
            for category in sorted(
                by_parent.get(parent_id, []), key=lambda value: value.name.casefold()
            ):
                item = QTreeWidgetItem([category.name])
                item.setData(0, Qt.ItemDataRole.UserRole, category.id)
                if parent_item:
                    parent_item.addChild(item)
                else:
                    self.tree.addTopLevelItem(item)
                add_children(item, category.id)

        add_children(None, "")
        self.tree.expandAll()
        matches = self.tree.findItems(
            "*", Qt.MatchFlag.MatchWildcard | Qt.MatchFlag.MatchRecursive, 0
        )
        selected = next(
            (
                item
                for item in matches
                if item.data(0, Qt.ItemDataRole.UserRole) == self.current_category_id
            ),
            matches[0] if matches else None,
        )
        if selected:
            self.tree.setCurrentItem(selected)

    def _category_selected(self) -> None:
        item = self.tree.currentItem()
        if not item:
            return
        self.current_category_id = str(item.data(0, Qt.ItemDataRole.UserRole))
        self.category_title.setText(item.text(0))
        self.reload_items()

    def add_category(self, child: bool) -> None:
        name, accepted = QInputDialog.getText(
            self, "Unterkategorie" if child else "Kategorie", "Name:"
        )
        if not accepted or not name.strip():
            return
        parent_id = self.current_category_id if child else ""
        category = BoardCategory(name=name.strip(), parent_id=parent_id)
        self.data.categories.append(category)
        self.current_category_id = category.id
        self._save()
        self._reload_tree()

    def delete_category(self) -> None:
        if len(self.data.categories) <= 1:
            QMessageBox.warning(self, "Nicht möglich", "Mindestens eine Kategorie muss bleiben.")
            return
        answer = QMessageBox.question(
            self,
            "Kategorie löschen",
            "Kategorie, Unterkategorien und alle enthaltenen Blöcke wirklich löschen?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        doomed = {self.current_category_id}
        changed = True
        while changed:
            before = len(doomed)
            doomed.update(
                category.id for category in self.data.categories if category.parent_id in doomed
            )
            changed = len(doomed) != before
        self.data.categories = [value for value in self.data.categories if value.id not in doomed]
        self.data.items = [value for value in self.data.items if value.category_id not in doomed]
        self.current_category_id = self.data.categories[0].id
        self._save()
        self._reload_tree()

    def _next_position(self) -> QPoint:
        count = sum(item.category_id == self.current_category_id for item in self.data.items)
        return QPoint(24 + (count % 4) * 38, 24 + (count % 6) * 38)

    def add_item(self) -> None:
        dialog = BoardItemDialog(parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        position = self._next_position()
        item = BoardItem(
            category_id=self.current_category_id,
            x=round(position.x() / self._scale()),
            y=round(position.y() / self._scale()),
            **dialog.values(),
        )
        self.data.items.append(item)
        self._save()
        self.reload_items()

    def edit_item(self, item_id: str) -> None:
        item = next((value for value in self.data.items if value.id == item_id), None)
        if not item:
            return
        dialog = BoardItemDialog(item, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        for key, value in dialog.values().items():
            setattr(item, key, value)
        self._save()
        self.reload_items()

    def delete_item(self, item_id: str) -> None:
        answer = QMessageBox.question(
            self, "Block löschen", "Diesen Block wirklich aus der Sammlung löschen?"
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.data.items = [value for value in self.data.items if value.id != item_id]
        self._save()
        self.reload_items()

    def _geometry_changed(self, item_id: str, rectangle: QRect) -> None:
        item = next((value for value in self.data.items if value.id == item_id), None)
        if not item:
            return
        scale = self._scale()
        item.x = round(rectangle.x() / scale)
        item.y = round(rectangle.y() / scale)
        item.width = round(rectangle.width() / scale)
        item.height = round(rectangle.height() / scale)
        self._save()

    def reload_items(self) -> None:
        for block in self.canvas.findChildren(BoardBlock):
            block.hide()
            block.deleteLater()
        scale = self._scale()
        for item in self.data.items:
            if item.category_id != self.current_category_id:
                continue
            block = BoardBlock(item, self.canvas)
            block.setGeometry(
                round(item.x * scale),
                round(item.y * scale),
                round(item.width * scale),
                round(item.height * scale),
            )
            block.edit_requested.connect(self.edit_item)
            block.delete_requested.connect(self.delete_item)
            block.geometry_changed.connect(self._geometry_changed)
            block.download_requested.connect(self.download_item)
            block.show()

    def _image_dropped(self, path: str, position: QPoint) -> None:
        scale = self._scale()
        item = BoardItem(
            category_id=self.current_category_id,
            kind="image",
            title=Path(path).stem,
            content=path,
            x=round(position.x() / scale),
            y=round(position.y() / scale),
        )
        self.data.items.append(item)
        self._save()
        self.reload_items()

    def download_item(self, item_id: str) -> None:
        item = next((value for value in self.data.items if value.id == item_id), None)
        if not item or item.kind != "video":
            return
        answer = QMessageBox.question(
            self,
            "Video herunterladen",
            "Nur fortfahren, wenn du das Video rechtmäßig speichern darfst. Download starten?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        url = f"https://www.youtube.com/watch?v={youtube_video_id(item.content)}"
        output = Path(self.settings.download_dir).expanduser()

        def runner(progress, cancel):
            return DownloadService().download(
                url, output, DownloadOptions(mode="video", playlist=False), progress, cancel
            )

        self.jobs.submit(
            JobKind.BOARD,
            f"Board-Video: {item.title}",
            url,
            str(output),
            {"item_id": item.id},
            runner,
        )

    def _zoom_changed(self) -> None:
        self.settings.board_zoom = int(self.zoom.currentData() or 100)
        self.settings_store.save(self.settings)
        self._apply_zoom_size()

    def _save(self) -> None:
        self.workspace_store.save(self.data)
