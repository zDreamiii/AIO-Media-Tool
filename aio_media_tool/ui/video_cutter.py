from __future__ import annotations

from bisect import bisect_left
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QUrl, Signal, Slot
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSlider, QSpinBox, QVBoxLayout, QWidget

from aio_media_tool.services.video import VideoService


class FrameIndexSignals(QObject):
    completed = Signal(object)
    failed = Signal(object)


class FrameIndexWorker(QRunnable):
    def __init__(self, source: Path) -> None:
        super().__init__()
        self.source = source
        self.signals = FrameIndexSignals()

    @Slot()
    def run(self) -> None:
        try:
            timestamps = VideoService().frame_timestamps(self.source)
            self.signals.completed.emit((self.source, timestamps))
        except Exception as exc:
            self.signals.failed.emit((self.source, str(exc).strip() or type(exc).__name__))


class VideoPreview(QWidget):
    """Local video player with timeline and frame-aware cutter navigation."""

    duration_changed = Signal(float)
    position_changed = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._dragging = False
        self._frame_dragging = False
        self._duration_ms = 0
        self._fps = 0.0
        self._frame_timestamps: list[float] = []
        self._source: Path | None = None
        self._frame_worker: FrameIndexWorker | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.video = QVideoWidget()
        self.video.setMinimumHeight(330)
        self.video.setStyleSheet("background: #050608; border-radius: 9px;")
        layout.addWidget(self.video)

        self.timeline = QSlider(Qt.Orientation.Horizontal)
        self.timeline.setRange(0, 0)
        self.timeline.sliderPressed.connect(self._slider_pressed)
        self.timeline.sliderReleased.connect(self._slider_released)
        self.timeline.sliderMoved.connect(self._slider_moved)
        layout.addWidget(self.timeline)

        controls = QHBoxLayout()
        self.play_button = QPushButton("▶ Abspielen")
        self.play_button.clicked.connect(self.toggle_playback)
        controls.addWidget(self.play_button)
        for label, delta in (("−5 s", -5), ("−1 s", -1), ("+1 s", 1), ("+5 s", 5)):
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, value=delta: self.jump(value))
            controls.addWidget(button)
        controls.addStretch()
        self.time_label = QLabel("00:00.000 / 00:00.000")
        self.time_label.setObjectName("Muted")
        controls.addWidget(self.time_label)
        layout.addLayout(controls)

        frame_controls = QHBoxLayout()
        self.previous_frame_button = QPushButton("◀ 1 Frame")
        self.next_frame_button = QPushButton("1 Frame ▶")
        self.previous_frame_button.clicked.connect(lambda: self.step_frames(-1))
        self.next_frame_button.clicked.connect(lambda: self.step_frames(1))
        frame_controls.addWidget(self.previous_frame_button)
        frame_controls.addWidget(self.next_frame_button)
        self.frame_label = QLabel("Frame – · Zeit – · FPS –")
        self.frame_label.setObjectName("Muted")
        frame_controls.addWidget(self.frame_label)
        frame_controls.addStretch()
        layout.addLayout(frame_controls)

        frame_seek = QHBoxLayout()
        frame_seek.addWidget(QLabel("Frame-Timeline"))
        self.frame_timeline = QSlider(Qt.Orientation.Horizontal)
        self.frame_timeline.setRange(0, 0)
        self.frame_timeline.sliderPressed.connect(self._frame_slider_pressed)
        self.frame_timeline.sliderReleased.connect(self._frame_slider_released)
        self.frame_timeline.sliderMoved.connect(self._frame_slider_moved)
        frame_seek.addWidget(self.frame_timeline, 1)
        frame_seek.addWidget(QLabel("Frame-Nr."))
        self.frame_number = QSpinBox()
        self.frame_number.setRange(0, 0)
        self.frame_number.setKeyboardTracking(False)
        self.frame_number.valueChanged.connect(self.seek_frame)
        frame_seek.addWidget(self.frame_number)
        layout.addLayout(frame_seek)

        self.status = QLabel(
            "Video laden, dann über die Timeline oder frameweise zur gewünschten Stelle springen."
        )
        self.status.setObjectName("Muted")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(0.8)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.setVideoOutput(self.video)
        self.player.durationChanged.connect(self._duration_updated)
        self.player.positionChanged.connect(self._position_updated)
        self.player.playbackStateChanged.connect(self._playback_state_changed)
        self.player.errorOccurred.connect(self._player_error)
        self._refresh_frame_controls()

    @staticmethod
    def format_ms(milliseconds: int) -> str:
        milliseconds = max(0, int(milliseconds))
        hours, remainder = divmod(milliseconds, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        seconds, millis = divmod(remainder, 1000)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"
        return f"{minutes:02d}:{seconds:02d}.{millis:03d}"

    def load(self, source: Path | None, fps: float = 0.0) -> None:
        self.player.stop()
        self._source = source.resolve() if source is not None else None
        self._duration_ms = 0
        self._fps = max(0.0, float(fps))
        self._frame_timestamps = []
        self.timeline.setRange(0, 0)
        self.timeline.setValue(0)
        self.frame_timeline.setRange(0, 0)
        self.frame_timeline.setValue(0)
        self.frame_number.setRange(0, 0)
        self.frame_number.setValue(0)
        self._refresh_time(0)
        self._refresh_frame_controls()
        if source is None:
            self.player.setSource(QUrl())
            self.status.setText("Kein Video ausgewählt.")
            return
        self.status.setText(
            f"Vorschau: {source.name} · Frameindex wird im Hintergrund geladen …"
        )
        self.player.setSource(QUrl.fromLocalFile(str(source.resolve())))
        self.player.setPosition(0)
        self._load_frame_index(source.resolve())

    def set_frame_rate(self, fps: float) -> None:
        self._fps = max(0.0, float(fps))
        self._refresh_frame_controls()
        self._refresh_time(self.player.position())

    def position_seconds(self) -> float:
        return self.player.position() / 1000.0

    def selected_frame_seconds(self) -> float:
        if self._frame_timestamps:
            index = self.current_frame_index()
            if index >= 0:
                return self._frame_timestamps[index]
        if self._fps > 0:
            index = self.current_frame_index()
            return max(0, index) / self._fps
        return self.position_seconds()

    def duration_seconds(self) -> float:
        return self._duration_ms / 1000.0

    def current_frame_index(self) -> int:
        position = self.position_seconds()
        if self._frame_timestamps:
            index = bisect_left(self._frame_timestamps, position)
            if index >= len(self._frame_timestamps):
                return len(self._frame_timestamps) - 1
            if index > 0:
                before = self._frame_timestamps[index - 1]
                after = self._frame_timestamps[index]
                if abs(position - before) <= abs(after - position):
                    return index - 1
            return index
        if self._fps > 0:
            return max(0, int(round(position * self._fps)))
        return -1

    def seek_seconds(self, seconds: float) -> None:
        position = int(round(max(0.0, seconds) * 1000))
        if self._duration_ms:
            position = min(position, self._duration_ms)
        self.player.setPosition(position)

    def seek_frame(self, index: int) -> None:
        self.player.pause()
        if self._frame_timestamps:
            index = max(0, min(int(index), len(self._frame_timestamps) - 1))
            self.seek_seconds(self._frame_timestamps[index])
            return
        if self._fps > 0:
            max_index = int(self.duration_seconds() * self._fps) if self._duration_ms else int(index)
            index = max(0, min(int(index), max_index))
            self.seek_seconds(index / self._fps)

    def step_frames(self, delta: int) -> None:
        if not self._frame_timestamps and self._fps <= 0:
            return
        current = self.current_frame_index()
        self.seek_frame(max(0, current + int(delta)))

    def toggle_playback(self) -> None:
        if self.player.source().isEmpty():
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def jump(self, seconds: int) -> None:
        self.seek_seconds(self.position_seconds() + seconds)

    def _load_frame_index(self, source: Path) -> None:
        worker = FrameIndexWorker(source)
        self._frame_worker = worker
        worker.signals.completed.connect(self._frame_index_ready)
        worker.signals.failed.connect(self._frame_index_failed)
        QThreadPool.globalInstance().start(worker)

    @Slot(object)
    def _frame_index_ready(self, result: object) -> None:
        source, timestamps = result
        if self._source is None or Path(source).resolve() != self._source:
            return
        self._frame_timestamps = list(timestamps)
        self._refresh_frame_controls()
        if self._frame_timestamps:
            self.status.setText(
                f"Vorschau: {self._source.name} · {len(self._frame_timestamps):,} Frames indexiert. "
                "Mit ◀/▶ 1 Frame kannst du framegenau navigieren."
            )
        else:
            self.status.setText(
                f"Vorschau: {self._source.name} · Kein Frameindex gefunden; Navigation nutzt die Bildrate."
            )
        self._refresh_time(self.player.position())

    @Slot(object)
    def _frame_index_failed(self, result: object) -> None:
        source, message = result
        if self._source is None or Path(source).resolve() != self._source:
            return
        self.status.setText(
            f"Vorschau: {self._source.name} · Frameindex nicht verfügbar ({message}). "
            "Navigation nutzt ersatzweise die erkannte Bildrate."
        )
        self._refresh_frame_controls()

    def _duration_updated(self, milliseconds: int) -> None:
        self._duration_ms = max(0, int(milliseconds))
        self.timeline.setRange(0, self._duration_ms)
        self._refresh_frame_controls()
        self._refresh_time(self.player.position())
        self.duration_changed.emit(self.duration_seconds())

    def _position_updated(self, milliseconds: int) -> None:
        if not self._dragging:
            self.timeline.setValue(int(milliseconds))
        self._refresh_time(milliseconds)
        self.position_changed.emit(max(0, milliseconds) / 1000.0)

    def _refresh_time(self, position_ms: int) -> None:
        self.time_label.setText(
            f"{self.format_ms(position_ms)} / {self.format_ms(self._duration_ms)}"
        )
        frame = self.current_frame_index()
        if frame >= 0:
            timestamp = self.selected_frame_seconds()
            fps_text = f"{self._fps:.3f} fps" if self._fps > 0 else "VFR"
            exact = "exakter Zeitstempel" if self._frame_timestamps else "FPS-Näherung"
            self.frame_label.setText(
                f"Frame {frame:,} · {self.format_ms(round(timestamp * 1000))} · {fps_text} · {exact}"
            )
            if not self._frame_dragging:
                self.frame_timeline.blockSignals(True)
                self.frame_timeline.setValue(frame)
                self.frame_timeline.blockSignals(False)
            self.frame_number.blockSignals(True)
            self.frame_number.setValue(frame)
            self.frame_number.blockSignals(False)
        else:
            self.frame_label.setText("Frame – · Zeit – · FPS –")

    def _refresh_frame_controls(self) -> None:
        enabled = bool(self._frame_timestamps) or self._fps > 0
        self.previous_frame_button.setEnabled(enabled)
        self.next_frame_button.setEnabled(enabled)
        if self._frame_timestamps:
            maximum = max(0, len(self._frame_timestamps) - 1)
        elif self._fps > 0 and self._duration_ms:
            maximum = max(0, int(round(self.duration_seconds() * self._fps)) - 1)
        else:
            maximum = 0
        self.frame_timeline.setRange(0, maximum)
        self.frame_number.setRange(0, maximum)
        self.frame_timeline.setEnabled(enabled)
        self.frame_number.setEnabled(enabled)

    def _frame_slider_pressed(self) -> None:
        self._frame_dragging = True
        self.player.pause()

    def _frame_slider_moved(self, value: int) -> None:
        self.seek_frame(value)

    def _frame_slider_released(self) -> None:
        value = self.frame_timeline.value()
        self._frame_dragging = False
        self.seek_frame(value)

    def _slider_pressed(self) -> None:
        self._dragging = True

    def _slider_moved(self, value: int) -> None:
        self._refresh_time(value)

    def _slider_released(self) -> None:
        self._dragging = False
        self.player.setPosition(self.timeline.value())

    def _playback_state_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        self.play_button.setText(
            "⏸ Pause" if state == QMediaPlayer.PlaybackState.PlayingState else "▶ Abspielen"
        )

    def _player_error(self, _error: QMediaPlayer.Error, error_string: str) -> None:
        if error_string:
            self.status.setText(
                "Die Qt-Vorschau konnte das Video nicht abspielen: "
                f"{error_string}. Der FFmpeg-Export kann trotzdem funktionieren."
            )
