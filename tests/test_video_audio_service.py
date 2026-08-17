from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from mutagen.id3 import ID3
from PIL import Image

from aio_media_tool.services.audio import AudioMetadata, AudioService
from aio_media_tool.services.video import (
    CutOptions,
    GifOptions,
    VideoOptions,
    VideoService,
    build_cut_segments,
    normalize_explicit_segments,
    numbered_segment_name,
    parse_timecode,
)

pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="FFmpeg/FFprobe sind nicht installiert",
)


def test_video_compression_and_audio_tags(tmp_path: Path) -> None:
    video_source = tmp_path / "source.mp4"
    audio_source = tmp_path / "source.mp3"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100",
            "-t",
            "0.6",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(video_source),
        ],
        check=True,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=660:sample_rate=44100",
            "-t",
            "0.6",
            "-c:a",
            "libmp3lame",
            str(audio_source),
        ],
        check=True,
    )
    video = VideoService().compress_one(
        video_source,
        tmp_path / "video-out",
        VideoOptions(container="mp4", codec="h264", crf=28, height=180),
    )
    probe = VideoService().probe(video)
    assert video.stat().st_size > 0
    assert any(
        stream.get("height") == 180
        for stream in probe["streams"]
        if stream.get("codec_type") == "video"
    )

    cover = tmp_path / "cover.jpg"
    Image.new("RGB", (300, 200), "#6f5bf3").save(cover)
    audio = AudioService().process_local_mp3(
        audio_source,
        tmp_path / "audio-out",
        AudioMetadata(title="Smoke Song", artist="AIO", lyrics="Test lyrics", cover=cover),
    )[0]
    tags = ID3(audio)
    assert str(tags["TIT2"]) == "Smoke Song"
    assert str(tags["TPE1"]) == "AIO"
    assert tags.getall("APIC") and tags.getall("USLT")


def test_target_size_gif_and_mp3_merge(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    mp3 = tmp_path / "source.mp3"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100",
            "-t",
            "1.2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=550:sample_rate=44100",
            "-t",
            "0.55",
            "-c:a",
            "libmp3lame",
            str(mp3),
        ],
        check=True,
    )
    service = VideoService()
    compressed = service.compress_one(
        source,
        tmp_path / "target",
        VideoOptions(container="mp4", codec="h264", target_mb=1, audio_bitrate=96),
    )
    assert 0 < compressed.stat().st_size <= 1024 * 1024
    gif = service.segment_to_gif(
        source,
        tmp_path / "gif",
        GifOptions(start_seconds=0.1, end_seconds=0.7, fps=8, width=240, colors=96),
    )
    assert gif.suffix == ".gif" and gif.stat().st_size > 0
    with Image.open(gif) as opened:
        assert opened.width == 240

    merged = AudioService().compose_mp3(
        [mp3, mp3],
        tmp_path / "mix",
        AudioMetadata(title="My Mix", artist="Tester", lyrics="Line one"),
        merge=True,
    )[0]
    probe = service.probe(merged)
    assert float(probe["format"]["duration"]) > 0.9
    tags = ID3(merged)
    assert str(tags["TIT2"]) == "My Mix"
    assert tags.getall("USLT")


def test_video_cut_and_compressed_segment(tmp_path: Path) -> None:
    source = tmp_path / "long-source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=480x270:rate=25",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=500:sample_rate=44100",
            "-t",
            "2.0",
            "-c:v",
            "libx264",
            "-g",
            "25",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
    )
    service = VideoService()
    copied = service.cut_segment(
        source,
        tmp_path / "cut",
        CutOptions(start_seconds=0.4, end_seconds=1.4, output_name="Teil:01"),
    )
    assert copied.name == "Teil_01.mp4"
    assert copied.stat().st_size > 0

    encoded = service.compress_one(
        source,
        tmp_path / "encoded",
        VideoOptions(container="mp4", codec="h264", crf=25),
        start_seconds=0.4,
        end_seconds=1.4,
        output_name="Kapitel 1.mp4",
    )
    assert encoded.name == "Kapitel 1.mp4"
    probe = service.probe(encoded)
    assert 0.8 <= float(probe["format"]["duration"]) <= 1.2


def test_max_height_is_a_cap_and_does_not_upscale(tmp_path: Path) -> None:
    source = tmp_path / "sd-source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=24",
            "-t",
            "0.5",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
    )
    service = VideoService()
    output = service.compress_one(
        source,
        tmp_path / "no-upscale",
        VideoOptions(container="mp4", codec="h264", crf=28, height=1080, mute=True),
    )
    probe = service.probe(output)
    video_stream = next(
        stream for stream in probe["streams"] if stream.get("codec_type") == "video"
    )
    assert video_stream["height"] == 240


