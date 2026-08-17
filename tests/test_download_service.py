from __future__ import annotations

import pytest

from aio_media_tool.services.downloads import DownloadService


def test_youtube_id_and_playlist_id_are_normalized() -> None:
    service = DownloadService()
    assert service.normalize_input("dQw4w9WgXcQ") == ("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    playlist_id = "PL1234567890abcd"
    assert service.normalize_input(playlist_id) == (
        f"https://www.youtube.com/playlist?list={playlist_id}"
    )
    assert service.looks_like_playlist(playlist_id)
    assert service.looks_like_playlist(f"https://www.youtube.com/playlist?list={playlist_id}")
    assert not service.looks_like_playlist("dQw4w9WgXcQ")
    with pytest.raises(ValueError):
        service.normalize_input("not an id")


def test_collection_inspection_flattens_playlist(monkeypatch) -> None:
    info = {
        "title": "Test Playlist",
        "uploader": "AIO",
        "entries": [
            {
                "id": "dQw4w9WgXcQ",
                "title": "First",
                "playlist_index": 1,
                "url": "dQw4w9WgXcQ",
                "duration": 61,
            },
            None,
            {
                "id": "aqz-KE-bpKQ",
                "title": "Second",
                "playlist_index": 3,
                "url": "aqz-KE-bpKQ",
            },
        ],
    }

    class FakeYdl:
        def __init__(self, _options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, download=False):
            assert not download
            return info

    class FakeBackend:
        YoutubeDL = FakeYdl

    monkeypatch.setattr(DownloadService, "_backend", staticmethod(lambda: FakeBackend))
    result = DownloadService().inspect_collection("dQw4w9WgXcQ")
    assert result["is_playlist"] is True
    assert [entry["index"] for entry in result["entries"]] == [1, 3]
    assert result["entries"][0]["webpage_url"].endswith("dQw4w9WgXcQ")
