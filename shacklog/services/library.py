"""Adding files to the library (used by the Library page, attachments and imports).

:func:`store_upload` puts the bytes in the blob store, creates (or reuses) the
:class:`StoredFile` row and extracts searchable text. Uploading the same bytes twice
returns the existing library entry instead of a duplicate.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import BinaryIO

from sqlalchemy.orm import Session

from shacklog.models import StoredFile
from shacklog.services.filestore import FileStore, extract_text, guess_mime


def safe_filename(name: str | None) -> str:
    """Keep the user's file name readable but strip any directory parts."""
    name = (name or "file").replace("\\", "/").split("/")[-1].strip()
    return name or "file"


def store_upload(session: Session, store: FileStore, stream: BinaryIO, filename: str,
                 category: str = "document", title: str | None = None,
                 description: str | None = None, tags: str | None = None) -> tuple[StoredFile, bool]:
    """Store a file. Returns (library entry, created?)."""
    filename = safe_filename(filename)
    sha, size = store.put_stream(stream)
    existing = (session.query(StoredFile)
                .filter(StoredFile.sha256 == sha, StoredFile.deleted_at.is_(None)).first())
    if existing is not None:
        return existing, False
    mime = guess_mime(filename)
    entry = StoredFile(
        sha256=sha, filename=filename, mime=mime, size=size, category=category or "document",
        title=title or Path(filename).stem.replace("_", " "), description=description, tags=tags,
        text_content=extract_text(store.path_for(sha), filename, mime),
    )
    session.add(entry)
    return entry, True


def store_bytes(session: Session, store: FileStore, data: bytes, filename: str,
                **kwargs) -> tuple[StoredFile, bool]:
    import io

    return store_upload(session, store, io.BytesIO(data), filename, **kwargs)


def temp_copy(store: FileStore, sha256: str, suffix: str = "") -> Path:
    """Copy a blob to a temporary file (for tools that need a real path)."""
    fd, name = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    store.copy_to(sha256, Path(name))
    return Path(name)
