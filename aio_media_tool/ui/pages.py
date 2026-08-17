from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from aio_media_tool.config import SettingsStore
from aio_media_tool.database import HistoryDatabase
from aio_media_tool.jobs import JobManager
from aio_media_tool.models import AppSettings, JobKind, JobRecord, JobStatus
from aio_media_tool.paths import AppPaths
from aio_media_tool.runtime import is_frozen
from aio_media_tool.services.audio import AudioMetadata, AudioService
from aio_media_tool.services.diagnostics import collect_tool_status, create_diagnostic_bundle
from aio_media_tool.services.downloads import DownloadOptions, DownloadService
from aio_media_tool.services.images import ImageOptions, ImageService
from aio_media_tool.services.pdfs import PdfService
from aio_media_tool.services.updater import UpdaterService
from aio_media_tool.services.video import (
    VIDEO_COMPRESSION_PROFILES,
    CutOptions,
    GifOptions,
    VideoOptions,
    VideoService,
    build_cut_segments,
    normalize_explicit_segments,
    numbered_segment_name,
    parse_timecode,
)
from aio_media_tool.ui.video_cutter import VideoPreview
from aio_media_tool.ui.widgets import (
    Card,
    FileDropList,
    PageHeader,
    PathPicker,
    PlaylistPreview,
    muted,
    section_title,
)


class InspectSignals(QObject):
    completed = Signal(object)
    failed = Signal(str)


class InspectWorker(QRunnable):
    def __init__(self, source: str) -> None:
        super().__init__()
        self.source = source
        self.signals = InspectSignals()

    @Slot()
    def run(self) -> None:
        try:
            self.signals.completed.emit(DownloadService().inspect_collection(self.source))
        except Exception as exc:
            self.signals.failed.emit(str(exc).strip() or type(exc).__name__)


def add_form_row(form: QFormLayout, label: str, widget: QWidget) -> None:
    form.addRow(f"{label}:", widget)


def primary_button(text: str) -> QPushButton:
    button = QPushButton(text)
    button.setObjectName("Primary")
    return button


def show_validation(parent: QWidget, message: str) -> None:
    QMessageBox.warning(parent, "Eingabe prüfen", message)


