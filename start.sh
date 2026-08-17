#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
uv sync --locked --no-progress --extra dev --extra transcription --extra ocr
exec uv run --no-sync aio-media-tool
