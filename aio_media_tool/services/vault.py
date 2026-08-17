from __future__ import annotations

import base64
import io
import json
import os
import shutil
import stat
import struct
import tempfile
import zipfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from threading import Event

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from aio_media_tool.services.common import (
    ProgressCallback,
    check_cancelled,
    noop_progress,
    unique_output,
)

MAGIC = b"AIOMENC1"
TAG_SIZE = 16
DEFAULT_ITERATIONS = 600_000
MEMORY_DECRYPT_LIMIT = 64 * 1024 * 1024


class VaultPasswordError(ValueError):
    pass


class VaultFormatError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class VaultEntry:
    source: Path
    archive_name: str
    size: int


class _EncryptionSink(io.RawIOBase):
    def __init__(self, output, encryptor) -> None:
        self.output = output
        self.encryptor = encryptor
        self.position = 0

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def tell(self) -> int:
        return self.position

    def write(self, data) -> int:
        block = bytes(data)
        if block:
            self.output.write(self.encryptor.update(block))
            self.position += len(block)
        return len(block)

    def flush(self) -> None:
        if not self.closed and not getattr(self.output, "closed", False):
            self.output.flush()


class VaultService:
    @staticmethod
    def validate_password(password: str) -> None:
        if len(password) < 12:
            raise VaultPasswordError("Das Vault-Passwort muss mindestens 12 Zeichen lang sein.")
        groups = sum(
            (
                any(character.islower() for character in password),
                any(character.isupper() for character in password),
                any(character.isdigit() for character in password),
                any(not character.isalnum() and not character.isspace() for character in password),
            )
        )
        if len(password) < 20 and groups < 3:
            raise VaultPasswordError(
                "Nutze mindestens drei Zeichengruppen oder eine Passphrase mit 20+ Zeichen."
            )

    @staticmethod
    def _derive_key(password: str, salt: bytes, iterations: int) -> bytes:
        return PBKDF2HMAC(
            algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations
        ).derive(password.encode("utf-8"))

    @staticmethod
    def _entries(sources: list[Path]) -> list[VaultEntry]:
        if not sources:
            raise ValueError("Bitte mindestens eine Datei oder einen Ordner auswählen.")
        entries: list[VaultEntry] = []
        names: set[str] = set()
        for raw in sources:
            source = raw.expanduser().resolve()
            if source.is_symlink():
                raise ValueError(f"Symbolische Links werden nicht archiviert: {source.name}")
            if source.is_file():
                candidates = [(source, source.name)]
            elif source.is_dir():
                candidates = [
                    (path, f"{source.name}/{path.relative_to(source).as_posix()}")
                    for path in sorted(source.rglob("*"))
                    if path.is_file() and not path.is_symlink()
                ]
            else:
                raise FileNotFoundError(source)
            for path, name in candidates:
                clean_name = PurePosixPath(name).as_posix().lstrip("/")
                if clean_name in names:
                    raise ValueError(f"Doppelter Archivname: {clean_name}")
                names.add(clean_name)
                entries.append(VaultEntry(path, clean_name, path.stat().st_size))
        if not entries:
            raise ValueError("Die gewählten Ordner enthalten keine Dateien.")
        return entries

    def encrypt(
        self,
        sources: list[Path],
        output: Path,
        password: str,
        progress: ProgressCallback = noop_progress,
        cancel: Event | None = None,
    ) -> list[Path]:
        self.validate_password(password)
        entries = self._entries(sources)
        output = output.expanduser()
        if output.suffix.casefold() != ".aio_enc":
            output = output.with_suffix(".aio_enc")
        output = unique_output(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        salt, nonce = os.urandom(16), os.urandom(12)
        header = json.dumps(
            {
                "version": 1,
                "kdf": "PBKDF2-HMAC-SHA256",
                "iterations": DEFAULT_ITERATIONS,
                "salt": base64.b64encode(salt).decode("ascii"),
                "cipher": "AES-256-GCM",
                "nonce": base64.b64encode(nonce).decode("ascii"),
                "file_count": len(entries),
                "plain_size": sum(entry.size for entry in entries),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        prefix = MAGIC + struct.pack(">I", len(header)) + header
        key = self._derive_key(password, salt, DEFAULT_ITERATIONS)
        encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
        encryptor.authenticate_additional_data(prefix)
        handle, temp_name = tempfile.mkstemp(
            prefix=f".{output.stem}-", suffix=".aio_enc", dir=output.parent
        )
        os.chmod(temp_name, stat.S_IRUSR | stat.S_IWUSR)
        temp = Path(temp_name)
        total = max(1, sum(entry.size for entry in entries))
        processed = 0
        try:
            with os.fdopen(handle, "wb") as encrypted_stream:
                encrypted_stream.write(prefix)
                sink = _EncryptionSink(encrypted_stream, encryptor)
                with zipfile.ZipFile(
                    sink, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
                ) as archive:
                    manifest = {
                        "format": "AIO_M Vault",
                        "version": 1,
                        "files": [entry.archive_name for entry in entries],
                    }
                    archive.writestr(
                        "AIO_M_manifest.json",
                        json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
                    )
                    for entry in entries:
                        check_cancelled(cancel)
                        info = zipfile.ZipInfo.from_file(entry.source, entry.archive_name)
                        info.compress_type = zipfile.ZIP_DEFLATED
                        with (
                            entry.source.open("rb") as source_stream,
                            archive.open(info, "w", force_zip64=True) as archive_stream,
                        ):
                            while block := source_stream.read(1024 * 1024):
                                check_cancelled(cancel)
                                archive_stream.write(block)
                                processed += len(block)
                                progress(
                                    min(98, int(processed / total * 98)),
                                    f"Verschlüsselt: {entry.archive_name}",
                                )
                encrypted_stream.write(encryptor.finalize())
                encrypted_stream.write(encryptor.tag)
                encrypted_stream.flush()
                os.fsync(encrypted_stream.fileno())
            os.replace(temp, output)
        finally:
            temp.unlink(missing_ok=True)
            del key
            del password
        progress(100, f"Vault erstellt: {output.name}")
        return [output]

    @staticmethod
    def _read_header(stream) -> tuple[dict, bytes, int]:
        magic = stream.read(len(MAGIC))
        if magic != MAGIC:
            raise VaultFormatError("Die Datei ist kein unterstütztes AIO_M-Vault-Archiv.")
        length_raw = stream.read(4)
        if len(length_raw) != 4:
            raise VaultFormatError("Der Vault-Header ist beschädigt.")
        length = struct.unpack(">I", length_raw)[0]
        if length < 20 or length > 64 * 1024:
            raise VaultFormatError("Der Vault-Header hat eine ungültige Größe.")
        header_raw = stream.read(length)
        if len(header_raw) != length:
            raise VaultFormatError("Der Vault-Header ist unvollständig.")
        try:
            header = json.loads(header_raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VaultFormatError("Der Vault-Header ist nicht lesbar.") from exc
        if header.get("cipher") != "AES-256-GCM" or header.get("kdf") != "PBKDF2-HMAC-SHA256":
            raise VaultFormatError("Unbekannte Vault-Verschlüsselung.")
        return header, magic + length_raw + header_raw, len(MAGIC) + 4 + length

    def decrypt(
        self,
        archive_path: Path,
        output_dir: Path,
        password: str,
        private_temp: Path | None = None,
        progress: ProgressCallback = noop_progress,
        cancel: Event | None = None,
    ) -> list[Path]:
        if not archive_path.is_file():
            raise FileNotFoundError(archive_path)
        if not password:
            raise VaultPasswordError("Bitte das Vault-Passwort eingeben.")
        archive_size = archive_path.stat().st_size
        staging_path: Path | None = None
        plain_stream: io.BytesIO | object
        try:
            with archive_path.open("rb") as source:
                header, aad, data_start = self._read_header(source)
                iterations = int(header.get("iterations") or 0)
                if not 100_000 <= iterations <= 2_000_000:
                    raise VaultFormatError("Ungültige PBKDF2-Parameter im Vault.")
                try:
                    salt = base64.b64decode(header["salt"], validate=True)
                    nonce = base64.b64decode(header["nonce"], validate=True)
                except (KeyError, ValueError) as exc:
                    raise VaultFormatError(
                        "Kryptografie-Parameter im Vault sind beschädigt."
                    ) from exc
                if len(salt) != 16 or len(nonce) != 12:
                    raise VaultFormatError("Kryptografie-Parameter im Vault sind ungültig.")
                cipher_size = archive_size - data_start - TAG_SIZE
                if cipher_size <= 0:
                    raise VaultFormatError("Der Vault enthält keine verschlüsselten Daten.")
                source.seek(archive_size - TAG_SIZE)
                tag = source.read(TAG_SIZE)
                key = self._derive_key(password, salt, iterations)
                decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
                decryptor.authenticate_additional_data(aad)
                source.seek(data_start)
                if cipher_size <= MEMORY_DECRYPT_LIMIT:
                    plain_stream = io.BytesIO()
                else:
                    if private_temp is None:
                        raise RuntimeError(
                            "Große Vaults benötigen den privaten App-Temp-Ordner; ein System-Temp wird nicht verwendet."
                        )
                    private_temp.mkdir(parents=True, exist_ok=True)
                    with suppress(OSError):
                        private_temp.chmod(stat.S_IRWXU)
                    handle, name = tempfile.mkstemp(
                        prefix="vault-decrypt-", suffix=".zip", dir=private_temp
                    )
                    os.chmod(name, stat.S_IRUSR | stat.S_IWUSR)
                    staging_path = Path(name)
                    plain_stream = os.fdopen(handle, "w+b")
                remaining = cipher_size
                try:
                    while remaining:
                        check_cancelled(cancel)
                        block = source.read(min(1024 * 1024, remaining))
                        if not block:
                            raise VaultFormatError("Der Vault-Datenstrom ist vorzeitig beendet.")
                        plain_stream.write(decryptor.update(block))
                        remaining -= len(block)
                        progress(
                            min(48, int((cipher_size - remaining) / cipher_size * 48)),
                            "Vault wird authentifiziert",
                        )
                    plain_stream.write(decryptor.finalize())
                    plain_stream.seek(0)
                except InvalidTag as exc:
                    raise VaultPasswordError(
                        "Falsches Passwort oder beschädigtes Vault-Archiv."
                    ) from exc
                finally:
                    del key
                    del password
            result = self._extract_authenticated_zip(
                plain_stream,
                archive_path,
                output_dir,
                int(header.get("plain_size") or 0),
                progress,
                cancel,
            )
            progress(100, f"Vault entschlüsselt: {result.name}")
            return [result]
        except InvalidTag as exc:
            raise VaultPasswordError("Falsches Passwort oder beschädigtes Vault-Archiv.") from exc
        finally:
            if "plain_stream" in locals():
                plain_stream.close()
            if staging_path:
                staging_path.unlink(missing_ok=True)

    @staticmethod
    def _safe_member(info: zipfile.ZipInfo) -> PurePosixPath:
        path = PurePosixPath(info.filename)
        mode = info.external_attr >> 16
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise VaultFormatError(f"Unsicherer Archivpfad: {info.filename}")
        if stat.S_ISLNK(mode):
            raise VaultFormatError(
                f"Symbolische Links sind im Vault nicht erlaubt: {info.filename}"
            )
        return path

    def _extract_authenticated_zip(
        self,
        stream,
        archive_path: Path,
        output_dir: Path,
        expected_size: int,
        progress: ProgressCallback,
        cancel: Event | None,
    ) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        base = archive_path.name
        if base.casefold().endswith(".aio_enc"):
            base = base[: -len(".aio_enc")]
        target = unique_output(output_dir / f"{base}_entschluesselt")
        partial = output_dir / f".{target.name}.partial-{os.urandom(5).hex()}"
        partial.mkdir(parents=True)
        try:
            with zipfile.ZipFile(stream, "r") as archive:
                members = [
                    info for info in archive.infolist() if info.filename != "AIO_M_manifest.json"
                ]
                total = sum(info.file_size for info in members)
                if expected_size and total != expected_size:
                    raise VaultFormatError("Die authentifizierte Vault-Größenangabe stimmt nicht.")
                processed = 0
                for info in members:
                    check_cancelled(cancel)
                    relative = self._safe_member(info)
                    destination = partial.joinpath(*relative.parts)
                    if info.is_dir():
                        destination.mkdir(parents=True, exist_ok=True)
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info, "r") as source, destination.open("wb") as output:
                        while block := source.read(1024 * 1024):
                            check_cancelled(cancel)
                            output.write(block)
                            processed += len(block)
                            progress(
                                50 + int(processed / max(1, total) * 49),
                                f"Stellt wieder her: {relative.as_posix()}",
                            )
            os.replace(partial, target)
        except Exception:
            shutil.rmtree(partial, ignore_errors=True)
            raise
        return target
