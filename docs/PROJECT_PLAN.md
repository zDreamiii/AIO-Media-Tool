# AIO Media Tool – Umsetzungsplan und Status

## Zielbild

Eine ausschließlich native Python-/Qt-Desktop-Anwendung bündelt Medien-, Datei-, Dokument-, Privacy- und Gaming-Wissensaufgaben. Lange Arbeiten laufen über Hintergrund-Worker, die gemeinsame Queue oder kontrollierte lokale Subprozesse. Es gibt keinen Webserver und keine Telemetrie.

## Module und Stand 0.3.14

| Bereich | Implementiert | Nächster sinnvoller Ausbau |
| --- | --- | --- |
| Download/Musik | ID/URL, Einzel/Playlist, entfernbare Vorschau, MP3, Cover, ID3, Lyrics/LRC, Normalisierung, MP3-Merge | Stream-Detailauswahl, pausierbare Downloads |
| Bilder/Video | WebP-Batch, Ziel-KB, Skalierung; CRF/Two-Pass-Ziel-MB, Presets, GIF-Segmente | Hardwareencoder, visueller Schnittdialog |
| PDF/Dateien | Merge/Split/Extract/Rotate/Kompression/Schutz; sicherer Bulk-Renamer | Seitenminiaturen, gespeicherte Rename-Presets |
| Sammlungen | Kategorien, Unterkategorien, Notiz-/Bild-/YouTube-Blöcke, Drag/Resize/Snap/Zoom | Suche, Import/Export, Backups |
| Transkription | Session-Ruhemodus, lokales faster-whisper, Auto-Sprache, vier Modellstufen, SRT/VTT, Hardsubs | Untertitel-Editor, Sprechertrennung |
| Deep Clean | rekursive JPG/PNG/MP4/PDF/MP3-Reinigung, Kopie/.bak, JSON/CSV-Diff | signierte Bereinigungsberichte, weitere Office-Formate |
| Vault | AES-256-GCM, PBKDF2, Streaming-Archiv, sichere Extraktion, privates Staging | Argon2id-Profil, Vault-Inhaltsbrowser |
| Smart Clipboard | opt-in Tray, Text/Bild-SQLite, Suche, Makros, Retention, App-Blacklist | globale Hotkeys, OS-spezifisch bessere Wayland-Integration |
| OCR/Übersetzung | Session-Ruhemodus, Tesseract/EasyOCR, PDF-Rendering, Doppelfeld-Editor, DeepL/MarianMT, TXT/DOCX | Layout-erhaltender PDF-Export, Tabellen-OCR |
| KI-Upscaling | Session-Ruhemodus, Real-ESRGAN, RIFE, Hardwarecheck, Queue-ETA, 1-s-Split-Vorschau | Binary-Installer/Hashprüfung, GPU-Presets |
| Plattform | 17 Seiten, Dark/Light, Queue, Abbruch, Verlauf, Diagnose, Git-/Paket-Rebuild | Wiederholen/Pause, Benachrichtigungen, Installer |

## Architektur

```text
Native PySide6 UI
        ↓ validierte Jobparameter
JobManager / QThreadPool / TaskWorker
        ↓ Fortschritt + Abbruchsignal
Medien-, Dokument-, Privacy-, OCR-, Vault- und KI-Dienste
        ↓ atomare oder authentifizierte Ausgaben
Lokales Dateisystem + SQLite-Verlauf/Snippets + Workspace-JSON
```

- Keine lang laufende Verarbeitung arbeitet im UI-Thread.
- FFmpeg, Tesseract, ExifTool und NCNN erhalten Argumentlisten; es gibt keine Shell-Ausführung mit Nutzereingaben.
- Downloads akzeptieren nur HTTP(S) oder YouTube-IDs und umgehen keine Zugriffsschutzmaßnahmen.
- Vault-Passwörter und API-Keys werden nicht persistiert oder in Job-Payloads geschrieben.
- Vault-Archive authentifizieren Header und Inhalt; Extraktion blockiert absolute Pfade, `..` und Symlinks.
- Deep Clean schreibt Kopien oder verschiebt das Original vorab nach `.bak`; ein Fehler löst Wiederherstellung aus.
- Clipboard-Monitoring ist standardmäßig deaktiviert, hat `secure_delete`, Retention und einen pausierbaren Tray-Modus.
- Whisper, OCR, Upscaling und MarianMT erkennen fehlende optionale Engines und melden konkrete Installationsschritte.
- Die drei KI-Seiten werden pro App-Sitzung erst durch einen eigenen Aktivierungsknopf erzeugt. Vorher laufen weder Backend-Importe noch Hardware-Scans oder KI-Prozesse.

## Native UI und Skalierung

- Qt 6 übernimmt Per-Monitor-DPI mit `PassThrough`-Rundung für gemischte FHD-/4K-Setups.
- Alle Hauptseiten verwenden Layouts und Scrollbereiche statt fester Bildschirmkoordinaten.
- Die gruppierte Sidebar bleibt bei kleinen Höhen scrollbar.
- Das Sammlungsboard besitzt 75–150 % Zoom; Blöcke unterstützen Drag/Resize und 8-px-Snapping.
- OCR nutzt einen horizontalen Splitter, der KI-Upscaler einen interaktiven Vorher/Nachher-Slider.

## Sicherheitsprofile

### Vault

1. PBKDF2-HMAC-SHA256 mit zufälligem 128-Bit-Salt und 600.000 Iterationen.
2. Zufällige 96-Bit-Nonce und AES-256-GCM.
3. ZIP-Daten werden direkt in den GCM-Verschlüsseler gestreamt; es entsteht kein Klartextarchiv im System-Temp.
4. Beim Öffnen erfolgt GCM-Authentifizierung vor der Extraktion. Große Archive nutzen nur den privaten App-Temp-Pfad mit restriktiven Rechten und anschließender Bereinigung.
5. Die eigentliche Wiederherstellung landet zuerst in einem partiellen Zielordner und wird danach atomar umbenannt.

