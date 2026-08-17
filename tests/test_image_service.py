from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from aio_media_tool.services.images import ImageOptions, ImageService


class ImageServiceTests(unittest.TestCase):
    def test_resize_and_convert_without_touching_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.png"
            Image.new("RGBA", (1200, 800), (90, 120, 210, 180)).save(source)
            before = source.read_bytes()
            result = ImageService().process_one(
                source,
                root / "out",
                ImageOptions(output_format="JPEG", quality=75, max_width=600, max_height=600),
            )
            self.assertTrue(result.exists())
            self.assertEqual(source.read_bytes(), before)
            with Image.open(result) as image:
                self.assertEqual(image.format, "JPEG")
                self.assertLessEqual(image.width, 600)
                self.assertLessEqual(image.height, 600)

    def test_existing_output_gets_unique_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "square.png"
            Image.new("RGB", (100, 100), "red").save(source)
            options = ImageOptions(output_format="PNG")
            first = ImageService().process_one(source, root / "out", options)
            second = ImageService().process_one(source, root / "out", options)
            self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