class BasePage(QScrollArea):
    def __init__(self, title: str, description: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.content = QWidget()
        self.content.setObjectName("Root")
        self.body = QVBoxLayout(self.content)
        self.body.setContentsMargins(30, 26, 30, 50)
        self.body.setSpacing(18)
        self.body.addWidget(PageHeader(title, description))
        self.setWidget(self.content)

    def finish(self) -> None:
        self.body.addStretch(1)


class DashboardPage(BasePage):
    navigate = Signal(int)

    def __init__(self, database: HistoryDatabase, parent: QWidget | None = None) -> None:
        super().__init__(
            "Übersicht",
            "Alle Medienwerkzeuge an einem Ort – lokal, nachvollziehbar und ohne Webserver.",
            parent,
        )
        self.database = database
        self.kpi_completed = QLabel("0")
        self.kpi_failed = QLabel("0")
        self.kpi_outputs = QLabel("0")
        kpis = QGridLayout()
        kpis.setHorizontalSpacing(14)
        for column, (title, value, caption) in enumerate(
            (
                ("Erledigt", self.kpi_completed, "erfolgreiche Jobs"),
                ("Ausgaben", self.kpi_outputs, "erzeugte Dateien"),
                ("Fehler", self.kpi_failed, "mit Details im Verlauf"),
            )
        ):
            card = Card()
            card.layout.addWidget(muted(title.upper()))
            value.setObjectName("Kpi")
            card.layout.addWidget(value)
            card.layout.addWidget(muted(caption))
            kpis.addWidget(card, 0, column)
        self.body.addLayout(kpis)
        self.body.addWidget(section_title("Schnellstart"))
        tools = QGridLayout()
        tools.setSpacing(14)
        definitions = (
            (
                1,
                "Download",
                "Video oder Audio aus einem berechtigten Link speichern",
                "Link öffnen",
            ),
            (2, "Musik", "MP3 mit Cover, Tags, Lyrics und Lautheitsnormalisierung", "Musik öffnen"),
            (
                3,
                "Bilder",
                "Mehrere Bilder skalieren, konvertieren und verkleinern",
                "Bilder öffnen",
            ),
            (4, "Videos", "FFmpeg-Presets, Zielgröße, Codec und Auflösung", "Videos öffnen"),
            (5, "PDFs", "Zusammenführen, trennen, drehen, schützen und mehr", "PDFs öffnen"),
            (
                6,
                "Dateien",
                "Ordner mit Muster, Datum, Regex und Nummerierung umbenennen",
                "Renamer öffnen",
            ),
            (
                7,
                "Sammlungen",
                "Gaming-Guides, Bilder, YouTube-Videos und Notizen anordnen",
                "Board öffnen",
            ),
            (
                11,
                "Transkription",
                "Whisper-Untertitel lokal als SRT/VTT und optionale Hardsubs",
                "Whisper öffnen",
            ),
            (
                12,
                "Deep Clean",
                "Sensible Metadaten rekursiv entfernen und protokollieren",
                "Stripper öffnen",
            ),
            (
                13,
                "Vault",
                "Dateien lokal mit AES-256-GCM verschlüsseln",
                "Vault öffnen",
            ),
            (
                14,
                "Smart Clipboard",
                "Lokaler Snippet-Verlauf, Textmakros und Auto-Löschen",
                "Clipboard öffnen",
            ),
            (
                15,
                "OCR & Übersetzen",
                "Text aus Bildern/PDFs erkennen und bearbeitbar exportieren",
                "OCR öffnen",
            ),
            (
                16,
                "KI-Upscaler",
                "Real-ESRGAN, RIFE, Hardwarecheck und Vorher/Nachher",
                "Upscaler öffnen",
            ),
        )
        for index, (target, title, description, action) in enumerate(definitions):
            card = Card()
            name = QLabel(title)
            name.setObjectName("CardTitle")
            text = muted(description)
            button = QPushButton(action)
            button.clicked.connect(lambda _checked=False, page=target: self.navigate.emit(page))
            card.layout.addWidget(name)
            card.layout.addWidget(text)
            card.layout.addStretch()
            card.layout.addWidget(button)
            tools.addWidget(card, index // 3, index % 3)
        self.body.addLayout(tools)
        self.finish()
        self.refresh()

    def refresh(self) -> None:
        jobs = self.database.recent(1000)
        self.kpi_completed.setText(str(sum(job.status == JobStatus.COMPLETED for job in jobs)))
        self.kpi_failed.setText(str(sum(job.status == JobStatus.FAILED for job in jobs)))
        self.kpi_outputs.setText(str(sum(len(job.outputs) for job in jobs)))


class DownloadPage(BasePage):
    def __init__(
        self, settings: AppSettings, jobs: JobManager, parent: QWidget | None = None
    ) -> None:
        super().__init__(
            "Download",
            "Speichert rechtmäßig nutzbare Medien über das optionale yt-dlp-Backend.",
            parent,
        )
        self.settings = settings
        self.jobs = jobs
        self._inspect_worker: InspectWorker | None = None
        self._preview_source = ""
        card = Card()
        card.layout.addWidget(section_title("Quelle und Ausgabe"))
        form = QFormLayout()
        form.setSpacing(12)
        self.url = QLineEdit()
        self.url.setPlaceholderText("YouTube-ID oder vollständige URL")
        self.mode = QComboBox()
        self.mode.addItem("Video", "video")
        self.mode.addItem("Nur Audio", "audio")
        self.format = QComboBox()
        self.format.addItems(["MP4", "MKV", "WebM"])
        self.quality = QComboBox()
        for label, height in (
            ("Bis 2160p", 2160),
            ("Bis 1440p", 1440),
            ("Bis 1080p", 1080),
            ("Bis 720p", 720),
            ("Bis 480p", 480),
        ):
            self.quality.addItem(label, height)
        self.output = PathPicker(settings.download_dir)
        add_form_row(form, "Adresse", self.url)
        add_form_row(form, "Modus", self.mode)
        add_form_row(form, "Container", self.format)
        add_form_row(form, "Qualität", self.quality)
        add_form_row(form, "Ausgabeordner", self.output)
        card.layout.addLayout(form)
        preview_actions = QHBoxLayout()
        self.preview_status = QLabel("Playlists vor dem Download prüfen")
        self.preview_status.setObjectName("Muted")
        load_preview = QPushButton("Inhalte laden")
        load_preview.clicked.connect(self.load_preview)
        preview_actions.addWidget(self.preview_status, 1)
        preview_actions.addWidget(load_preview)
        card.layout.addLayout(preview_actions)
        self.preview = PlaylistPreview()
        card.layout.addWidget(self.preview)
        options = QHBoxLayout()
        self.playlist = QCheckBox("Playlist/Album vollständig")
        self.subtitles = QCheckBox("Untertitel speichern")
        self.thumbnail = QCheckBox("Vorschaubild speichern")
        self.thumbnail.setChecked(True)
        options.addWidget(self.playlist)
        options.addWidget(self.subtitles)
        options.addWidget(self.thumbnail)
        options.addStretch()
        card.layout.addLayout(options)
        self.rights = QCheckBox("Ich darf diese Inhalte herunterladen und verarbeiten.")
        card.layout.addWidget(self.rights)
        actions = QHBoxLayout()
        actions.addStretch()
        enqueue = primary_button("Zur Queue hinzufügen")
        enqueue.clicked.connect(self.submit)
        actions.addWidget(enqueue)
        card.layout.addLayout(actions)
        self.body.addWidget(card)
        info = Card()
        info.layout.addWidget(section_title("Hinweis"))
        info.layout.addWidget(
            muted(
                "Die App übernimmt keine Cookies oder Logins und umgeht keine Zugriffsschutzmaßnahmen. Nicht jede von yt-dlp grundsätzlich erkannte Quelle ist zwangsläufig rechtmäßig nutzbar."
            )
        )
        self.body.addWidget(info)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        self.finish()

    def load_preview(self) -> None:
        source = self.url.text().strip()
        if not source:
            return show_validation(self, "Bitte eine YouTube-ID oder Medienadresse eingeben.")
        if self._inspect_worker is not None:
            return
        self.preview_status.setText("Inhalte werden geladen …")
        worker = InspectWorker(source)
        self._inspect_worker = worker
        worker.signals.completed.connect(
            lambda result, value=source: self._preview_ready(value, result)
        )
        worker.signals.failed.connect(self._preview_failed)
        QThreadPool.globalInstance().start(worker)

    def _preview_ready(self, source: str, collection: dict) -> None:
        self._inspect_worker = None
        self._preview_source = source
        self.preview.set_collection(collection)
        self.preview_status.setText("Nicht gewünschte Zeilen markieren und entfernen")
        self.playlist.setChecked(bool(collection.get("is_playlist")))

    def _preview_failed(self, error: str) -> None:
        self._inspect_worker = None
        self.preview_status.setText("Vorschau fehlgeschlagen")
        QMessageBox.warning(self, "Vorschau fehlgeschlagen", error)

    def _mode_changed(self) -> None:
        is_audio = self.mode.currentData() == "audio"
        self.format.clear()
        self.format.addItems(["MP3", "M4A", "FLAC", "WAV"] if is_audio else ["MP4", "MKV", "WebM"])
        self.quality.setEnabled(not is_audio)

    def submit(self) -> None:
        if not self.url.text().strip():
            return show_validation(self, "Bitte eine YouTube-ID oder Medienadresse eingeben.")
        if not self.rights.isChecked():
            return show_validation(self, "Bitte die Nutzungsberechtigung bestätigen.")
        output = Path(self.output.text()).expanduser()
        mode = self.mode.currentData()
        url = self.url.text().strip()
        playlist = self.playlist.isChecked() or DownloadService.looks_like_playlist(url)
        if playlist and self._preview_source != url:
            return show_validation(self, "Bitte die Playlist zuerst mit „Inhalte laden“ prüfen.")
        selected = (
            self.preview.selected_indices()
            if self._preview_source == url and self.preview.isVisible()
            else None
        )
        if selected == []:
            return show_validation(self, "In der Vorschau ist kein Eintrag mehr ausgewählt.")
        options = DownloadOptions(
            mode=mode,
            video_format=self.format.currentText().lower(),
            max_height=int(self.quality.currentData() or 1080),
            audio_format=self.format.currentText().lower(),
            playlist=playlist,
            subtitles=self.subtitles.isChecked(),
            thumbnail=self.thumbnail.isChecked(),
            playlist_items=selected,
        )

        def runner(progress, cancel):
            return DownloadService().download(url, output, options, progress, cancel)

        self.jobs.submit(
            JobKind.DOWNLOAD,
            "Medien herunterladen",
            url,
            str(output),
            {"mode": mode, "format": self.format.currentText()},
            runner,
        )
        self.url.clear()
        self.preview.clear()
        self._preview_source = ""


class MusicPage(BasePage):
    def __init__(
        self, settings: AppSettings, jobs: JobManager, parent: QWidget | None = None
    ) -> None:
        super().__init__(
            "Musik",
            "Audio und Playlists als MP3 exportieren oder im MP3-Studio Ton, Cover, Tags und Lyrics zusammenbauen.",
            parent,
        )
        self.settings = settings
        self.jobs = jobs
        self._music_inspect_worker: InspectWorker | None = None
        self._music_preview_source = ""
        tabs = QTabWidget()
        tabs.addTab(self._download_tab(), "Online → MP3")
        tabs.addTab(self._tag_tab(), "MP3-Studio")
        self.body.addWidget(tabs)
        self.finish()

    def _metadata_fields(self):
        fields = {}
        widget = QWidget()
        form = QFormLayout(widget)
        for key, label, placeholder in (
            ("title", "Titel", "optional – vorhandenen Titel behalten"),
            ("artist", "Interpret", "optional"),
            ("album", "Album", "optional"),
            ("album_artist", "Album-Interpret", "optional"),
            ("year", "Jahr", "optional"),
            ("genre", "Genre", "optional"),
            ("track", "Tracknummer", "z. B. 3/12"),
        ):
            edit = QLineEdit()
            edit.setPlaceholderText(placeholder)
            fields[key] = edit
            add_form_row(form, label, edit)
        return widget, fields

    @staticmethod
    def _metadata(
        fields: dict[str, QLineEdit],
        cover: PathPicker,
        lyrics: PathPicker,
        source_url: str = "",
        direct_lyrics: str = "",
    ) -> AudioMetadata:
        lyrics_text = direct_lyrics.strip()
        lyrics_path = Path(lyrics.text()).expanduser() if lyrics.text() else None
        if not lyrics_text and lyrics_path and lyrics_path.is_file():
            lyrics_text = lyrics_path.read_text(encoding="utf-8", errors="replace")
        cover_path = Path(cover.text()).expanduser() if cover.text() else None
        return AudioMetadata(
            **{key: edit.text().strip() for key, edit in fields.items()},
            source_url=source_url,
            lyrics=lyrics_text,
            cover=cover_path if cover_path and cover_path.is_file() else None,
        )

    def _download_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 14, 0, 0)
        layout.setSpacing(14)
        card = Card()
        form = QFormLayout()
        self.music_url = QLineEdit()
        self.music_url.setPlaceholderText("YouTube-ID, YouTube- oder YouTube-Music-URL")
        self.music_quality = QComboBox()
        self.music_quality.addItems(["320", "256", "192", "128"])
        self.music_output = PathPicker(self.settings.download_dir)
        self.music_cover = PathPicker(mode="file", file_filter="Bilder (*.jpg *.jpeg *.png *.webp)")
        self.music_lyrics = PathPicker(mode="file", file_filter="Lyrics (*.lrc *.txt)")
        add_form_row(form, "Adresse", self.music_url)
        add_form_row(form, "MP3-kbit/s", self.music_quality)
        add_form_row(form, "Ausgabeordner", self.music_output)
        add_form_row(form, "Eigenes Cover", self.music_cover)
        add_form_row(form, "Lyrics/LRC", self.music_lyrics)
        card.layout.addLayout(form)
        self.music_lyrics_text = QPlainTextEdit()
        self.music_lyrics_text.setPlaceholderText(
            "Lyrics direkt einfügen (überschreibt die Lyrics-Datei)"
        )
        self.music_lyrics_text.setMaximumHeight(105)
        card.layout.addWidget(self.music_lyrics_text)
        metadata_widget, self.music_fields = self._metadata_fields()
        card.layout.addWidget(metadata_widget)
        preview_row = QHBoxLayout()
        self.music_preview_status = QLabel("Playlists vor dem Download prüfen")
        self.music_preview_status.setObjectName("Muted")
        music_preview_button = QPushButton("Inhalte laden")
        music_preview_button.clicked.connect(self.load_music_preview)
        preview_row.addWidget(self.music_preview_status, 1)
        preview_row.addWidget(music_preview_button)
        card.layout.addLayout(preview_row)
        self.music_preview = PlaylistPreview()
        card.layout.addWidget(self.music_preview)
        self.music_playlist = QCheckBox("Playlist/Album vollständig")
        self.music_normalize = QCheckBox(
            "Lautheit auf -16 LUFS normalisieren (erzeugt zusätzliche Datei)"
        )
        self.music_rights = QCheckBox("Ich darf diese Inhalte herunterladen und verarbeiten.")
        card.layout.addWidget(self.music_playlist)
        card.layout.addWidget(self.music_normalize)
        card.layout.addWidget(self.music_rights)
        action = primary_button("MP3-Job starten")
        action.clicked.connect(self.submit_music_download)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(action)
        card.layout.addLayout(row)
        layout.addWidget(card)
        layout.addStretch()
        return tab

    def _tag_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 14, 0, 0)
        layout.setSpacing(14)
        card = Card()
        self.local_mp3 = FileDropList("MP3 (*.mp3)", {".mp3"}, multiple=True)
        card.layout.addWidget(self.local_mp3)
        self.local_output = PathPicker(self.settings.download_dir)
        self.local_output_name = QLineEdit()
        self.local_output_name.setPlaceholderText("optional, sonst Titel oder MP3-Mix")
        self.local_cover = PathPicker(mode="file", file_filter="Bilder (*.jpg *.jpeg *.png *.webp)")
        self.local_lyrics = PathPicker(mode="file", file_filter="Lyrics (*.lrc *.txt)")
        form = QFormLayout()
        add_form_row(form, "Ausgabeordner", self.local_output)
        add_form_row(form, "Ausgabename", self.local_output_name)
        add_form_row(form, "Cover", self.local_cover)
        add_form_row(form, "Lyrics/LRC", self.local_lyrics)
        card.layout.addLayout(form)
        self.local_lyrics_text = QPlainTextEdit()
        self.local_lyrics_text.setPlaceholderText(
            "Lyrics hier direkt einfügen – ideal für eine MP3 aus Ton + Cover + Text + Tags"
        )
        self.local_lyrics_text.setMaximumHeight(120)
        card.layout.addWidget(self.local_lyrics_text)
        metadata_widget, self.local_fields = self._metadata_fields()
        card.layout.addWidget(metadata_widget)
        self.local_merge = QCheckBox("Mehrere MP3s in der angezeigten Reihenfolge verbinden")
        self.local_normalize = QCheckBox("Lautheit normalisieren")
        card.layout.addWidget(self.local_merge)
        card.layout.addWidget(self.local_normalize)
        action = primary_button("MP3 bauen")
        action.clicked.connect(self.submit_local_audio)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(action)
        card.layout.addLayout(row)
        layout.addWidget(card)
        layout.addStretch()
        return tab

    def load_music_preview(self) -> None:
        source = self.music_url.text().strip()
        if not source:
            return show_validation(self, "Bitte eine YouTube-ID oder Musikadresse eingeben.")
        if self._music_inspect_worker is not None:
            return
        self.music_preview_status.setText("Inhalte werden geladen …")
        worker = InspectWorker(source)
        self._music_inspect_worker = worker
        worker.signals.completed.connect(
            lambda result, value=source: self._music_preview_ready(value, result)
        )
        worker.signals.failed.connect(self._music_preview_failed)
        QThreadPool.globalInstance().start(worker)

    def _music_preview_ready(self, source: str, collection: dict) -> None:
        self._music_inspect_worker = None
        self._music_preview_source = source
        self.music_preview.set_collection(collection)
        self.music_preview_status.setText("Nicht gewünschte Zeilen markieren und entfernen")
        self.music_playlist.setChecked(bool(collection.get("is_playlist")))

    def _music_preview_failed(self, error: str) -> None:
        self._music_inspect_worker = None
        self.music_preview_status.setText("Vorschau fehlgeschlagen")
        QMessageBox.warning(self, "Vorschau fehlgeschlagen", error)

    def submit_music_download(self) -> None:
        url = self.music_url.text().strip()
        if not url:
            return show_validation(self, "Bitte eine Musikadresse eingeben.")
        if not self.music_rights.isChecked():
            return show_validation(self, "Bitte die Nutzungsberechtigung bestätigen.")
        output = Path(self.music_output.text()).expanduser()
        fields = self.music_fields
        cover, lyrics = self.music_cover, self.music_lyrics
        normalize = self.music_normalize.isChecked()
        quality = self.music_quality.currentText()
        playlist = self.music_playlist.isChecked() or DownloadService.looks_like_playlist(url)
        if playlist and self._music_preview_source != url:
            return show_validation(self, "Bitte die Playlist zuerst mit „Inhalte laden“ prüfen.")
        selected = (
            self.music_preview.selected_indices()
            if self._music_preview_source == url and self.music_preview.isVisible()
            else None
        )
        if selected == []:
            return show_validation(self, "In der Vorschau ist kein Eintrag mehr ausgewählt.")
        try:
            metadata = self._metadata(
                fields,
                cover,
                lyrics,
                source_url=url,
                direct_lyrics=self.music_lyrics_text.toPlainText(),
            )
        except OSError as exc:
            return show_validation(self, f"Cover oder Lyrics konnten nicht gelesen werden: {exc}")

        def runner(progress, cancel):
            options = DownloadOptions(
                mode="audio",
                audio_format="mp3",
                audio_quality=quality,
                thumbnail=True,
                playlist=playlist,
                playlist_items=selected,
            )
            files = DownloadService().download(url, output, options, progress, cancel)
            results: list[Path] = []
            for path in files:
                if path.suffix.lower() != ".mp3":
                    continue
                if normalize:
                    result = AudioService().normalize_mp3(
                        path, output / f"{path.stem}_normalisiert.mp3", progress, cancel
                    )
                else:
                    result = path
                AudioService().tag_mp3(result, metadata)
                results.append(result)
            return results or files

        self.jobs.submit(
            JobKind.MUSIC,
            "Musik als MP3",
            url,
            str(output),
            {"quality": quality, "normalize": normalize},
            runner,
        )
        self.music_url.clear()
        self.music_preview.clear()
        self._music_preview_source = ""

    def submit_local_audio(self) -> None:
        paths = self.local_mp3.paths()
        if not paths:
            return show_validation(self, "Bitte eine MP3-Datei auswählen.")
        output = Path(self.local_output.text()).expanduser()
        try:
            metadata = self._metadata(
                self.local_fields,
                self.local_cover,
                self.local_lyrics,
                direct_lyrics=self.local_lyrics_text.toPlainText(),
            )
        except OSError as exc:
            return show_validation(self, f"Cover oder Lyrics konnten nicht gelesen werden: {exc}")
        normalize = self.local_normalize.isChecked()
        merge = self.local_merge.isChecked() or len(paths) > 1
        output_name = self.local_output_name.text().strip()

        def runner(progress, cancel):
            return AudioService().compose_mp3(
                paths,
                output,
                metadata,
                merge=merge,
                normalize=normalize,
                output_name=output_name,
                progress=progress,
                cancel=cancel,
            )

        self.jobs.submit(
            JobKind.AUDIO,
            "MP3 bauen" if merge else "MP3-Tags bearbeiten",
            f"{len(paths)} MP3-Datei(en)",
            str(output),
            {"normalize": normalize, "merge": merge},
            runner,
        )