### Deep Clean

- Bilder werden ohne EXIF/Text-Chunks neu codiert.
- PDFs werden seitenweise ohne Dokumentinformationen neu geschrieben.
- MP3-Tags werden vollständig entfernt.
- MP4 wird mit `map_metadata -1`, entfernten Kapiteln und Bitexact-Flags remuxt, damit keine FFmpeg-Encoder-Signatur bleibt.
- ExifTool kann zusätzlich analysieren, verifizieren und alle verbleibenden beschreibbaren Tags löschen.

### Netzwerkgrenzen

- Whisper, Tesseract, EasyOCR, MarianMT, Vault, Deep Clean und NCNN arbeiten lokal.
- Whisper darf nur nach bewusster Deaktivierung des Offline-Hakens ein noch fehlendes Modell laden.
- DeepL ist explizit als externer Dienst markiert; Schlüssel werden im Authorization-Header statt in URLs gesendet.

## Privater Update-/Rebuild-Ablauf

1. Höchstens im konfigurierten Intervall prüfen.
2. Remote und Fast-Forward-Möglichkeit ermitteln; bei lokalen Änderungen stoppen.
3. App beenden und separaten Updater starten.
4. `uv sync --locked --extra dev --extra transcription --extra ocr` ausführen.
5. Tests und PyInstaller-Onefile-Build starten.
6. Bei Fehlern auf den vorherigen Commit zurückrollen, Umgebung erneut synchronisieren und die letzte Version starten.

Für öffentliche Windows-Releases wird zusätzlich eine einzelne EXE über GitHub Actions gebaut. Signierung und ein Installer bleiben Themen für 1.0.

## Umsetzungsphasen

### Phase 1 – Medien-/Dokumentbasis (implementiert)

- native Navigation, Themes, Einstellungen und Pfade
- Download-, Audio-, Bild-, Video- und PDF-Dienste
- Queue, Abbruch, Verlauf, Diagnose und Git-basierter Updater

### Phase 2 – AIO-Workflows (implementiert in 0.2)

- Playlist-Vorschau und selektive Downloads
- MP3-Studio, Zielgrößen-Video, GIF, WebP, Bulk-Renamer und Sammlungsboard

### Phase 3 – Offline-KI und Privacy (implementiert in 0.3)

- faster-whisper mit SRT/VTT/Hardsubs
- Metadaten-Deep-Clean mit Backup/Diff
- authentifizierter lokaler Vault
- Smart Clipboard mit Retention/Blacklist
- OCR/Übersetzung und editierbare Exporte
- Real-ESRGAN/RIFE mit Hardwarecheck, ETA und Split-Vorschau

### Phase 4 – Workflow-Tiefe

- speicherbare Presets, Dublettenerkennung und „Job wiederholen“
- Untertitel-Editor, Sprechertrennung, OCR-Layoutrekonstruktion
- Workspace-Suche/Export und automatische Backups
- geführte, hashgeprüfte Installation externer Binaries

### Phase 5 – Distribution

- reproduzierbare Installer pro Zielbetriebssystem
- signierte Releases, Rollback und Datenmigrationen
- UI-End-to-End-, Abbruch-, Wiederanlauf-, GPU- und Mixed-DPI-Abnahmetests

## Teststand 0.3.14

- Ruff, Format- und Bytecodeprüfung
- 38 definierte automatisierte Testfälle
- Startregression: Whisper-Probe und GPU-/Hardware-Scan bleiben vor dem Aktivierungsklick unaufgerufen; die echte Seite wird erst danach erzeugt
- echte FFmpeg-Verarbeitung für Videozielgröße, GIF, MP3 und Hardsubs
- echte Tesseract-OCR auf PNG und gerendertem PDF
- Deep Clean auf JPEG/PDF sowie real getaggten MP3-/MP4-Dateien
- Vault-Roundtrip, falsches Passwort, erzwungenes privates Staging und Pfadsicherheit
- Snippet-Deduplizierung, Suche, Retention und lokaler Clipboard-Monitor
- Queue-Integration über Transkription, Deep Clean, Vault und Upscaler
- Offscreen-Start aller 17 Navigationseinträge mit KI-Platzhaltern sowie erneutes Rendering nach Aktivierung aller drei KI-Seiten; Aktivierungskarten zusätzlich in FHD und 4K geprüft
- PyInstaller-Onefile-Build mit Whisper/OCR/Kryptografie ist im Build-/Testablauf vorgesehen und sollte vor jedem öffentlichen Release erneut geprüft werden

## Definition of Done für 1.0

- alle Bereiche liefern validierte Ergebnisse und blockieren die UI nicht
- Abbruch oder Fehler beschädigt weder Quellen noch fertige Ausgaben
- Offline-Module funktionieren ohne Netzwerk, sobald Modelle/Binaries lokal vorliegen
- Passwörter, Tokens und sensible Metadaten gelangen nicht in Logs oder Jobverlauf
- Updates und externe Binaries sind signiert beziehungsweise hashgeprüft und rollback-fähig
- Windows-, macOS- und Linux-Abnahmen einschließlich FHD/4K-Mischbetrieb und repräsentativer GPUs sind erfolgreich

## Rechtlicher Rahmen

Downloads sind nur für eigene, gemeinfreie, ausdrücklich freigegebene oder anderweitig rechtmäßig nutzbare Inhalte vorgesehen. Die App umgeht keine DRM-, Login-, Bezahl- oder Zugriffsschutzmaßnahmen und übernimmt keine Browser-Anmeldedaten.