def test_multi_segment_ranges_names_and_processing_end(tmp_path: Path) -> None:
    segments = build_cut_segments(0.0, 3.0, [1.0, 2.0, 3.5, -1.0])
    assert segments == [(0.0, 1.0), (1.0, 2.0), (2.0, 3.0)]
    names = [numbered_segment_name("Video 1", index) for index in range(1, 4)]
    assert names == ["Video 11", "Video 12", "Video 13"]

    source = tmp_path / "Video 1.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=480x270:rate=25",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=500:sample_rate=44100",
            "-t",
            "4.0",
            "-c:v",
            "libx264",
            "-g",
            "25",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
    )

    service = VideoService()
    outputs = []
    for (start, end), name in zip(segments, names):
        outputs.append(
            service.compress_one(
                source,
                tmp_path / "multi",
                VideoOptions(container="mp4", codec="h264", crf=28),
                start_seconds=start,
                end_seconds=end,
                output_name=name,
            )
        )

    assert [path.name for path in outputs] == ["Video 11.mp4", "Video 12.mp4", "Video 13.mp4"]
    assert not (tmp_path / "multi" / "Video 14.mp4").exists()
    for output in outputs:
        duration = float(service.probe(output)["format"]["duration"] or 0)
        assert 0.75 <= duration <= 1.25


def test_direct_time_ranges_and_frame_index(tmp_path: Path) -> None:
    assert parse_timecode("00:01:30") == 90.0
    assert parse_timecode("03:00.250") == 180.25
    assert parse_timecode("12.5") == 12.5
    assert normalize_explicit_segments([(180, 240), (90, 120)], 300) == [
        (90.0, 120.0),
        (180.0, 240.0),
    ]
    with pytest.raises(ValueError):
        normalize_explicit_segments([(10, 20), (19, 30)], 60)

    source = tmp_path / "frame-source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=25",
            "-t",
            "2.0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
    )
    service = VideoService()
    probe = service.probe(source)
    assert 24.9 <= service.frame_rate_from_probe(probe) <= 25.1
    timestamps = service.frame_timestamps(source)
    assert 48 <= len(timestamps) <= 52
    assert timestamps[0] <= 0.01
    assert 1.8 <= timestamps[-1] <= 2.0

    ranges = normalize_explicit_segments([(0.2, 0.6), (1.2, 1.7)], 2.0)
    outputs = [
        service.compress_one(
            source,
            tmp_path / "ranges",
            VideoOptions(container="mp4", codec="h264", crf=30, mute=True),
            start_seconds=start,
            end_seconds=end,
            output_name=f"Bereich {index}",
        )
        for index, (start, end) in enumerate(ranges, 1)
    ]
    assert [path.name for path in outputs] == ["Bereich 1.mp4", "Bereich 2.mp4"]
    durations = [float(service.probe(path)["format"]["duration"] or 0) for path in outputs]
    assert 0.3 <= durations[0] <= 0.6
    assert 0.4 <= durations[1] <= 0.7


def test_nonzero_cut_start_does_not_include_long_gop_preroll(tmp_path: Path) -> None:
    """Regression: stream-copy used to include content before a non-zero start."""
    source = tmp_path / "long-gop.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=6",
            "-f", "lavfi", "-i", "sine=frequency=700:sample_rate=48000:duration=6",
            "-c:v", "libx264", "-g", "150", "-keyint_min", "150", "-sc_threshold", "0",
            "-pix_fmt", "yuv420p", "-c:a", "aac", str(source),
        ],
        check=True,
    )
    service = VideoService()
    output = service.cut_segment(
        source,
        tmp_path / "cut-nonzero",
        CutOptions(start_seconds=2.0, end_seconds=3.0, output_name="ab-zwei"),
    )
    duration = float(service.probe(output)["format"]["duration"] or 0)
    assert 0.9 <= duration <= 1.15


