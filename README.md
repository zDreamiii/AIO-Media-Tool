# AIO Media Tool

AIO Media Tool is a Windows desktop application that combines a collection of useful media, file, and document tools in one place.

> **Current version:** 0.3.14 / Alpha

The project is already usable, but it is still in active development and some features depend on external tools.

## Download

For normal use, download **`AIO-Media-Tool.exe`** from the latest GitHub Release and run it.

The release is built as a single executable. Python, `uv`, and a separate installation folder are not required on the target PC.

Some features still require external programs such as FFmpeg, Tesseract, Real-ESRGAN, or RIFE.

## Features

### Media

* Video and audio downloads using `yt-dlp`
* Playlist preview and individual item selection
* MP3 tags, cover art, lyrics, normalization, and merging
* Image optimization and resizing
* Video compression with H.264, HEVC, AV1, or VP9
* CPU encoding and NVIDIA NVENC support
* Video cutter with multiple segments and frame navigation
* GIF export from video clips

### Files and documents

* Merge, split, rotate, compress, protect, and unlock PDFs
* Bulk file renaming with preview and conflict checking
* Remove metadata from images, audio, video, and PDF files
* Collection board for notes, images, and YouTube links
* Encrypted vault archives using AES-256-GCM

### Optional tools

* Transcription with `faster-whisper`
* OCR using Tesseract
* Translation using DeepL or a local model
* Upscaling with Real-ESRGAN
* Frame interpolation with RIFE
* Local clipboard history

## Building the Windows EXE

The easiest way to build the application on Windows is to run:

```text
build_windows.bat
```

The script creates its own build environment, installs the required dependencies, and produces:

```text
dist\AIO-Media-Tool.exe
```

The resulting EXE can be used on another Windows PC without installing Python.

Python 3.11 to 3.13 is required to build the application.

To run the test suite before building:

```text
build_windows.bat --test
```

### Manual build

```bash
uv sync --locked --extra dev --extra transcription --extra ocr
uv run --no-sync python scripts/build.py
```

The Windows release uses PyInstaller in `--onefile` and `--windowed` mode.

## GitHub Actions

The repository includes a Windows build workflow at:

```text
.github/workflows/windows-exe.yml
```

The workflow runs the test suite, builds `AIO-Media-Tool.exe`, and uploads it as a GitHub Actions artifact.

Pushing a version tag such as:

```text
v0.3.14
```

also creates or updates the matching GitHub Release and attaches the Windows EXE automatically.

## External tools

Depending on which features you use, you may need:

* **FFmpeg + FFprobe** — audio and video processing
* **Tesseract** — OCR
* **ExifTool** — additional metadata inspection
* **realesrgan-ncnn-vulkan** — image upscaling
* **rife-ncnn-vulkan** — frame interpolation

Paths for Tesseract and the AI tools can be configured inside the application.

## Updates

The Git-based updater is intended for source checkouts only and is disabled in packaged EXE releases.

To update the packaged application, download the latest `AIO-Media-Tool.exe` from GitHub Releases.

## Privacy

Media and file processing is performed locally and the application does not include telemetry.

Network access is only used by features that require it, such as downloads, DeepL translation, or explicitly requested model downloads.

## Download notice

Download features are intended for content you are allowed to download and store.

The application is not designed to bypass DRM, authentication, paywalls, or other access restrictions.

## Startup errors

If the packaged EXE crashes during startup, the application writes the error details to:

```text
%LOCALAPPDATA%\AIO Media Tool\logs\startup_error.log
```

## Tests

```bash
uv sync --locked --extra dev --extra transcription --extra ocr
uv run --no-sync pytest
```

## License

Licensed under the MIT License. See [`LICENSE`](LICENSE) for details.

