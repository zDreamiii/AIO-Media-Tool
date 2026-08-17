from __future__ import annotations

import json
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter

from aio_media_tool.services import transcription as transcription_module
from aio_media_tool.services.metadata_cleaner import MetadataCleanerService
from aio_media_tool.services.ocr import OCRService
from aio_media_tool.services.snippets import SnippetDatabase
from aio_media_tool.services.transcription import (
    SubtitleSegment,
    TranscriptionOptions,
    TranscriptionService,
)
from aio_media_tool.services.upscaler import UpscaleOptions, UpscalerService
from aio_media_tool.services.vault import VaultPasswordError, VaultService


def test_whisper_availability_probe_does_not_import_backend(monkeypatch) -> None:
    sys.modules.pop("faster_whisper", None)
    monkeypatch.setattr(transcription_module, "find_spec", lambda name: object())
    assert TranscriptionService.available() == (
        True,
        "faster-whisper ist installiert und noch nicht geladen",
    )
    assert "faster_whisper" not in sys.modules


def test_subtitle_rendering_and_mocked_transcription(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "speech.wav"
    source.write_bytes(b"fake audio for mocked backend")

    class Segment:
        def __init__(self, start, end, text):
            self.start, self.end, self.text = start, end, text

    class Info:
        duration = 3.0
        language = "de"

    class Model:
        def __init__(self, *_args, **kwargs):
            assert kwargs["local_files_only"] is True

        def transcribe(self, *_args, **_kwargs):
            return iter([Segment(0, 1.25, " Hallo "), Segment(1.5, 2.9, "Welt")]), Info()

    class Backend:
        WhisperModel = Model

    monkeypatch.setattr(TranscriptionService, "_backend", staticmethod(lambda: Backend))
    monkeypatch.setattr(
        TranscriptionService, "_device_and_compute", staticmethod(lambda _device: ("cpu", "int8"))
    )
    outputs = TranscriptionService().transcribe(
        source,
        tmp_path / "out",
        tmp_path / "models",
        TranscriptionOptions(model="tiny", write_srt=True, write_vtt=True),
    )
    assert [path.suffix for path in outputs] == [".srt", ".vtt"]
    assert "00:00:01,250" in outputs[0].read_text(encoding="utf-8")
    assert outputs[1].read_text(encoding="utf-8").startswith("WEBVTT")
    assert "Hallo" in TranscriptionService.render_srt([SubtitleSegment(0, 1, "Hallo")])


def test_metadata_cleaner_copy_mode_strips_image_and_pdf(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    photo = source / "photo.jpg"
    exif = Image.Exif()
    exif[0x010F] = "Secret Camera"
    exif[0x0131] = "Private Software"
    Image.new("RGB", (80, 60), "purple").save(photo, exif=exif)
    pdf = source / "document.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_metadata({"/Author": "Private Author", "/Creator": "Private App"})
    with pdf.open("wb") as stream:
        writer.write(stream)

    outputs = MetadataCleanerService(exiftool_path="").clean_batch(
        source, "copy", tmp_path / "clean"
    )
    clean_photo = next(path for path in outputs if path.name == "photo.jpg")
    clean_pdf = next(path for path in outputs if path.name == "document.pdf")
    with Image.open(clean_photo) as opened:
        assert not opened.getexif()
    assert not (PdfReader(clean_pdf).metadata or {}).get("/Author")
    log = next(path for path in outputs if path.suffix == ".json")
    payload = json.loads(log.read_text(encoding="utf-8"))
    assert len(payload["results"]) == 2
    assert photo.read_bytes() != clean_photo.read_bytes()


def test_vault_roundtrip_wrong_password_and_private_staging(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "note.txt").write_text("very private", encoding="utf-8")
    nested = source / "nested"
    nested.mkdir()
    (nested / "data.bin").write_bytes(b"x" * 4096)
    vault = VaultService().encrypt([source], tmp_path / "archive.aio_enc", "Correct-Horse-42!")[0]
    assert b"very private" not in vault.read_bytes()

    with pytest.raises(VaultPasswordError, match="Falsches Passwort"):
        VaultService().decrypt(
            vault, tmp_path / "wrong", "Definitely-Wrong-42!", tmp_path / "private-temp"
        )
    assert (
        not list((tmp_path / "private-temp").glob("vault-decrypt-*.zip"))
        if (tmp_path / "private-temp").exists()
        else True
    )

    monkeypatch.setattr("aio_media_tool.services.vault.MEMORY_DECRYPT_LIMIT", 1)
    restored_root = VaultService().decrypt(
        vault, tmp_path / "restored", "Correct-Horse-42!", tmp_path / "private-temp"
    )[0]
    assert (restored_root / "source" / "note.txt").read_text(encoding="utf-8") == "very private"
    assert (restored_root / "source" / "nested" / "data.bin").read_bytes() == b"x" * 4096
    assert not list((tmp_path / "private-temp").glob("vault-decrypt-*.zip"))


def test_snippet_database_search_dedup_and_retention(tmp_path: Path) -> None:
    database = SnippetDatabase(tmp_path / "snippets.sqlite3")
    first = database.add_text("  Hallo Clipboard  ", "Editor")
    assert first is not None
    assert database.add_text("  Hallo Clipboard  ", "Editor") is None
    image_id = database.add_image(b"fake-png", "Bild 1 × 1", "Screenshot")
    assert image_id is not None
    assert database.recent(100, "Clipboard")[0].text_content.strip() == "Hallo Clipboard"
    with database._connection() as connection:
        connection.execute(
            f"UPDATE {database.TABLE} SET created_at = ? WHERE id = ?",
            ((datetime.now(UTC) - timedelta(hours=48)).isoformat(), first),
        )
    assert database.delete_older_than(24) == 1
    assert len(database.recent()) == 1


def test_ocr_pipeline_and_exports_without_external_engine(tmp_path: Path, monkeypatch) -> None:
    image = tmp_path / "scan.png"
    Image.new("RGB", (40, 30), "white").save(image)
    monkeypatch.setattr(
        OCRService,
        "_tesseract",
        staticmethod(lambda _executable, _path, languages, _cancel: f"Text {languages}"),
    )
    result = OCRService().recognize(
        image,
        engine="tesseract",
        languages="deu+eng",
        tesseract_path="mock-tesseract",
        private_temp=tmp_path / "private",
    )
    assert result.text == "Text deu+eng"
    txt = OCRService.export_txt(result.text, "Translated", tmp_path / "export")
    assert "ÜBERSETZUNG" in txt.read_text(encoding="utf-8")
    docx = OCRService.export_docx(result.text, "Translated", tmp_path / "export")
    assert docx.suffix == ".docx" and docx.stat().st_size > 0


def test_upscale_image_command_is_atomic(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "small.png"
    Image.new("RGB", (20, 20), "orange").save(source)

    def fake_run(command, _output_dir, _expected, _start, _end, _progress, _cancel):
        shutil.copyfile(
            Path(command[command.index("-i") + 1]), Path(command[command.index("-o") + 1])
        )

    monkeypatch.setattr(UpscalerService, "_run_tool", staticmethod(fake_run))
    output = UpscalerService().upscale_image(
        source,
        tmp_path / "out",
        UpscaleOptions(scale=2),
        "mock-realesrgan",
    )
    assert output.name == "small_2x.png"
    with Image.open(output) as opened:
        assert opened.size == (20, 20)
