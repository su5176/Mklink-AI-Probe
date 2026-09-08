"""Content identity and durable probe-volume writes; never trust timestamps alone."""
from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path
import sys


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_fingerprint(path: str | Path) -> dict:
    path = Path(path)
    before = path.stat()
    digest = sha256_file(path)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_size, after.st_mtime_ns, after.st_ctime_ns
    ):
        raise OSError("File changed while reading; wait for the build to finish")
    return {"size": after.st_size, "mtime_ns": after.st_mtime_ns, "sha256": digest}


def is_metadata_file(path: str | Path) -> bool:
    return any(part.startswith("._") or part in {
        ".DS_Store", ".Spotlight-V100", ".fseventsd", ".Trashes", "__MACOSX",
    } for part in Path(path).parts)


def sync_file(stream) -> None:
    stream.flush()
    os.fsync(stream.fileno())
    if sys.platform == "darwin":
        import fcntl
        try:
            full_sync = getattr(fcntl, "F_FULLFSYNC", None)
            if full_sync is not None:
                fcntl.fcntl(stream.fileno(), full_sync)
        except OSError as error:
            if error.errno not in {errno.EINVAL, errno.ENOTSUP, errno.ENOTTY}:
                raise


def sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        try:
            os.fsync(descriptor)
        except OSError as error:
            if error.errno not in {errno.EINVAL, errno.ENOTSUP}:
                raise
    finally:
        os.close(descriptor)


def write_verified(path: str | Path, payload: bytes) -> None:
    """Flush data and directory metadata before a probe is told to open the file.

    Host readback validates the copy, not the target Flash or probe cache.
    """
    path = Path(path)
    if path.is_symlink() or is_metadata_file(path):
        raise ValueError("Invalid firmware/algorithm destination")
    with path.open("wb") as stream:
        stream.write(payload)
        sync_file(stream)
    sync_directory(path.parent)
    if sha256_file(path) != hashlib.sha256(payload).hexdigest():
        raise OSError("Probe-volume file readback does not match source")


def copy_verified(source: str | Path, destination: str | Path) -> bool:
    """Return True when copied, False only when all bytes already match."""
    source, destination = Path(source), Path(destination)
    if is_metadata_file(source) or is_metadata_file(destination) or destination.is_symlink():
        raise ValueError("Invalid firmware/algorithm file")
    before = source_fingerprint(source)
    payload = source.read_bytes()
    if hashlib.sha256(payload).hexdigest() != before["sha256"]:
        raise OSError("Source changed while preparing probe-volume copy")
    if destination.is_file() and sha256_file(destination) == before["sha256"]:
        return False
    write_verified(destination, payload)
    return True