def test_nvenc_profile_mapping_and_recording_safety(monkeypatch: pytest.MonkeyPatch) -> None:
    help_text = " ".join(
        [
            "Encoder hevc_nvenc",
            "-multipass qres disabled",
            "-rc-lookahead",
            "-spatial-aq",
            "-temporal-aq",
            "-split_encode_mode",
        ]
    )
    monkeypatch.setattr(VideoService, "_nvenc_encoder_help", {"hevc_nvenc": help_text})
    service = VideoService()

    recording = VideoOptions(
        codec="h265", preset="quality", encoder_backend="nvenc", nvenc_mode="recording"
    )
    encoder, is_nvenc = service._resolve_encoder(recording)
    assert encoder == "hevc_nvenc" and is_nvenc
    args = service._nvenc_tuning_args(encoder, recording)
    assert args[args.index("-preset") + 1] == "p4"
    assert args[args.index("-multipass") + 1] == "disabled"
    assert args[args.index("-rc-lookahead") + 1] == "0"
    assert args[args.index("-split_encode_mode") + 1] == "disabled"

    quality = VideoOptions(
        codec="h265", preset="quality", encoder_backend="nvenc", nvenc_mode="quality"
    )
    quality_args = service._nvenc_tuning_args(encoder, quality)
    assert quality_args[quality_args.index("-preset") + 1] == "p6"
    assert quality_args[quality_args.index("-multipass") + 1] == "qres"
    assert "-split_encode_mode" not in quality_args

    with pytest.raises(ValueError, match="VP9"):
        service._resolve_encoder(VideoOptions(codec="vp9", encoder_backend="nvenc"))


def test_hdr_dual_export_preserves_hdr_and_creates_bt709_sdr(tmp_path: Path) -> None:
    filters = subprocess.run(
        ["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True, check=False
    ).stdout
    encoders = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, check=False
    ).stdout
    if " zscale " not in filters or " tonemap " not in filters or "libx265" not in encoders:
        pytest.skip("FFmpeg-Build hat nicht alle HDR-Testkomponenten")

    source = tmp_path / "hdr10-source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=24:duration=1.0",
            "-vf",
            "format=yuv420p10le",
            "-c:v",
            "libx265",
            "-preset",
            "ultrafast",
            "-x265-params",
            (
                "log-level=error:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:"
                "master-display=G(13250,34500)B(7500,3000)R(34000,16000)"
                "WP(15635,16450)L(10000000,50):max-cll=1000,400"
            ),
            "-tag:v",
            "hvc1",
            "-color_primaries",
            "bt2020",
            "-color_trc",
            "smpte2084",
            "-colorspace",
            "bt2020nc",
            "-color_range",
            "tv",
            str(source),
        ],
        check=True,
    )

    service = VideoService()
    detected = service.hdr_info(source)
    assert detected.is_hdr
    assert detected.label == "HDR10 / PQ"

    hdr_output, sdr_output = service.compress_hdr_sdr_pair(
        source,
        tmp_path / "dual",
        VideoOptions(container="mp4", codec="h264", preset="fast", crf=30, mute=True),
        hdr_codec="h265",
        tone_map="hable",
    )
    assert hdr_output.name == "hdr10-source_HDR.mp4"
    assert sdr_output.name == "hdr10-source_SDR.mp4"

    hdr_stream = next(
        item for item in service.probe(hdr_output)["streams"] if item.get("codec_type") == "video"
    )
    assert hdr_stream.get("codec_name") == "hevc"
    assert hdr_stream.get("pix_fmt") == "yuv420p10le"
    assert hdr_stream.get("color_primaries") == "bt2020"
    assert hdr_stream.get("color_transfer") == "smpte2084"
    assert hdr_stream.get("color_space") == "bt2020nc"

    sdr_stream = next(
        item for item in service.probe(sdr_output)["streams"] if item.get("codec_type") == "video"
    )
    assert sdr_stream.get("codec_name") == "h264"
    assert sdr_stream.get("pix_fmt") == "yuv420p"
    assert sdr_stream.get("color_primaries") == "bt709"
    assert sdr_stream.get("color_transfer") == "bt709"
    assert sdr_stream.get("color_space") == "bt709"

    side_data = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_frames",
            "-read_intervals",
            "%+#1",
            "-show_entries",
            "frame=side_data_list",
            "-of",
            "json",
            str(hdr_output),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "Mastering display metadata" in side_data
    assert "Content light level metadata" in side_data


def test_hdr_dual_export_rejects_sdr_source(tmp_path: Path) -> None:
    source = tmp_path / "sdr.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=24:duration=0.4",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
    )
    service = VideoService()
    assert not service.hdr_info(source).is_hdr
    with pytest.raises(ValueError, match="HDR10/PQ oder HLG"):
        service.compress_hdr_sdr_pair(
            source,
            tmp_path / "dual",
            VideoOptions(container="mp4", codec="h264", crf=30, mute=True),
        )
