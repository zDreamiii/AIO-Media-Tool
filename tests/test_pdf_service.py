from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from aio_media_tool.services.pdfs import PdfService


def make_pdf(path: Path, pages: int) -> None:
    writer = PdfWriter()
    for index in range(pages):
        writer.add_blank_page(width=200 + index, height=300 + index)
    with path.open("wb") as stream:
        writer.write(stream)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PdfServiceTests(unittest.TestCase):
    def test_merge_split_extract_rotate_and_source_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / "a.pdf", root / "b.pdf"
            make_pdf(first, 2)
            make_pdf(second, 3)
            before = {first: digest(first), second: digest(second)}
            service = PdfService()
            merged = service.merge([first, second], root / "out" / "merged.pdf")[0]
            self.assertEqual(len(PdfReader(merged).pages), 5)
            pieces = service.split(merged, root / "split", "1-2;3-5")
            self.assertEqual([len(PdfReader(path).pages) for path in pieces], [2, 3])
            extract = service.extract(merged, root / "extract.pdf", "2,4")[0]
            self.assertEqual(len(PdfReader(extract).pages), 2)
            rotated = service.rotate(merged, root / "rotated.pdf", 90, "1")[0]
            self.assertEqual(PdfReader(rotated).pages[0].rotation, 90)
            self.assertEqual({path: digest(path) for path in before}, before)

    def test_compress_protect_and_unlock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            make_pdf(source, 2)
            service = PdfService()
            compressed = service.compress(source, root / "compressed.pdf")[0]
            self.assertEqual(len(PdfReader(compressed).pages), 2)
            protected = service.protect(source, root / "protected.pdf", "secret42")[0]
            self.assertTrue(PdfReader(protected).is_encrypted)
            unlocked = service.unlock(protected, root / "unlocked.pdf", "secret42")[0]
            self.assertFalse(PdfReader(unlocked).is_encrypted)
            self.assertEqual(len(PdfReader(unlocked).pages), 2)
            tagged = service.set_metadata(
                source, root / "tagged.pdf", title="Mein Dokument", author="AIO"
            )[0]
            self.assertEqual(PdfReader(tagged).metadata.title, "Mein Dokument")
            self.assertEqual(PdfReader(tagged).metadata.author, "AIO")


if __name__ == "__main__":
    unittest.main()
