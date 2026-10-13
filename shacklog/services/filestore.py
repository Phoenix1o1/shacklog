"""Content-addressed blob store for the library.

A file's bytes are stored once, under their SHA-256: ``files/ab/abcdef…``. The database
(:class:`shacklog.models.StoredFile`) holds the metadata. Consequences:

* the same manual uploaded twice (or arriving from two instances) is stored once;
* a blob is never modified, so sync only has to ask "do you have hash X?";
* blobs are never deleted automatically — deleting a library entry is a soft delete, and
  the bytes stay until a future "purge" tool is run on purpose.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import BinaryIO

CHUNK = 1024 * 1024

#: Extensions whose text we index for search, besides PDFs.
TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".adi", ".adif", ".s1p", ".s2p", ".json",
                   ".ini", ".cfg", ".log", ".xml", ".html", ".htm"}
#: Cap on extracted text kept per file, to keep the database and index lean.
MAX_TEXT_CHARS = 2_000_000

# Types Python's table may not know on every OS.
mimetypes.add_type("text/markdown", ".md")
mimetypes.add_type("application/octet-stream", ".img")
mimetypes.add_type("text/plain", ".s1p")
mimetypes.add_type("text/plain", ".s2p")
mimetypes.add_type("text/plain", ".adi")


SHA256_RE = re.compile(r"[0-9a-f]{64}")


def is_sha256(value) -> bool:
    """True for a lowercase hex SHA-256: the only names blobs may have (no paths!)."""
    return isinstance(value, str) and SHA256_RE.fullmatch(value) is not None


class FileStore:
    """Blob storage rooted at a folder (``Paths.files``)."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, sha256: str) -> Path:
        # The hash comes from the database, which sync and imports fill from other
        # instances: never let it be a path ("../..", "/etc/…").
        if not is_sha256(sha256):
            raise ValueError("Not a SHA-256 file name.")
        return self.root / sha256[:2] / sha256

    def exists(self, sha256: str) -> bool:
        return is_sha256(sha256) and self.path_for(sha256).is_file()

    def put_stream(self, stream: BinaryIO) -> tuple[str, int]:
        """Store a stream, return (sha256, size). Writes to a temp file then renames,
        so a crash never leaves a half-written blob under a valid name."""
        digest = hashlib.sha256()
        size = 0
        fd, tmp_name = tempfile.mkstemp(dir=self.root, prefix=".incoming-")
        try:
            with os.fdopen(fd, "wb") as out:
                while chunk := stream.read(CHUNK):
                    digest.update(chunk)
                    out.write(chunk)
                    size += len(chunk)
            sha = digest.hexdigest()
            target = self.path_for(sha)
            if target.exists():
                os.unlink(tmp_name)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(tmp_name, target)
            return sha, size
        except BaseException:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise

    def put_bytes(self, data: bytes) -> tuple[str, int]:
        import io

        return self.put_stream(io.BytesIO(data))

    def put_file(self, path: Path) -> tuple[str, int]:
        with open(path, "rb") as fh:
            return self.put_stream(fh)

    def put_verified(self, stream: BinaryIO, expected_sha256: str) -> bool:
        """Store a blob received from a peer; reject it if the hash doesn't match."""
        sha, _ = self.put_stream(stream)
        if sha != expected_sha256:
            # Remove only if nothing else legitimately stored these bytes before.
            return False
        return True

    def open(self, sha256: str) -> BinaryIO:
        return open(self.path_for(sha256), "rb")

    def read_bytes(self, sha256: str) -> bytes:
        return self.path_for(sha256).read_bytes()

    def all_hashes(self) -> set[str]:
        return {p.name for p in self.root.glob("??/*") if p.is_file() and len(p.name) == 64}

    def copy_to(self, sha256: str, destination: Path) -> None:
        shutil.copyfile(self.path_for(sha256), destination)


def guess_mime(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def extract_text(path: Path, filename: str, mime: str | None = None) -> str | None:
    """Best-effort text for the search index. Returns None when there is nothing useful.

    PDFs use pypdf (optional dependency); plain-text formats are read directly. Errors are
    swallowed: a file that can't be read is still stored, just not searchable inside.
    """
    suffix = Path(filename).suffix.lower()
    try:
        if suffix == ".pdf" or mime == "application/pdf":
            try:
                from pypdf import PdfReader
            except ImportError:
                return None
            reader = PdfReader(str(path))
            parts: list[str] = []
            total = 0
            for page in reader.pages:
                txt = page.extract_text() or ""
                parts.append(txt)
                total += len(txt)
                if total > MAX_TEXT_CHARS:
                    break
            text = "\n".join(parts).strip()
            return text[:MAX_TEXT_CHARS] or None
        if suffix in TEXT_EXTENSIONS or (mime or "").startswith("text/"):
            raw = path.read_bytes()[: MAX_TEXT_CHARS * 2]
            for encoding in ("utf-8", "latin-1"):
                try:
                    return raw.decode(encoding)[:MAX_TEXT_CHARS]
                except UnicodeDecodeError:
                    continue
    except Exception:  # damaged PDF, permission error…
        return None
    return None