class ImagesPage(BasePage):
    IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".bmp", ".tif", ".tiff"}

    def __init__(
        self, settings: AppSettings, jobs: JobManager, parent: QWidget | None = None
    ) -> None:
        super().__init__(
            "Bilder",
            "Bilder stapelweise konvertieren, skalieren und auf Wunsch an eine Zielgröße annähern.",
            parent,
        )
        self.settings = settings
        self.jobs = jobs
        card = Card()
        self.files = FileDropList(
            "Bilder (*.jpg *.jpeg *.png *.webp *.avif *.bmp *.tif *.tiff)", self.IMAGE_EXTENSIONS
        )
        card.layout.addWidget(self.files)
        form = QFormLayout()
        form.setSpacing(12)
        self.image_output = PathPicker(settings.image_dir)
        self.image_format = QComboBox()
        self.image_format.addItems(["Original", "JPEG", "PNG", "WebP", "AVIF"])
        self.image_format.setCurrentText("WebP")
        self.image_quality = QSlider(Qt.Orientation.Horizontal)
        self.image_quality.setRange(20, 100)
        self.image_quality.setValue(82)
        self.image_quality_label = QLabel("82 %")
        quality_row = QWidget()
        quality_layout = QHBoxLayout(quality_row)
        quality_layout.setContentsMargins(0, 0, 0, 0)
        quality_layout.addWidget(self.image_quality, 1)
        quality_layout.addWidget(self.image_quality_label)
        self.image_quality.valueChanged.connect(
            lambda value: self.image_quality_label.setText(f"{value} %")
        )
        size_row = QWidget()
        size_layout = QHBoxLayout(size_row)
        size_layout.setContentsMargins(0, 0, 0, 0)
        self.max_width = QSpinBox()
        self.max_width.setRange(0, 30000)
        self.max_width.setSpecialValueText("Original")
        self.max_width.setSuffix(" px")
        self.max_height = QSpinBox()
        self.max_height.setRange(0, 30000)
        self.max_height.setSpecialValueText("Original")
        self.max_height.setSuffix(" px")
        size_layout.addWidget(self.max_width)
        size_layout.addWidget(QLabel("×"))
        size_layout.addWidget(self.max_height)
        self.target_kb = QSpinBox()
        self.target_kb.setRange(0, 250000)
        self.target_kb.setSpecialValueText("Keine Zielgröße")
        self.target_kb.setSuffix(" KB")
        add_form_row(form, "Ausgabeordner", self.image_output)
        add_form_row(form, "Format", self.image_format)
        add_form_row(form, "Qualität", quality_row)
        add_form_row(form, "Maximale Abmessung", size_row)
        add_form_row(form, "Zielgröße je Bild", self.target_kb)
        card.layout.addLayout(form)
        self.keep_metadata = QCheckBox("EXIF- und Farbprofil-Metadaten behalten")
        card.layout.addWidget(self.keep_metadata)
        row = QHBoxLayout()
        row.addStretch()
        action = primary_button("Bilder optimieren")
        action.clicked.connect(self.submit)
        row.addWidget(action)
        card.layout.addLayout(row)
        self.body.addWidget(card)
        self.finish()

    def submit(self) -> None:
        sources = self.files.paths()
        if not sources:
            return show_validation(self, "Bitte mindestens ein Bild auswählen.")
        output = Path(self.image_output.text()).expanduser()
        options = ImageOptions(
            output_format=self.image_format.currentText(),
            quality=self.image_quality.value(),
            max_width=self.max_width.value(),
            max_height=self.max_height.value(),
            target_kb=self.target_kb.value(),
            preserve_metadata=self.keep_metadata.isChecked(),
        )

        def runner(progress, cancel):
            return ImageService().process_many(sources, output, options, progress, cancel)

        self.jobs.submit(
            JobKind.IMAGE,
            f"{len(sources)} Bild(er) optimieren",
            f"{len(sources)} Dateien",
            str(output),
            {"format": options.output_format, "quality": options.quality},
            runner,
        )


