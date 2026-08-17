from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aio_media_tool.config import SettingsStore
from aio_media_tool.models import AppSettings
from aio_media_tool.services.common import (
    CpuSetEntry,
    cpu_limit_core_count,
    select_efficiency_cpu_set_ids,
    unique_output,
    validate_public_media_url,
)
from aio_media_tool.services.pdfs import parse_page_groups, parse_page_range
from aio_media_tool.services.updater import UpdaterService


class FoundationTests(unittest.TestCase):
    def test_page_ranges_are_one_based_and_unique(self) -> None:
        self.assertEqual(parse_page_range("1-3,3,5", 6), [0, 1, 2, 4])
        self.assertEqual(parse_page_groups("1-2;3,5", 5), [[0, 1], [2, 4]])
        with self.assertRaises(ValueError):
            parse_page_range("7", 6)

    def test_private_urls_are_rejected(self) -> None:
        self.assertEqual(
            validate_public_media_url("https://www.youtube.com/watch?v=test"),
            "https://www.youtube.com/watch?v=test",
        )
        for value in (
            "file:///tmp/a",
            "http://localhost/video",
            "http://127.0.0.1/a",
            "http://192.168.1.2/a",
        ):
            with self.assertRaises(ValueError, msg=value):
                validate_public_media_url(value)

    def test_cpu_limit_maps_to_logical_cores(self) -> None:
        self.assertEqual(cpu_limit_core_count(100, 16), 16)
        self.assertEqual(cpu_limit_core_count(75, 16), 12)
        self.assertEqual(cpu_limit_core_count(50, 16), 8)
        self.assertEqual(cpu_limit_core_count(25, 16), 4)
        self.assertEqual(cpu_limit_core_count(10, 8), 1)

    def test_efficiency_cpu_sets_select_only_lowest_class(self) -> None:
        # Synthetic 14900-style topology: P-core logical processors have the higher
        # EfficiencyClass, E-cores the lower one. CPU Set IDs need not match LP indexes.
        cpu_sets = [
            *(CpuSetEntry(100 + index, 0, index, index // 2, 1, 0) for index in range(16)),
            *(CpuSetEntry(200 + index, 0, 16 + index, 8 + index, 0, 0) for index in range(16)),
        ]
        self.assertEqual(
            select_efficiency_cpu_set_ids(cpu_sets, 100),
            [200 + index for index in range(16)],
        )
        self.assertEqual(
            select_efficiency_cpu_set_ids(cpu_sets, 50),
            [200 + index for index in range(8)],
        )

    def test_efficiency_cpu_sets_require_hybrid_classes(self) -> None:
        cpu_sets = [CpuSetEntry(index, 0, index, index, 0, 0) for index in range(8)]
        self.assertEqual(select_efficiency_cpu_set_ids(cpu_sets, 100), [])

    def test_efficiency_cpu_sets_skip_allocated_sets(self) -> None:
        cpu_sets = [
            CpuSetEntry(1, 0, 0, 0, 0, 0),
            CpuSetEntry(2, 0, 1, 1, 0, 0b10),
            CpuSetEntry(3, 0, 2, 2, 1, 0),
        ]
        self.assertEqual(select_efficiency_cpu_set_ids(cpu_sets, 100), [1])

    def test_unique_output_never_overwrites(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "result.pdf"
            original.write_bytes(b"old")
            self.assertEqual(unique_output(original).name, "result (2).pdf")

    def test_settings_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.json")
            settings = AppSettings(
                theme="light",
                parallel_jobs=4,
                download_dir=directory,
                image_dir=directory,
                video_dir=directory,
                pdf_dir=directory,
            )
            store.save(settings)
            loaded = store.load()
            self.assertEqual(loaded.theme, "light")
            self.assertEqual(loaded.parallel_jobs, 4)

    def test_remote_ref_validation(self) -> None:
        self.assertEqual(UpdaterService.split_remote_ref("origin/main"), ("origin", "main"))
        with self.assertRaises(ValueError):
            UpdaterService.split_remote_ref("main")


if __name__ == "__main__":
    unittest.main()
