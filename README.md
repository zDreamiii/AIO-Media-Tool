# AIO Media Tool

AIO Media Tool ist eine lokale Desktop-App für Medien, Dateien und Dokumente. Die Idee dahinter ist simpel: kleinere Aufgaben, für die sonst mehrere Programme nötig wären, in einer Oberfläche zusammenzufassen.

> **Aktueller Stand:** 0.3.14 / Alpha

## Windows-Download

Für normale Nutzer ist die fertige **`AIO-Media-Tool.exe`** gedacht. Dafür werden weder Python noch `uv` benötigt.

Die Release-Version wird als einzelne Datei gebaut. Beim Start legt die App ihre Einstellungen, Logs und lokalen Daten wie gewohnt unter dem Benutzerprofil ab; neben der EXE muss kein Programmordner liegen.

**Wichtig:** FFmpeg/FFprobe und einige Spezialwerkzeuge werden aktuell nicht in die EXE eingebettet. Medienfunktionen, die FFmpeg benötigen, setzen daher weiterhin eine FFmpeg-Installation im `PATH` voraus. Tesseract, Real-ESRGAN und RIFE sind ebenfalls optional externe Werkzeuge.

## Funktionen

### Medien

- Video- und Audio-Downloads über `yt-dlp`
- Playlist-Vorschau mit Auswahl einzelner Einträge
- MP3-Tags, Cover, Lyrics, Normalisierung und Merging
- Bildoptimierung und Skalierung
- Video-Kompression mit H.264, HEVC, AV1 oder VP9
- CPU-Encoding und NVIDIA NVENC
- Video-Cutter mit mehreren Segmenten und Frame-Navigation
- GIF-Export aus Videoausschnitten

### Dateien & Dokumente

- PDFs zusammenführen, teilen, drehen, komprimieren, schützen und entsperren
- Bulk-Renamer mit Vorschau und Konfliktprüfung
- Metadaten aus Bildern, Audio, Video und PDFs entfernen
- Sammlungsboard für Notizen, Bilder und YouTube-Links
- verschlüsselte Vault-Archive mit AES-256-GCM

### Optional

- Transkription mit `faster-whisper`
- OCR über Tesseract
- Übersetzung über DeepL oder ein lokales Modell
- Upscaling mit Real-ESRGAN
- Frame-Interpolation mit RIFE
- lokaler Clipboard-Verlauf

## EXE selbst bauen

### Einfach unter Windows

`build_windows.bat` im Hauptordner doppelklicken. Die Datei findet ihren Projektordner selbst; ein fester Pfad muss nicht eingetragen werden.

Das Skript erstellt eine eigene `.build-venv`, installiert die Build-Abhängigkeiten und erzeugt anschließend direkt die EXE. Die Tests blockieren einen normalen lokalen Build nicht mehr; GitHub Actions führt sie weiterhin vor jedem automatischen Build aus.

```text
dist\AIO-Media-Tool.exe
```

Diese Datei kann danach alleine weitergegeben werden. Python und die virtuelle Umgebung werden auf dem Ziel-PC nicht gebraucht.

Zum Bauen selbst wird Python 3.11 bis 3.13 benötigt. Wer lokal vor dem Build zusätzlich die Tests ausführen möchte, kann `build_windows.bat --test` in einer Eingabeaufforderung starten.

### Manuell

```bash
uv sync --locked --extra dev --extra transcription --extra ocr
uv run --no-sync python scripts/build.py
```

Der Build verwendet PyInstaller im `--onefile`- und `--windowed`-Modus.

## GitHub Actions

Das Repository enthält `.github/workflows/windows-exe.yml`. Bei einem Push auf `main`, einem `v...`-Tag oder per manuellem Start in GitHub Actions wird die Windows-EXE automatisch gebaut und als Artifact hochgeladen.

Damit muss für einen öffentlichen Release nicht auf dem eigenen PC gebaut werden.

## Source-Version starten

Wer am Code arbeitet, kann weiterhin `start.bat` beziehungsweise `start.sh` verwenden. Unter Windows findet `start.bat` seinen Projektordner selbst und legt beim ersten Start eine lokale `.venv` an. Ein fest eingetragener Installationspfad ist nicht nötig.

```bat
start.bat
```

oder manuell:

```bash
uv sync --locked --extra transcription --extra ocr
uv run --no-sync aio-media-tool
```

## Externe Werkzeuge

Einige Funktionen starten andere Programme lokal. Je nach Nutzung werden benötigt:

- **FFmpeg + FFprobe** für viele Audio- und Videofunktionen
- **Tesseract** für OCR
- **ExifTool** für zusätzliche Metadaten-Prüfungen
- **realesrgan-ncnn-vulkan** für Upscaling
- **rife-ncnn-vulkan** für Frame-Interpolation

Die Pfade zu Tesseract und den KI-Werkzeugen können in der App eingestellt werden.

## Updates

Der Git-basierte Updater ist nur für Source-Checkouts gedacht. In der fertigen EXE ist er deaktiviert, weil eine Release-Datei nicht selbst an ihrem Git-Repository oder ihrer Python-Umgebung herumschreiben soll.

Für neue Release-Versionen wird einfach die neue `AIO-Media-Tool.exe` verwendet.

## Datenschutz

Die eigentliche Medien- und Dateiverarbeitung läuft lokal. Es gibt keine eingebaute Telemetrie.

Netzwerkzugriffe entstehen nur bei Funktionen, die sie tatsächlich brauchen, zum Beispiel Downloads, DeepL oder bewusst erlaubte Modell-Downloads.

## Hinweis zu Downloads

Die Download-Funktionen sind für Inhalte gedacht, die du herunterladen und speichern darfst. Die App ist nicht dafür ausgelegt, DRM, Logins, Paywalls oder andere Zugriffsschutzmaßnahmen zu umgehen.

## Startfehler der EXE

Wenn eine gebaute EXE schon beim Start abstürzt, zeigt die Release-Version jetzt einen Fehlerdialog an und schreibt die genaue Python-Fehlermeldung nach `%LOCALAPPDATA%\AIO Media Tool\logs\startup_error.log`. Dadurch verschwindet ein Fehler bei einem `--windowed`-Build nicht mehr einfach unsichtbar.

## Tests

```bash
uv sync --locked --extra dev --extra transcription --extra ocr
uv run --no-sync pytest
```

## Lizenz

MIT License. Siehe `LICENSE`.