class VideosPage(BasePage):
    VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".ts", ".mts"}

    def __init__(
        self, settings: AppSettings, jobs: JobManager, parent: QWidget | None = None
    ) -> None:
        super().__init__(
            "Videos",
            "Videos lokal komprimieren, visuell schneiden und markierte Segmente oder GIFs exportieren.",
            parent,
        )
        self.settings = settings
        self.jobs = jobs
        card = Card()
        self.files = FileDropList(
            "Videos (*.mp4 *.mkv *.webm *.mov *.avi *.m4v *.ts *.mts)", self.VIDEO_EXTENSIONS
        )
        card.layout.addWidget(self.files)
        form = QFormLayout()
        form.setSpacing(12)
        self._applying_profile = False
        self.compression_profile = QComboBox()
        self.compression_profile.addItem("Manuell / eigene Einstellungen", None)
        for profile in VIDEO_COMPRESSION_PROFILES:
            self.compression_profile.addItem(profile.name, profile.key)
        self.profile_description = QLabel(
            "Eigene Einstellungen verwenden oder oben ein vordefiniertes Profil auswählen."
        )
        self.profile_description.setObjectName("Muted")
        self.profile_description.setWordWrap(True)
        self.video_output = PathPicker(settings.video_dir)
        self.container = QComboBox()
        self.container.addItems(["MP4", "MKV", "WebM"])
        self.codec = QComboBox()
        self.codec.addItem("H.264 – kompatibel", "h264")
        self.codec.addItem("H.265 / HEVC – kleiner", "h265")
        self.codec.addItem("VP9 – WebM", "vp9")
        self.codec.addItem("AV1 – sehr klein (Software langsam)", "av1")
        self.preset = QComboBox()
        self.preset.addItem("Schnell", "fast")
        self.preset.addItem("Ausgewogen", "balanced")
        self.preset.addItem("Klein", "small")
        self.preset.addItem("Hohe Qualität", "quality")
        self.preset.setCurrentIndex(1)
        self.encoder_backend = QComboBox()
        self.encoder_backend.addItem("CPU / Software", "software")
        self.encoder_backend.addItem("NVIDIA NVENC / GPU", "nvenc")
        self.encoder_backend.setToolTip(
            "NVENC verwendet die dedizierte Video-Encoder-Hardware einer NVIDIA-GPU für H.264, "
            "HEVC oder AV1. VP9 bleibt CPU/Software."
        )
        self.nvenc_mode = QComboBox()
        self.nvenc_mode.addItem("Qualität / normal", "quality")
        self.nvenc_mode.addItem("Aufnahme schonen", "recording")
        self.nvenc_mode.setCurrentIndex(1)
        self.nvenc_mode.setToolTip(
            "'Aufnahme schonen' nutzt nur einen Hintergrund-NVENC-Job, Preset P4, kein NVENC-Multipass "
            "und kein Lookahead. Bei HEVC/AV1 wird Split-Frame-Encoding deaktiviert, damit ein einzelner "
            "Datei-Encode nicht mehrere NVENC-Engines belegt."
        )
        self.cpu_mode = QComboBox()
        self.cpu_mode.addItem("Alle Kerne / normal", "all")
        self.cpu_mode.addItem("Nur E-Cores / Hintergrund", "e_cores")
        self.cpu_mode.setToolTip(
            "Im E-Core-Modus erkennt Windows die Effizienzklassen der CPU automatisch und "
            "führt FFmpeg nur auf der energieeffizientesten Kernklasse aus. Ideal, um die "
            "P-Cores für Spiele, Browser und andere Vordergrundprogramme freizuhalten."
        )
        self.cpu_limit = QSpinBox()
        self.cpu_limit.setRange(10, 100)
        self.cpu_limit.setSingleStep(5)
        self.cpu_limit.setValue(100)
        self.cpu_limit.setSuffix(" %")
        self.cpu_limit.setToolTip(
            "Begrenzt FFmpeg ungefähr auf diesen Anteil der im CPU-Modus erlaubten logischen Prozessoren. "
            "Im E-Core-Modus bedeutet 100 %: alle erkannten E-Cores."
        )
        self.crf = QSlider(Qt.Orientation.Horizontal)
        self.crf.setRange(14, 40)
        self.crf.setValue(23)
        self.crf_label = QLabel("23")
        crf_widget = QWidget()
        crf_layout = QHBoxLayout(crf_widget)
        crf_layout.setContentsMargins(0, 0, 0, 0)
        crf_layout.addWidget(self.crf, 1)
        crf_layout.addWidget(self.crf_label)
        self.crf.valueChanged.connect(lambda value: self.crf_label.setText(str(value)))
        self.height = QComboBox()
        for label, value in (
            ("Original", 0),
            ("2160p", 2160),
            ("1440p", 1440),
            ("1080p", 1080),
            ("720p", 720),
            ("480p", 480),
        ):
            self.height.addItem(label, value)
        self.fps = QComboBox()
        for label, value in (
            ("Original", 0),
            ("60 fps", 60),
            ("30 fps", 30),
            ("25 fps", 25),
            ("24 fps", 24),
        ):
            self.fps.addItem(label, value)
        self.target_mb = QSpinBox()
        self.target_mb.setRange(0, 500000)
        self.target_mb.setSpecialValueText("CRF/CQ-Modus")
        self.target_mb.setSuffix(" MB")
        self.target_preset = QComboBox()
        for label, value in (
            ("Benutzerdefiniert / CRF-CQ", 0),
            ("Chat-Upload klein · 8 MB", 8),
            ("Discord · 10 MB", 10),
            ("WhatsApp kompakt · 16 MB", 16),
            ("Discord · 25 MB", 25),
            ("Großer Upload · 50 MB", 50),
        ):
            self.target_preset.addItem(label, value)
        self.audio_bitrate = QComboBox()
        for value in (320, 256, 192, 160, 128, 96):
            self.audio_bitrate.addItem(f"{value} kbit/s", value)
        self.audio_bitrate.setCurrentText("160 kbit/s")
        self.rotation = QComboBox()
        for label, value in (("Keine", 0), ("90° rechts", 90), ("180°", 180), ("90° links", 270)):
            self.rotation.addItem(label, value)
        add_form_row(form, "Kompressionsprofil", self.compression_profile)
        add_form_row(form, "Profilinfo", self.profile_description)
        add_form_row(form, "Ausgabeordner", self.video_output)
        add_form_row(form, "Container", self.container)
        add_form_row(form, "Videocodec", self.codec)
        add_form_row(form, "Preset", self.preset)
        add_form_row(form, "Encoder-Engine", self.encoder_backend)
        add_form_row(form, "NVENC-Modus", self.nvenc_mode)
        add_form_row(form, "CPU-Modus", self.cpu_mode)
        add_form_row(form, "CPU-Limit", self.cpu_limit)
        add_form_row(form, "Qualität (CRF / CQ)", crf_widget)
        add_form_row(form, "Maximale Auflösung", self.height)
        add_form_row(form, "Bildrate", self.fps)
        add_form_row(form, "Zielgrößen-Preset", self.target_preset)
        add_form_row(form, "Zielgröße je Video", self.target_mb)
        add_form_row(form, "Audio", self.audio_bitrate)
        add_form_row(form, "Drehung", self.rotation)
        self.hdr_dual_export = QCheckBox("HDR + SDR erzeugen (nur HDR10/HLG-Quellen)")
        self.hdr_codec = QComboBox()
        self.hdr_codec.addItem("H.265 / HEVC 10-Bit – empfohlen", "h265")
        self.hdr_codec.addItem("AV1 10-Bit – kleiner (Software langsamer, NVENC schnell)", "av1")
        self.hdr_tone_map = QComboBox()
        self.hdr_tone_map.addItem("Hable – filmisch / Standard", "hable")
        self.hdr_tone_map.addItem("Mobius – Highlights sanfter", "mobius")
        self.hdr_tone_map.addItem("Reinhard – weich", "reinhard")
        add_form_row(form, "HDR-Dual-Export", self.hdr_dual_export)
        add_form_row(form, "HDR-Ausgabe-Codec", self.hdr_codec)
        add_form_row(form, "HDR → SDR Tone-Mapping", self.hdr_tone_map)
        card.layout.addLayout(form)
        self.mute = QCheckBox("Ton entfernen")
        card.layout.addWidget(self.mute)
        self.hdr_dual_info = muted(
            "Dual-Export erzeugt pro HDR-Quelle zwei Dateien: *_HDR mit 10-Bit HDR-Farbinformationen "
            "und *_SDR mit BT.709-Tone-Mapping. Die SDR-Datei nutzt Codec/CRF-CQ/Auflösung/Audio des "
            "gewählten Profils; für HDR wird der oben gewählte HDR-Codec verwendet."
        )
        card.layout.addWidget(self.hdr_dual_info)
        card.layout.addWidget(
            muted(
                "NVIDIA NVENC: Die Profile verwenden bei GPU-Encoding automatisch h264_nvenc, hevc_nvenc bzw. "
                "av1_nvenc; der Qualitätswert wird als CQ interpretiert. 'Aufnahme schonen' begrenzt nicht auf "
                "einen erfundenen GPU-Prozentwert, sondern reduziert NVENC-Aufwand, deaktiviert HEVC/AV1-Split-Frame "
                "und serialisiert diese Hintergrund-Encodes, damit eine laufende Aufnahme möglichst viel Reserve behält."
            )
        )
        card.layout.addWidget(
            muted(
                "CPU-Modus: 'Nur E-Cores / Hintergrund' nutzt unter Windows die gemeldete EfficiencyClass und "
                "bindet FFmpeg ausschließlich an die effizienteste Kernklasse. Das CPU-Limit gilt dann innerhalb "
                "dieser E-Cores; 100 % bedeutet alle erkannten E-Cores. 'Alle Kerne' verwendet weiterhin die "
                "normale Prozentbegrenzung. Der E-Core-Modus setzt FFmpeg zusätzlich auf niedrige Prozesspriorität."
            )
        )
        card.layout.addWidget(
            muted(
                "Je kleiner CRF/CQ, desto höher die Qualität. Software-H.264/H.265 verwenden bei fester Zielgröße zwei Durchläufe; NVENC verwendet dafür VBR in einem Hardware-Encoding-Durchlauf."
            )
        )
        action_row = QHBoxLayout()
        action_row.addStretch()
        action = primary_button("Videos komprimieren")
        action.clicked.connect(self.submit)
        action_row.addWidget(action)
        card.layout.addLayout(action_row)
        self.body.addWidget(card)
        self.container.currentTextChanged.connect(self._container_changed)
        self.target_preset.currentIndexChanged.connect(self._target_preset_changed)
        self.target_mb.valueChanged.connect(self._update_crf_state)
        self.compression_profile.currentIndexChanged.connect(self._compression_profile_changed)
        for control, signal_name in (
            (self.container, "currentIndexChanged"),
            (self.codec, "currentIndexChanged"),
            (self.preset, "currentIndexChanged"),
            (self.crf, "valueChanged"),
            (self.height, "currentIndexChanged"),
            (self.fps, "currentIndexChanged"),
            (self.target_preset, "currentIndexChanged"),
            (self.target_mb, "valueChanged"),
            (self.audio_bitrate, "currentIndexChanged"),
            (self.mute, "toggled"),
            (self.rotation, "currentIndexChanged"),
        ):
            getattr(control, signal_name).connect(self._compression_setting_changed)
        self.hdr_dual_export.toggled.connect(self._update_hdr_dual_state)
        self.encoder_backend.currentIndexChanged.connect(self._update_encoder_state)
        self.nvenc_mode.currentIndexChanged.connect(self._update_encoder_state)
        self._update_crf_state()
        self._update_hdr_dual_state()
        self._update_encoder_state()

        self._cut_duration_initialized = False
        self._cut_source_duration = 0.0
        self._cut_markers: list[float] = []
        self._direct_cut_segments: list[dict[str, object]] = []
        self._manual_cut_names: dict[tuple[float, float], str] = {}
        self._refreshing_cut_table = False

        cutter_card = Card()
        cutter_card.layout.addWidget(section_title("Video in mehrere Segmente schneiden"))
        cutter_card.layout.addWidget(
            muted(
                "Video laden und entweder gewünschte Zeitbereiche direkt eingeben oder wie bisher fortlaufende "
                "Schnittmarken setzen. Im Bereichsmodus werden ausschließlich die eingetragenen Stellen exportiert; "
                "Lücken und nicht markiertes Material werden vollständig verworfen."
            )
        )
        self.cut_file = FileDropList(
            "Videos (*.mp4 *.mkv *.webm *.mov *.avi *.m4v *.ts *.mts)",
            self.VIDEO_EXTENSIONS,
            multiple=False,
        )
        self.cut_file.files_changed.connect(self._cut_file_changed)
        cutter_card.layout.addWidget(self.cut_file)

        self.cut_preview = VideoPreview()
        self.cut_preview.duration_changed.connect(self._cut_duration_changed)
        cutter_card.layout.addWidget(self.cut_preview)

        mode_form = QFormLayout()
        self.cut_mode = QComboBox()
        self.cut_mode.addItem(
            "Bereiche direkt eingeben – nur diese Stellen behalten", "ranges"
        )
        self.cut_mode.addItem(
            "Fortlaufend teilen – Schnittmarken zwischen Start und Ende", "markers"
        )
        self.cut_mode.currentIndexChanged.connect(self._cut_mode_changed)
        add_form_row(mode_form, "Schnittmodus", self.cut_mode)
        cutter_card.layout.addLayout(mode_form)

        self.cut_direct_panel = QWidget()
        direct_layout = QVBoxLayout(self.cut_direct_panel)
        direct_layout.setContentsMargins(0, 0, 0, 0)
        direct_layout.setSpacing(8)
        direct_input = QHBoxLayout()
        self.cut_range_start = QLineEdit()
        self.cut_range_start.setText("00:00:00.000")
        self.cut_range_start.setPlaceholderText("00:00:00.000")
        self.cut_range_end = QLineEdit()
        self.cut_range_end.setPlaceholderText("Videoende")
        self.cut_range_name = QLineEdit()
        self.cut_range_name.setPlaceholderText("optional: eigener Dateiname")
        direct_input.addWidget(QLabel("Start"))
        direct_input.addWidget(self.cut_range_start)
        direct_input.addWidget(QLabel("Ende"))
        direct_input.addWidget(self.cut_range_end)
        direct_input.addWidget(QLabel("Name"))
        direct_input.addWidget(self.cut_range_name, 1)
        direct_layout.addLayout(direct_input)

        direct_actions = QHBoxLayout()
        range_start_frame = QPushButton("Start ← aktueller Frame")
        range_end_frame = QPushButton("Ende ← aktueller Frame")
        add_range = primary_button("+ Segmentbereich hinzufügen")
        range_start_frame.clicked.connect(self._direct_start_from_frame)
        range_end_frame.clicked.connect(self._direct_end_from_frame)
        add_range.clicked.connect(self._add_direct_cut_segment)
        direct_actions.addWidget(range_start_frame)
        direct_actions.addWidget(range_end_frame)
        direct_actions.addWidget(add_range)
        direct_actions.addStretch()
        direct_layout.addLayout(direct_actions)
        direct_layout.addWidget(
            muted(
                "Zeitformat: HH:MM:SS.mmm, MM:SS.mmm oder Sekunden. Nur die eingetragenen "
                "Bereiche werden exportiert; alles davor, dazwischen und danach wird verworfen. "
                "Start und Ende lassen sich auch direkt vom aktuell angezeigten Frame übernehmen."
            )
        )
        cutter_card.layout.addWidget(self.cut_direct_panel)

        self.cut_marker_panel = QWidget()
        marker_panel_layout = QVBoxLayout(self.cut_marker_panel)
        marker_panel_layout.setContentsMargins(0, 0, 0, 0)
        marker_panel_layout.setSpacing(8)

        marker_actions = QHBoxLayout()
        mark_start = QPushButton("◀ Verarbeitung startet hier")
        add_marker = primary_button("✂ Schnittmarke setzen")
        mark_end = QPushButton("Verarbeitung endet hier ▶")
        mark_start.clicked.connect(self._mark_cut_start)
        add_marker.clicked.connect(self._add_cut_marker)
        mark_end.clicked.connect(self._mark_cut_end)
        marker_actions.addWidget(mark_start)
        marker_actions.addWidget(add_marker)
        marker_actions.addWidget(mark_end)
        marker_actions.addStretch()
        marker_panel_layout.addLayout(marker_actions)

        navigation_actions = QHBoxLayout()
        go_start = QPushButton("Zu Start")
        go_end = QPushButton("Zu Verarbeitungsende")
        go_start.clicked.connect(lambda: self.cut_preview.seek_seconds(self.cut_start.value()))
        go_end.clicked.connect(lambda: self.cut_preview.seek_seconds(self.cut_end.value()))
        navigation_actions.addWidget(go_start)
        navigation_actions.addWidget(go_end)
        navigation_actions.addStretch()
        marker_panel_layout.addLayout(navigation_actions)

        cut_times_form = QFormLayout()
        cut_times = QWidget()
        cut_times_layout = QHBoxLayout(cut_times)
        cut_times_layout.setContentsMargins(0, 0, 0, 0)
        self.cut_start = QDoubleSpinBox()
        self.cut_start.setRange(0, 604_800)
        self.cut_start.setDecimals(3)
        self.cut_start.setSingleStep(0.1)
        self.cut_start.setSuffix(" s")
        self.cut_end = QDoubleSpinBox()
        self.cut_end.setRange(0.001, 604_800)
        self.cut_end.setDecimals(3)
        self.cut_end.setSingleStep(0.1)
        self.cut_end.setValue(5)
        self.cut_end.setSuffix(" s")
        self.cut_start.valueChanged.connect(self._cut_selection_changed)
        self.cut_end.valueChanged.connect(self._cut_selection_changed)
        cut_times_layout.addWidget(QLabel("Start"))
        cut_times_layout.addWidget(self.cut_start)
        cut_times_layout.addWidget(QLabel("Verarbeitungsende"))
        cut_times_layout.addWidget(self.cut_end)
        self.cut_selection = QLabel("1 Segment")
        self.cut_selection.setObjectName("Muted")
        cut_times_layout.addWidget(self.cut_selection)
        cut_times_layout.addStretch()
        add_form_row(cut_times_form, "Verarbeitungsbereich", cut_times)
        marker_panel_layout.addLayout(cut_times_form)
        cutter_card.layout.addWidget(self.cut_marker_panel)

        common_cut_form = QFormLayout()
        common_cut_form.setSpacing(12)
        self.cut_name = QLineEdit()
        self.cut_name.setPlaceholderText("z. B. Video 1 → Video 11, Video 12, Video 13 …")
        self.cut_name.textEdited.connect(self._cut_base_name_changed)
        self.cut_output = PathPicker(settings.video_dir)
        add_form_row(common_cut_form, "Basisname", self.cut_name)
        add_form_row(common_cut_form, "Ausgabeordner", self.cut_output)
        cutter_card.layout.addLayout(common_cut_form)

        self.cut_segments_table = QTableWidget(0, 5)
        self.cut_segments_table.setHorizontalHeaderLabels(
            ["Segment", "Start", "Ende", "Dauer", "Dateiname"]
        )
        self.cut_segments_table.verticalHeader().setVisible(False)
        self.cut_segments_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.cut_segments_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.cut_segments_table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        header = self.cut_segments_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.cut_segments_table.itemChanged.connect(self._cut_table_item_changed)
        self.cut_segments_table.cellDoubleClicked.connect(self._cut_table_double_clicked)
        self.cut_segments_table.setMinimumHeight(170)
        cutter_card.layout.addWidget(self.cut_segments_table)

        segment_actions = QHBoxLayout()
        self.cut_remove_button = QPushButton("Gewählten Segmentbereich löschen")
        self.cut_clear_button = QPushButton("Alle Segmentbereiche löschen")
        self.cut_sort_button = QPushButton("Nach Startzeit sortieren")
        self.cut_remove_button.clicked.connect(self._remove_selected_cut_item)
        self.cut_clear_button.clicked.connect(self._clear_cut_items)
        self.cut_sort_button.clicked.connect(self._sort_direct_cut_segments)
        segment_actions.addWidget(self.cut_remove_button)
        segment_actions.addWidget(self.cut_clear_button)
        segment_actions.addWidget(self.cut_sort_button)
        segment_actions.addStretch()
        cutter_card.layout.addLayout(segment_actions)

        self.cut_segment_info = muted(
            "Noch keine Segmentbereiche eingetragen. Nur explizit eingetragene Bereiche werden exportiert."
        )
        cutter_card.layout.addWidget(self.cut_segment_info)

        self.cut_compress = QCheckBox(
            "Alle Segmente direkt mit den Kompressions-Einstellungen oben exportieren (framegenauer Schnitt)"
        )
        self.cut_compress.setChecked(True)
        cutter_card.layout.addWidget(self.cut_compress)
        cutter_card.layout.addWidget(
            muted(
                "Mit Kompression wird jedes Segment direkt neu encodiert; Container, Codec, CRF, Auflösung, "
                "Zielgröße und Audio werden von oben übernommen. Ohne Profil-Kompression bleibt Start=0 ein "
                "schneller Stream-Copy. Bei einem Start nach 0 wird für eine verlässliche Startgrenze das Video "
                "automatisch in sehr hoher Qualität neu encodiert. Die feste Zielgröße gilt – falls aktiviert – "
                "jeweils pro Segment."
            )
        )
        cut_actions = QHBoxLayout()
        cut_actions.addStretch()
        cut_button = primary_button("Alle Segmente exportieren")
        cut_button.clicked.connect(self.submit_cut)
        cut_actions.addWidget(cut_button)
        cutter_card.layout.addLayout(cut_actions)
        self.body.addWidget(cutter_card)
        self._cut_mode_changed()
        self._refresh_cut_segments()

        gif_card = Card()
        gif_card.layout.addWidget(section_title("Video-Segment als GIF"))
        self.gif_file = FileDropList(
            "Videos (*.mp4 *.mkv *.webm *.mov *.avi *.m4v)", self.VIDEO_EXTENSIONS, multiple=False
        )
        gif_card.layout.addWidget(self.gif_file)
        gif_form = QFormLayout()
        self.gif_output = PathPicker(settings.video_dir)
        times = QWidget()
        times_layout = QHBoxLayout(times)
        times_layout.setContentsMargins(0, 0, 0, 0)
        self.gif_start = QDoubleSpinBox()
        self.gif_start.setRange(0, 86_400)
        self.gif_start.setDecimals(2)
        self.gif_start.setSuffix(" s")
        self.gif_end = QDoubleSpinBox()
        self.gif_end.setRange(0.1, 86_400)
        self.gif_end.setValue(5)
        self.gif_end.setDecimals(2)
        self.gif_end.setSuffix(" s")
        times_layout.addWidget(QLabel("Start"))
        times_layout.addWidget(self.gif_start)
        times_layout.addWidget(QLabel("Ende"))
        times_layout.addWidget(self.gif_end)
        self.gif_fps = QSpinBox()
        self.gif_fps.setRange(4, 30)
        self.gif_fps.setValue(12)
        self.gif_fps.setSuffix(" fps")
        self.gif_width = QSpinBox()
        self.gif_width.setRange(160, 3840)
        self.gif_width.setValue(720)
        self.gif_width.setSuffix(" px")
        self.gif_colors = QComboBox()
        for value in (256, 192, 128, 96, 64):
            self.gif_colors.addItem(f"{value} Farben", value)
        gif_form.addRow("Ausgabeordner:", self.gif_output)
        gif_form.addRow("Segment:", times)
        gif_form.addRow("Bildrate:", self.gif_fps)
        gif_form.addRow("Breite:", self.gif_width)
        gif_form.addRow("Palette:", self.gif_colors)
        gif_card.layout.addLayout(gif_form)
        gif_card.layout.addWidget(
            muted(
                "Eine optimierte Palette hält Text und Spielgrafik scharf; lange GIFs werden sehr groß."
            )
        )
        gif_actions = QHBoxLayout()
        gif_actions.addStretch()
        gif_button = primary_button("GIF-Segment erstellen")
        gif_button.clicked.connect(self.submit_gif)
        gif_actions.addWidget(gif_button)
        gif_card.layout.addLayout(gif_actions)
        self.body.addWidget(gif_card)
        self.finish()

    @staticmethod
    def _set_combo_data(combo: QComboBox, value: object) -> None:
        index = combo.findData(value)
        if index < 0:
            index = combo.findText(str(value))
        if index >= 0:
            combo.setCurrentIndex(index)

    def _compression_profile_changed(self) -> None:
        key = self.compression_profile.currentData()
        if key is None:
            self.profile_description.setText(
                "Eigene Einstellungen verwenden oder oben ein vordefiniertes Profil auswählen."
            )
            return
        profile = next((item for item in VIDEO_COMPRESSION_PROFILES if item.key == key), None)
        if profile is None:
            return
        self._applying_profile = True
        try:
            self._set_combo_data(self.container, profile.container.upper())
            self._set_combo_data(self.codec, profile.codec)
            self._set_combo_data(self.preset, profile.preset)
            self.crf.setValue(profile.crf)
            self._set_combo_data(self.height, profile.height)
            self._set_combo_data(self.fps, profile.fps)
            self._set_combo_data(self.target_preset, profile.target_mb)
            self.target_mb.setValue(profile.target_mb)
            self._set_combo_data(self.audio_bitrate, profile.audio_bitrate)
            self.mute.setChecked(False)
            self._set_combo_data(self.rotation, 0)
            self._refresh_profile_description(profile)
            self._update_crf_state()
        finally:
            self._applying_profile = False

    def _refresh_profile_description(self, profile=None) -> None:
        if profile is None:
            key = self.compression_profile.currentData()
            profile = next((item for item in VIDEO_COMPRESSION_PROFILES if item.key == key), None)
        if profile is None:
            return
        text = profile.description
        if str(self.encoder_backend.currentData() or "software") == "nvenc":
            text += (
                " NVIDIA NVENC nutzt denselben Qualitätswert als CQ; die Profil-Presetstufe wird "
                "auf P3–P6 abgebildet. Im Modus 'Aufnahme schonen' wird unabhängig davon P4 verwendet."
            )
        self.profile_description.setText(text)

    def _update_encoder_state(self, *_args) -> None:
        nvenc = str(self.encoder_backend.currentData() or "software") == "nvenc"
        self.nvenc_mode.setEnabled(nvenc)
        self._update_crf_state()
        self._refresh_profile_description()

    def _compression_setting_changed(self, *_args) -> None:
        if self._applying_profile or self.compression_profile.currentData() is None:
            return
        self.compression_profile.setCurrentIndex(0)

    def _target_preset_changed(self) -> None:
        value = int(self.target_preset.currentData() or 0)
        self.target_mb.setValue(value)
        self._update_crf_state()

    def _update_crf_state(self, *_args) -> None:
        crf_mode = self.target_mb.value() == 0
        self.crf.setEnabled(crf_mode)
        self.crf_label.setEnabled(crf_mode)
        quality_name = (
            "CQ (NVENC)"
            if str(self.encoder_backend.currentData() or "software") == "nvenc"
            else "CRF (Software)"
        )
        if crf_mode:
            self.crf.setToolTip(
                f"{quality_name} ist aktiv: kleinerer Wert = höhere Qualität / größere Datei."
            )
        else:
            self.crf.setToolTip(
                f"{quality_name} ist deaktiviert, weil eine feste Zielgröße aktiv ist."
            )

    def _update_hdr_dual_state(self, *_args) -> None:
        enabled = self.hdr_dual_export.isChecked()
        self.hdr_codec.setEnabled(enabled)
        self.hdr_tone_map.setEnabled(enabled)
        self.hdr_dual_info.setEnabled(enabled)

    def _container_changed(self, value: str) -> None:
        if value.casefold() == "webm" and self.codec.currentData() in {"h264", "h265"}:
            self.codec.setCurrentIndex(2)

    def _video_options(self) -> VideoOptions:
        return VideoOptions(
            container=self.container.currentText().lower(),
            codec=self.codec.currentData(),
            preset=self.preset.currentData(),
            crf=self.crf.value(),
            height=int(self.height.currentData()),
            fps=int(self.fps.currentData()),
            audio_bitrate=int(self.audio_bitrate.currentData()),
            target_mb=self.target_mb.value(),
            mute=self.mute.isChecked(),
            rotation=int(self.rotation.currentData()),
            cpu_limit_percent=self.cpu_limit.value(),
            cpu_mode=str(self.cpu_mode.currentData() or "all"),
            encoder_backend=str(self.encoder_backend.currentData() or "software"),
            nvenc_mode=str(self.nvenc_mode.currentData() or "quality"),
        )

    def _cut_mode_key(self) -> str:
        return str(self.cut_mode.currentData() or "ranges")

    def _cut_mode_changed(self, *_args) -> None:
        direct = self._cut_mode_key() == "ranges"
        self.cut_direct_panel.setVisible(direct)
        self.cut_marker_panel.setVisible(not direct)
        self.cut_sort_button.setVisible(direct)
        if direct:
            self.cut_remove_button.setText("Gewählten Segmentbereich löschen")
            self.cut_clear_button.setText("Alle Segmentbereiche löschen")
        else:
            self.cut_remove_button.setText("Schnittmarke nach gewähltem Segment entfernen")
            self.cut_clear_button.setText("Alle Schnittmarken löschen")
        self._refresh_cut_segments()

    def _cut_file_changed(self) -> None:
        sources = self.cut_file.paths()
        source = sources[0] if sources else None
        self._cut_duration_initialized = False
        self._cut_source_duration = 0.0
        self._cut_markers.clear()
        self._direct_cut_segments.clear()
        self._manual_cut_names.clear()
        self.cut_start.setMaximum(604_800)
        self.cut_end.setMaximum(604_800)
        self.cut_preview.load(None)
        self.cut_start.setValue(0)
        self.cut_end.setValue(5)
        self.cut_range_start.setText("00:00:00.000")
        self.cut_range_end.clear()
        self.cut_range_end.setPlaceholderText("Videoende")
        self.cut_range_name.clear()
        if source is None:
            self.cut_name.clear()
            self._refresh_cut_segments()
            return
        self.cut_name.setText(source.stem)
        fps = 0.0
        try:
            info = VideoService().probe(source)
            duration = float(info.get("format", {}).get("duration") or 0)
            fps = VideoService.frame_rate_from_probe(info)
            if duration > 0:
                self._cut_duration_changed(duration)
        except Exception:
            # The Qt preview or manual time fields can still be used if probing fails here.
            pass
        self.cut_preview.load(source, fps=fps)
        self._refresh_cut_segments()

    def _cut_duration_changed(self, duration: float) -> None:
        if duration <= 0:
            return
        maximum = max(0.001, duration)
        self._cut_source_duration = max(self._cut_source_duration, float(duration))
        self.cut_start.setMaximum(maximum)
        self.cut_end.setMaximum(maximum)
        if not self._cut_duration_initialized:
            self._cut_duration_initialized = True
            self.cut_start.setValue(0)
            self.cut_end.setValue(maximum)
            self.cut_range_start.setText("00:00:00.000")
            self.cut_range_end.setText(self._direct_time_text(maximum))
        self._cut_selection_changed()

    def _cut_selection_changed(self, *_args) -> None:
        self._refresh_cut_segments()

    def _active_cut_markers(self) -> list[float]:
        start = self.cut_start.value()
        end = self.cut_end.value()
        if end <= start:
            return []
        return sorted(
            {
                round(marker, 3)
                for marker in self._cut_markers
                if start + 0.001 < marker < end - 0.001
            }
        )

    def _cut_segments(self) -> list[tuple[float, float]]:
        if self._cut_mode_key() == "ranges":
            return [
                (float(item["start"]), float(item["end"]))
                for item in self._direct_cut_segments
            ]
        return build_cut_segments(
            self.cut_start.value(), self.cut_end.value(), self._cut_markers
        )

    def _default_cut_name(self, index: int) -> str:
        base = self.cut_name.text().strip()
        if not base:
            sources = self.cut_file.paths()
            base = sources[0].stem if sources else "Segment"
        return numbered_segment_name(base, index + 1)

    @staticmethod
    def _cut_segment_key(start: float, end: float) -> tuple[float, float]:
        return round(start, 3), round(end, 3)

    @staticmethod
    def _direct_time_text(seconds: float) -> str:
        milliseconds = max(0, round(float(seconds) * 1000))
        hours, remainder = divmod(milliseconds, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        secs, millis = divmod(remainder, 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"

    def _refresh_cut_segments(self) -> None:
        if not hasattr(self, "cut_segments_table"):
            return
        direct = self._cut_mode_key() == "ranges"
        segments = self._cut_segments()
        active_markers = self._active_cut_markers() if not direct else []
        self._refreshing_cut_table = True
        try:
            self.cut_segments_table.setRowCount(len(segments))
            for row, (start, end) in enumerate(segments):
                number_item = QTableWidgetItem(str(row + 1))
                start_item = QTableWidgetItem(self._direct_time_text(start))
                end_item = QTableWidgetItem(self._direct_time_text(end))
                duration_item = QTableWidgetItem(VideoPreview.format_ms(round((end - start) * 1000)))
                number_item.setFlags(number_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                duration_item.setFlags(duration_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if not direct:
                    start_item.setFlags(start_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    end_item.setFlags(end_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if direct:
                    stored_name = str(self._direct_cut_segments[row].get("name") or "").strip()
                    name = stored_name or self._default_cut_name(row)
                else:
                    key = self._cut_segment_key(start, end)
                    name = self._manual_cut_names.get(key) or self._default_cut_name(row)
                name_item = QTableWidgetItem(name)
                self.cut_segments_table.setItem(row, 0, number_item)
                self.cut_segments_table.setItem(row, 1, start_item)
                self.cut_segments_table.setItem(row, 2, end_item)
                self.cut_segments_table.setItem(row, 3, duration_item)
                self.cut_segments_table.setItem(row, 4, name_item)
        finally:
            self._refreshing_cut_table = False

        kept_duration = sum(end - start for start, end in segments)
        self.cut_selection.setText(
            f"{len(segments)} Segment{'e' if len(segments) != 1 else ''} · "
            f"{VideoPreview.format_ms(round(kept_duration * 1000))}"
        )
        if direct:
            if not segments:
                info = (
                    "Noch keine Bereiche eingetragen. Beispiel: 00:01:30–00:02:00 und "
                    "00:03:00–00:04:00. Alles außerhalb dieser Bereiche wird verworfen."
                )
            else:
                info = (
                    f"{len(segments)} explizite Bereich{'e' if len(segments) != 1 else ''} → "
                    f"{len(segments)} Ausgabedatei{'en' if len(segments) != 1 else ''}. "
                    f"Behalten werden insgesamt {VideoPreview.format_ms(round(kept_duration * 1000))}. "
                    "Alle Lücken zwischen den Bereichen sowie Material davor/danach werden nicht verarbeitet. "
                    "Start, Ende und Dateiname können per Doppelklick direkt in der Tabelle geändert werden."
                )
        else:
            preview_duration = self.cut_preview.duration_seconds()
            ignored = max(0.0, preview_duration - self.cut_end.value()) if preview_duration else 0.0
            if not segments:
                info = "Der Verarbeitungsstart muss vor dem Verarbeitungsende liegen."
            else:
                info = (
                    f"{len(active_markers)} Schnittmarke{'n' if len(active_markers) != 1 else ''} aktiv → "
                    f"{len(segments)} Ausgabedatei{'en' if len(segments) != 1 else ''}. "
                    "Doppelklick auf einen Dateinamen, um ihn individuell zu ändern."
                )
                if ignored >= 0.001:
                    info += (
                        f" Alles ab {VideoPreview.format_ms(round(self.cut_end.value() * 1000))} "
                        f"({VideoPreview.format_ms(round(ignored * 1000))}) wird nicht verarbeitet."
                    )
        self.cut_segment_info.setText(info)

    def _cut_base_name_changed(self, _text: str) -> None:
        self._manual_cut_names.clear()
        self._refresh_cut_segments()

    def _cut_table_item_changed(self, item: QTableWidgetItem) -> None:
        if self._refreshing_cut_table:
            return
        row = item.row()
        if self._cut_mode_key() == "ranges":
            if not 0 <= row < len(self._direct_cut_segments):
                return
            if item.column() in {1, 2}:
                try:
                    value = parse_timecode(item.text())
                    candidate = [
                        (float(entry["start"]), float(entry["end"]))
                        for entry in self._direct_cut_segments
                    ]
                    start, end = candidate[row]
                    candidate[row] = (value, end) if item.column() == 1 else (start, value)
                    duration = self._cut_source_duration or self.cut_preview.duration_seconds()
                    normalize_explicit_segments(candidate, duration)
                except ValueError as exc:
                    show_validation(self, str(exc))
                    self._refresh_cut_segments()
                    return
                # Preserve names by matching the edited row before sorting.
                edited = self._direct_cut_segments[row]
                edited["start"], edited["end"] = candidate[row]
                self._sort_direct_cut_segments(refresh=False)
                self._refresh_cut_segments()
                return
            if item.column() == 4:
                value = item.text().strip()
                default = self._default_cut_name(row)
                self._direct_cut_segments[row]["name"] = "" if value == default else value
                return
            return

        if item.column() != 4:
            return
        segments = self._cut_segments()
        if not 0 <= row < len(segments):
            return
        start, end = segments[row]
        key = self._cut_segment_key(start, end)
        value = item.text().strip()
        default = self._default_cut_name(row)
        if value and value != default:
            self._manual_cut_names[key] = value
        else:
            self._manual_cut_names.pop(key, None)
            if not value:
                self._refresh_cut_segments()

    def _cut_table_double_clicked(self, row: int, column: int) -> None:
        if column == 4:
            return
        segments = self._cut_segments()
        if not 0 <= row < len(segments):
            return
        start, end = segments[row]
        if column in {0, 1}:
            self.cut_preview.seek_seconds(start)
        elif column == 2:
            self.cut_preview.seek_seconds(end)

    def _direct_start_from_frame(self) -> None:
        self.cut_range_start.setText(self._direct_time_text(self.cut_preview.selected_frame_seconds()))

    def _direct_end_from_frame(self) -> None:
        self.cut_range_end.setText(self._direct_time_text(self.cut_preview.selected_frame_seconds()))

    def _add_direct_cut_segment(self) -> None:
        try:
            start = parse_timecode(self.cut_range_start.text())
            end = parse_timecode(self.cut_range_end.text())
            candidate = [
                (float(entry["start"]), float(entry["end"]))
                for entry in self._direct_cut_segments
            ]
            candidate.append((start, end))
            normalize_explicit_segments(
                candidate, self._cut_source_duration or self.cut_preview.duration_seconds()
            )
        except ValueError as exc:
            return show_validation(self, str(exc))
        self._direct_cut_segments.append(
            {"start": start, "end": end, "name": self.cut_range_name.text().strip()}
        )
        self._sort_direct_cut_segments(refresh=False)
        maximum = self._cut_source_duration or self.cut_preview.duration_seconds()
        self.cut_range_start.setText(self._direct_time_text(end))
        if maximum > end + 0.0005:
            self.cut_range_end.setText(self._direct_time_text(maximum))
        else:
            self.cut_range_end.setText(self._direct_time_text(end))
        self.cut_range_name.clear()
        self._refresh_cut_segments()

    def _sort_direct_cut_segments(self, *_args, refresh: bool = True) -> None:
        self._direct_cut_segments.sort(
            key=lambda entry: (float(entry["start"]), float(entry["end"]))
        )
        if refresh:
            self._refresh_cut_segments()

    def _mark_cut_start(self) -> None:
        self.cut_start.setValue(self.cut_preview.selected_frame_seconds())

    def _mark_cut_end(self) -> None:
        self.cut_end.setValue(self.cut_preview.selected_frame_seconds())

    def _add_cut_marker(self) -> None:
        position = round(self.cut_preview.selected_frame_seconds(), 3)
        start = self.cut_start.value()
        end = self.cut_end.value()
        if end <= start:
            return show_validation(self, "Der Verarbeitungsstart muss vor dem Verarbeitungsende liegen.")
        if position <= start + 0.001 or position >= end - 0.001:
            return show_validation(
                self,
                "Die Schnittmarke muss zwischen Verarbeitungsstart und Verarbeitungsende liegen.",
            )
        if any(abs(marker - position) < 0.002 for marker in self._cut_markers):
            return show_validation(self, "An dieser Position existiert bereits eine Schnittmarke.")
        self._cut_markers.append(position)
        self._cut_markers.sort()
        self._refresh_cut_segments()

    def _remove_selected_cut_item(self) -> None:
        selected = self.cut_segments_table.selectionModel().selectedRows()
        if not selected:
            return show_validation(self, "Bitte zuerst eine Tabellenzeile auswählen.")
        row = selected[0].row()
        if self._cut_mode_key() == "ranges":
            if 0 <= row < len(self._direct_cut_segments):
                self._direct_cut_segments.pop(row)
                self._refresh_cut_segments()
            return

        active_markers = self._active_cut_markers()
        if row >= len(active_markers):
            return show_validation(
                self,
                "Nach dem letzten Segment liegt keine Schnittmarke. Bitte ein vorheriges Segment auswählen.",
            )
        marker = active_markers[row]
        self._cut_markers = [value for value in self._cut_markers if abs(value - marker) >= 0.002]
        self._manual_cut_names.clear()
        self._refresh_cut_segments()

    def _clear_cut_items(self) -> None:
        if self._cut_mode_key() == "ranges":
            self._direct_cut_segments.clear()
            self.cut_range_start.setText("00:00:00.000")
            maximum = self._cut_source_duration or self.cut_preview.duration_seconds()
            if maximum > 0:
                self.cut_range_end.setText(self._direct_time_text(maximum))
            else:
                self.cut_range_end.clear()
                self.cut_range_end.setPlaceholderText("Videoende")
            self.cut_range_name.clear()
        else:
            self._cut_markers.clear()
            self._manual_cut_names.clear()
        self._refresh_cut_segments()

    def submit_cut(self) -> None:
        sources = self.cut_file.paths()
        if not sources:
            return show_validation(self, "Bitte genau ein Video zum Schneiden auswählen.")
        segments = self._cut_segments()
        if not segments:
            if self._cut_mode_key() == "ranges":
                return show_validation(self, "Bitte mindestens einen Segmentbereich eingeben.")
            return show_validation(self, "Der Verarbeitungsstart muss vor dem Verarbeitungsende liegen.")

        source = sources[0]
        if self._cut_mode_key() == "ranges":
            try:
                segments = normalize_explicit_segments(
                    segments, self._cut_source_duration or self.cut_preview.duration_seconds()
                )
            except ValueError as exc:
                return show_validation(self, str(exc))
        output = Path(self.cut_output.text()).expanduser()
        names: list[str] = []
        for row in range(len(segments)):
            item = self.cut_segments_table.item(row, 4)
            name = item.text().strip() if item is not None else self._default_cut_name(row)
            names.append(name or self._default_cut_name(row))
        if len({name.casefold() for name in names}) != len(names):
            return show_validation(self, "Jedes Segment benötigt einen eindeutigen Dateinamen.")

        compress = self.cut_compress.isChecked()
        cpu_limit_percent = self.cpu_limit.value()
        cpu_mode = str(self.cpu_mode.currentData() or "all")
        video_options = self._video_options() if compress else None
        if video_options is not None and video_options.encoder_backend == "nvenc" and video_options.codec == "vp9":
            return show_validation(
                self, "VP9 wird von NVIDIA NVENC nicht unterstützt. Bitte AV1 wählen oder CPU / Software verwenden."
            )
        specs = [(*segment, name) for segment, name in zip(segments, names)]

        def runner(progress, cancel):
            service = VideoService()
            results = []
            total = len(specs)
            for index, (start, end, output_name) in enumerate(specs):
                def item_progress(value, message, item_index=index):
                    overall = int((item_index + value / 100) / total * 100)
                    progress(overall, f"Segment {item_index + 1}/{total}: {message}")

                if video_options is not None:
                    result = service.compress_one(
                        source,
                        output,
                        video_options,
                        item_progress,
                        cancel,
                        start_seconds=start,
                        end_seconds=end,
                        output_name=output_name,
                    )
                else:
                    result = service.cut_segment(
                        source,
                        output,
                        CutOptions(
                            start_seconds=start,
                            end_seconds=end,
                            output_name=output_name,
                            cpu_limit_percent=cpu_limit_percent,
                            cpu_mode=cpu_mode,
                        ),
                        item_progress,
                        cancel,
                    )
                results.append(result)
            progress(100, f"{len(results)} Segment(e) fertig")
            return results

        payload = {
            "cut_mode": self._cut_mode_key(),
            "segments": [
                {"start": start, "end": end, "output_name": name}
                for start, end, name in specs
            ],
            "compressed": compress,
            "cpu_limit_percent": cpu_limit_percent,
            "cpu_mode": cpu_mode,
        }
        if self._cut_mode_key() == "markers":
            payload.update(
                {
                    "processing_start": self.cut_start.value(),
                    "processing_end": self.cut_end.value(),
                    "cut_markers": self._active_cut_markers(),
                }
            )
        if video_options is not None:
            payload.update(
                {
                    "codec": video_options.codec,
                    "container": video_options.container,
                    "crf": video_options.crf,
                    "cpu_limit_percent": video_options.cpu_limit_percent,
                    "cpu_mode": video_options.cpu_mode,
                    "encoder_backend": video_options.encoder_backend,
                    "nvenc_mode": video_options.nvenc_mode,
                }
            )
        self.jobs.submit(
            JobKind.VIDEO,
            f"{len(specs)} Video-Segment(e) exportieren · {self.cut_name.text().strip() or source.stem}",
            str(source),
            str(output),
            payload,
            runner,
        )

    def submit(self) -> None:
        sources = self.files.paths()
        if not sources:
            return show_validation(self, "Bitte mindestens ein Video auswählen.")
        output = Path(self.video_output.text()).expanduser()
        options = self._video_options()
        if options.encoder_backend == "nvenc" and options.codec == "vp9":
            return show_validation(
                self, "VP9 wird von NVIDIA NVENC nicht unterstützt. Bitte AV1 wählen oder CPU / Software verwenden."
            )

        dual_export = self.hdr_dual_export.isChecked()
        hdr_codec = str(self.hdr_codec.currentData() or "h265")
        tone_map = str(self.hdr_tone_map.currentData() or "hable")

        def runner(progress, cancel):
            service = VideoService()
            if dual_export:
                return service.compress_many_hdr_sdr(
                    sources,
                    output,
                    options,
                    hdr_codec=hdr_codec,
                    tone_map=tone_map,
                    progress=progress,
                    cancel=cancel,
                )
            return service.compress_many(sources, output, options, progress, cancel)

        payload = {
            "codec": options.codec,
            "container": options.container,
            "crf": options.crf,
            "cpu_limit_percent": options.cpu_limit_percent,
            "cpu_mode": options.cpu_mode,
            "encoder_backend": options.encoder_backend,
            "nvenc_mode": options.nvenc_mode,
        }
        if dual_export:
            payload.update(
                {
                    "hdr_sdr_dual_export": True,
                    "hdr_codec": hdr_codec,
                    "tone_map": tone_map,
                }
            )
        self.jobs.submit(
            JobKind.VIDEO,
            (
                f"{len(sources)} HDR-Video(s) als HDR + SDR komprimieren"
                if dual_export
                else f"{len(sources)} Video(s) komprimieren"
            ),
            f"{len(sources)} Dateien",
            str(output),
            payload,
            runner,
        )

    def submit_gif(self) -> None:
        sources = self.gif_file.paths()
        if not sources:
            return show_validation(self, "Bitte genau ein Video für das GIF auswählen.")
        if self.gif_end.value() <= self.gif_start.value():
            return show_validation(self, "Das GIF-Ende muss nach dem Start liegen.")
        source = sources[0]
        output = Path(self.gif_output.text()).expanduser()
        options = GifOptions(
            start_seconds=self.gif_start.value(),
            end_seconds=self.gif_end.value(),
            fps=self.gif_fps.value(),
            width=self.gif_width.value(),
            colors=int(self.gif_colors.currentData()),
            cpu_limit_percent=self.cpu_limit.value(),
            cpu_mode=str(self.cpu_mode.currentData() or "all"),
        )

        def runner(progress, cancel):
            return [VideoService().segment_to_gif(source, output, options, progress, cancel)]

        self.jobs.submit(
            JobKind.VIDEO,
            "Video-Segment als GIF",
            str(source),
            str(output),
            {
                "start": options.start_seconds,
                "end": options.end_seconds,
                "fps": options.fps,
                "width": options.width,
                "cpu_limit_percent": options.cpu_limit_percent,
                "cpu_mode": options.cpu_mode,
            },
            runner,
        )


class PdfPage(BasePage):
    PDF_EXTENSIONS = {".pdf"}

    def __init__(
        self, settings: AppSettings, jobs: JobManager, parent: QWidget | None = None
    ) -> None:
        super().__init__(
            "PDFs",
            "Dokumente lokal zusammenführen, trennen, extrahieren, drehen, komprimieren oder schützen.",
            parent,
        )
        self.settings = settings
        self.jobs = jobs
        card = Card()
        self.files = FileDropList("PDF-Dateien (*.pdf)", self.PDF_EXTENSIONS)
        card.layout.addWidget(self.files)
        form = QFormLayout()
        form.setSpacing(12)
        self.pdf_action = QComboBox()
        for label, value in (
            ("PDFs zusammenführen", "merge"),
            ("Jede Seite einzeln trennen", "split_each"),
            ("Nach Bereichen trennen", "split_groups"),
            ("Seiten extrahieren", "extract"),
            ("Seiten drehen", "rotate"),
            ("Verlustfrei komprimieren", "compress"),
            ("Mit Passwort schützen", "protect"),
            ("Mit bekanntem Passwort entsperren", "unlock"),
            ("Dokument-Metadaten bearbeiten", "metadata"),
        ):
            self.pdf_action.addItem(label, value)
        self.pdf_output = PathPicker(settings.pdf_dir)
        self.pages = QLineEdit()
        self.pages.setPlaceholderText("z. B. 1-3,5 oder Gruppen 1-3;4-6")
        self.rotation = QComboBox()
        self.rotation.addItem("90° rechts", 90)
        self.rotation.addItem("180°", 180)
        self.rotation.addItem("90° links", 270)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("Passwort wird nicht gespeichert")
        self.pdf_title = QLineEdit()
        self.pdf_author = QLineEdit()
        self.pdf_subject = QLineEdit()
        self.pdf_keywords = QLineEdit()
        add_form_row(form, "Aktion", self.pdf_action)
        add_form_row(form, "Ausgabeordner", self.pdf_output)
        add_form_row(form, "Seiten/Bereiche", self.pages)
        add_form_row(form, "Drehung", self.rotation)
        add_form_row(form, "Passwort", self.password)
        add_form_row(form, "PDF-Titel", self.pdf_title)
        add_form_row(form, "Autor", self.pdf_author)
        add_form_row(form, "Thema", self.pdf_subject)
        add_form_row(form, "Stichwörter", self.pdf_keywords)
        card.layout.addLayout(form)
        self.remove_pdf_metadata = QCheckBox("Metadaten bei Kompression entfernen")
        self.remove_pdf_metadata.setChecked(True)
        card.layout.addWidget(self.remove_pdf_metadata)
        card.layout.addWidget(
            muted(
                "Mehrere Trenn-Gruppen mit Semikolon angeben, etwa 1-3;4-6;7. Quelldateien werden nie überschrieben."
            )
        )
        row = QHBoxLayout()
        row.addStretch()
        action = primary_button("PDF-Job starten")
        action.clicked.connect(self.submit)
        row.addWidget(action)
        card.layout.addLayout(row)
        self.body.addWidget(card)
        self.pdf_action.currentIndexChanged.connect(self._update_controls)
        self._update_controls()
        self.finish()

    def _update_controls(self) -> None:
        action = self.pdf_action.currentData()
        self.pages.setEnabled(action in {"split_groups", "extract", "rotate"})
        self.rotation.setEnabled(action == "rotate")
        self.password.setEnabled(action in {"protect", "unlock"})
        self.remove_pdf_metadata.setEnabled(action == "compress")
        for widget in (self.pdf_title, self.pdf_author, self.pdf_subject, self.pdf_keywords):
            widget.setEnabled(action == "metadata")

    def submit(self) -> None:
        sources = self.files.paths()
        if not sources:
            return show_validation(self, "Bitte mindestens eine PDF-Datei auswählen.")
        action = self.pdf_action.currentData()
        if action == "merge" and len(sources) < 2:
            return show_validation(self, "Zum Zusammenführen werden mindestens zwei PDFs benötigt.")
        if action != "merge" and len(sources) != 1:
            return show_validation(self, "Für diese Aktion bitte genau eine PDF-Datei auswählen.")
        if action in {"split_groups", "extract"} and not self.pages.text().strip():
            return show_validation(self, "Bitte Seiten beziehungsweise Gruppen angeben.")
        if action in {"protect", "unlock"} and not self.password.text():
            return show_validation(self, "Bitte das Passwort eingeben.")
        output_dir = Path(self.pdf_output.text()).expanduser()
        pages = self.pages.text().strip()
        password = self.password.text()
        degrees = int(self.rotation.currentData())
        remove_metadata = self.remove_pdf_metadata.isChecked()
        pdf_metadata = {
            "title": self.pdf_title.text().strip(),
            "author": self.pdf_author.text().strip(),
            "subject": self.pdf_subject.text().strip(),
            "keywords": self.pdf_keywords.text().strip(),
        }

        def runner(progress, cancel):
            service = PdfService()
            source = sources[0]
            if action == "merge":
                return service.merge(sources, output_dir / "zusammengefuehrt.pdf", progress, cancel)
            if action == "split_each":
                return service.split(source, output_dir, "", progress, cancel)
            if action == "split_groups":
                return service.split(source, output_dir, pages, progress, cancel)
            if action == "extract":
                return service.extract(
                    source, output_dir / f"{source.stem}_auszug.pdf", pages, progress, cancel
                )
            if action == "rotate":
                return service.rotate(
                    source,
                    output_dir / f"{source.stem}_gedreht.pdf",
                    degrees,
                    pages,
                    progress,
                    cancel,
                )
            if action == "compress":
                return service.compress(
                    source,
                    output_dir / f"{source.stem}_optimiert.pdf",
                    remove_metadata,
                    progress,
                    cancel,
                )
            if action == "protect":
                return service.protect(
                    source, output_dir / f"{source.stem}_geschuetzt.pdf", password, progress, cancel
                )
            if action == "unlock":
                return service.unlock(
                    source, output_dir / f"{source.stem}_entsperrt.pdf", password, progress, cancel
                )
            return service.set_metadata(
                source,
                output_dir / f"{source.stem}_metadaten.pdf",
                progress=progress,
                cancel=cancel,
                **pdf_metadata,
            )

        label = self.pdf_action.currentText()
        self.jobs.submit(
            JobKind.PDF,
            label,
            f"{len(sources)} PDF(s)",
            str(output_dir),
            {"action": action, "pages": pages},
            runner,
        )
        self.password.clear()


class QueuePage(BasePage):
    def __init__(self, jobs: JobManager, parent: QWidget | None = None) -> None:
        super().__init__(
            "Queue",
            "Aktive Aufgaben laufen außerhalb des UI-Threads und lassen sich einzeln abbrechen.",
            parent,
        )
        self.jobs = jobs
        self.rows: dict[str, int] = {}
        card = Card(padding=1)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Typ", "Aufgabe", "Fortschritt", "Status", "Details", ""]
        )
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(
            5, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        card.layout.addWidget(self.table)
        self.body.addWidget(card)
        self.empty = muted("Noch keine Aufgaben in dieser Sitzung.")
        self.body.addWidget(self.empty)
        self.jobs.job_added.connect(self.add_job)
        self.jobs.job_updated.connect(self.update_job)
        self.finish()

    def add_job(self, job: JobRecord) -> None:
        if job.id in self.rows:
            return
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.rows[job.id] = row
        self.table.setItem(row, 0, QTableWidgetItem(job.kind.value.upper()))
        self.table.setItem(row, 1, QTableWidgetItem(job.label))
        progress = QProgressBar()
        progress.setRange(0, 100)
        progress.setValue(job.progress)
        progress.setFixedWidth(130)
        self.table.setCellWidget(row, 2, progress)
        self.table.setItem(row, 3, QTableWidgetItem(job.status.value))
        self.table.setItem(row, 4, QTableWidgetItem(job.message))
        cancel = QPushButton("Abbrechen")
        cancel.setObjectName("Danger")
        cancel.clicked.connect(lambda _checked=False, job_id=job.id: self.jobs.cancel(job_id))
        self.table.setCellWidget(row, 5, cancel)
        self.empty.setVisible(False)

    def update_job(self, job: JobRecord) -> None:
        if job.id not in self.rows:
            self.add_job(job)
        row = self.rows[job.id]
        progress = self.table.cellWidget(row, 2)
        if isinstance(progress, QProgressBar):
            progress.setValue(job.progress)
        self.table.item(row, 3).setText(job.status.value)
        detail = job.error if job.status == JobStatus.FAILED else job.message
        self.table.item(row, 4).setText(detail[:180])
        self.table.item(row, 4).setToolTip(detail)
        button = self.table.cellWidget(row, 5)
        if isinstance(button, QPushButton):
            button.setEnabled(job.status in {JobStatus.QUEUED, JobStatus.RUNNING})


class HistoryPage(BasePage):
    def __init__(self, database: HistoryDatabase, parent: QWidget | None = None) -> None:
        super().__init__(
            "Verlauf",
            "Ergebnisse und Fehler früherer Aufgaben. Adressen werden nur lokal in SQLite gespeichert.",
            parent,
        )
        self.database = database
        actions = QHBoxLayout()
        refresh = QPushButton("Aktualisieren")
        refresh.clicked.connect(self.reload)
        open_output = QPushButton("Ausgabe öffnen")
        open_output.clicked.connect(self.open_selected)
        clear = QPushButton("Fertige Einträge löschen")
        clear.setObjectName("Danger")
        clear.clicked.connect(self.clear_finished)
        actions.addWidget(refresh)
        actions.addWidget(open_output)
        actions.addStretch()
        actions.addWidget(clear)
        self.body.addLayout(actions)
        card = Card(padding=1)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["Zeit", "Typ", "Aufgabe", "Status", "Ausgabe / Fehler"]
        )
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        card.layout.addWidget(self.table)
        self.body.addWidget(card)
        self.finish()
        self.reload()

    def reload(self) -> None:
        self.table.setRowCount(0)
        for job in self.database.recent():
            row = self.table.rowCount()
            self.table.insertRow(row)
            timestamp = job.updated_at.replace("T", " ")[:16]
            detail = job.outputs[0] if job.outputs else job.error or job.message
            values = (timestamp, job.kind.value.upper(), job.label, job.status.value, detail)
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value))
                item.setData(Qt.ItemDataRole.UserRole, job.outputs)
                self.table.setItem(row, column, item)

    def open_selected(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return show_validation(self, "Bitte zuerst einen Eintrag auswählen.")
        outputs = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) or []
        if not outputs:
            return show_validation(self, "Dieser Eintrag besitzt keine Ausgabedatei.")
        path = Path(outputs[0])
        target = path if path.is_dir() else path.parent
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def clear_finished(self) -> None:
        answer = QMessageBox.question(
            self,
            "Verlauf leeren",
            "Alle abgeschlossenen, fehlgeschlagenen und abgebrochenen Einträge löschen?",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.database.clear_finished()
            self.reload()


class SettingsPage(BasePage):
    settings_saved = Signal(object)
    update_check_requested = Signal(bool)

    UPDATE_MODES = (
        ("Aus", "off"),
        ("Nur prüfen", "check"),
        ("Code automatisch aktualisieren und neu bauen", "code"),
        ("Code + Pakete automatisch aktualisieren und neu bauen", "code_and_packages"),
    )

    def __init__(
        self,
        settings: AppSettings,
        store: SettingsStore,
        paths: AppPaths,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            "Einstellungen",
            "Ausgabeorte, Darstellung, Parallelität, Diagnose und Git-basierte Updates.",
            parent,
        )
        self.settings = settings
        self.store = store
        self.paths = paths
        general = Card()
        general.layout.addWidget(section_title("Allgemein"))
        form = QFormLayout()
        self.theme = QComboBox()
        self.theme.addItem("Dunkel", "dark")
        self.theme.addItem("Hell", "light")
        self.theme.setCurrentIndex(max(0, self.theme.findData(settings.theme)))
        self.parallel = QSpinBox()
        self.parallel.setRange(1, 8)
        self.parallel.setValue(settings.parallel_jobs)
        add_form_row(form, "Darstellung", self.theme)
        add_form_row(form, "Parallele Jobs", self.parallel)
        general.layout.addLayout(form)
        self.body.addWidget(general)
        folders = Card()
        folders.layout.addWidget(section_title("Ausgabeordner"))
        folder_form = QFormLayout()
        self.download_dir = PathPicker(settings.download_dir)
        self.image_dir = PathPicker(settings.image_dir)
        self.video_dir = PathPicker(settings.video_dir)
        self.pdf_dir = PathPicker(settings.pdf_dir)
        self.transcription_dir = PathPicker(settings.transcription_dir)
        self.privacy_dir = PathPicker(settings.privacy_dir)
        self.vault_dir = PathPicker(settings.vault_dir)
        self.ocr_dir = PathPicker(settings.ocr_dir)
        self.upscale_dir = PathPicker(settings.upscale_dir)
        add_form_row(folder_form, "Downloads/Musik", self.download_dir)
        add_form_row(folder_form, "Bilder", self.image_dir)
        add_form_row(folder_form, "Videos", self.video_dir)
        add_form_row(folder_form, "PDFs", self.pdf_dir)
        add_form_row(folder_form, "Transkripte", self.transcription_dir)
        add_form_row(folder_form, "Deep Clean", self.privacy_dir)
        add_form_row(folder_form, "Vault", self.vault_dir)
        add_form_row(folder_form, "OCR", self.ocr_dir)
        add_form_row(folder_form, "Upscaling", self.upscale_dir)
        folders.layout.addLayout(folder_form)
        self.body.addWidget(folders)
        updates = Card()
        updates.layout.addWidget(section_title("Updates"))
        self.update_mode = QComboBox()
        for label, value in self.UPDATE_MODES:
            self.update_mode.addItem(label, value)
        self.update_mode.setCurrentIndex(max(0, self.update_mode.findData(settings.update_mode)))
        self.remote = QLineEdit(settings.update_remote)
        self.remote.setPlaceholderText("origin/main")
        self.interval = QSpinBox()
        self.interval.setRange(1, 168)
        self.interval.setValue(settings.update_interval_hours)
        self.interval.setSuffix(" Stunden")
        self.update_status = QLabel("Noch nicht geprüft")
        self.update_status.setObjectName("Muted")

        if is_frozen():
            self.update_mode.setCurrentIndex(max(0, self.update_mode.findData("off")))
            self.update_mode.setEnabled(False)
            self.remote.setEnabled(False)
            self.interval.setEnabled(False)
            self.update_status.setText("EXE-Version: Updates über neue GitHub-Releases")
            updates.layout.addWidget(
                muted(
                    "Die fertige EXE verändert ihren eigenen Programmcode nicht. "
                    "Für eine neue Version wird die neue AIO-Media-Tool.exe heruntergeladen."
                )
            )
        else:
            update_form = QFormLayout()
            add_form_row(update_form, "Modus", self.update_mode)
            add_form_row(update_form, "Remote-Branch", self.remote)
            add_form_row(update_form, "Prüfintervall", self.interval)
            updates.layout.addLayout(update_form)
            updates.layout.addWidget(
                muted(
                    "Updates sind standardmäßig aus. Bei aktivierten automatischen Modi wird nur mit sauberem Git-Stand aktualisiert. Verwende dafür nur einen Remote, dem du vertraust."
                )
            )
            update_actions = QHBoxLayout()
            check = QPushButton("Jetzt prüfen")
            check.clicked.connect(lambda: self.update_check_requested.emit(True))
            update_actions.addWidget(self.update_status, 1)
            update_actions.addWidget(check)
            updates.layout.addLayout(update_actions)
        self.body.addWidget(updates)
        tools = Card()
        tools.layout.addWidget(section_title("Systemwerkzeuge"))
        self.tools_table = QTableWidget(0, 3)
        self.tools_table.setHorizontalHeaderLabels(["Werkzeug", "Status", "Version / Hinweis"])
        self.tools_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.tools_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.tools_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.tools_table.verticalHeader().setVisible(False)
        self.tools_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tools.layout.addWidget(self.tools_table)
        tool_actions = QHBoxLayout()
        refresh_tools = QPushButton("Werkzeuge neu prüfen")
        refresh_tools.clicked.connect(self.refresh_tools)
        diagnostics = QPushButton("Diagnosepaket erstellen")
        diagnostics.clicked.connect(self.create_diagnostics)
        tool_actions.addWidget(refresh_tools)
        tool_actions.addWidget(diagnostics)
        tool_actions.addStretch()
        tools.layout.addLayout(tool_actions)
        self.body.addWidget(tools)
        save_row = QHBoxLayout()
        save_row.addStretch()
        save = primary_button("Einstellungen speichern")
        save.clicked.connect(self.save)
        save_row.addWidget(save)
        self.body.addLayout(save_row)
        self.finish()
        self.refresh_tools()

    def save(self) -> None:
        values = (
            self.download_dir.text(),
            self.image_dir.text(),
            self.video_dir.text(),
            self.pdf_dir.text(),
            self.transcription_dir.text(),
            self.privacy_dir.text(),
            self.vault_dir.text(),
            self.ocr_dir.text(),
            self.upscale_dir.text(),
        )
        if not all(values):
            return show_validation(self, "Bitte alle Ausgabeordner angeben.")
        if not is_frozen():
            try:
                UpdaterService.split_remote_ref(self.remote.text())
            except ValueError as exc:
                return show_validation(self, str(exc))
        self.settings.theme = self.theme.currentData()
        self.settings.parallel_jobs = self.parallel.value()
        (
            self.settings.download_dir,
            self.settings.image_dir,
            self.settings.video_dir,
            self.settings.pdf_dir,
            self.settings.transcription_dir,
            self.settings.privacy_dir,
            self.settings.vault_dir,
            self.settings.ocr_dir,
            self.settings.upscale_dir,
        ) = values
        if is_frozen():
            self.settings.update_mode = "off"
        else:
            self.settings.update_mode = self.update_mode.currentData()
            self.settings.update_remote = self.remote.text().strip()
            self.settings.update_interval_hours = self.interval.value()
        self.settings.ensure_output_dirs()
        self.store.save(self.settings)
        self.settings_saved.emit(self.settings)
        QMessageBox.information(self, "Gespeichert", "Die Einstellungen wurden gespeichert.")

    def refresh_tools(self) -> None:
        statuses = collect_tool_status()
        self.tools_table.setRowCount(len(statuses))
        for row, status in enumerate(statuses):
            values = (
                status.name,
                "Bereit" if status.available else "Fehlt",
                status.version if status.available else status.note,
            )
            for column, value in enumerate(values):
                self.tools_table.setItem(row, column, QTableWidgetItem(value))

    def create_diagnostics(self) -> None:
        try:
            output = create_diagnostic_bundle(
                Path(self.settings.download_dir), self.settings, self.paths.logs
            )
        except Exception as exc:
            QMessageBox.critical(self, "Diagnose fehlgeschlagen", str(exc))
            return
        QMessageBox.information(self, "Diagnose erstellt", f"Gespeichert als:\n{output}")

    def set_update_status(self, text: str) -> None:
        self.update_status.setText(text)
