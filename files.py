"""Read-only file browser over a few configured roots (config.BROWSE_ROOTS,
default /tmp plus FLEET_ROOT) so artifacts that instances leave behind can be
listed, viewed in the browser and downloaded (single files, or a folder as a
zip).

Every path is resolved with realpath and must land inside one of the roots, so
`..` and symlinks pointing elsewhere are refused rather than followed. The
secrets dir and the SQLite DB are refused even if a root happens to contain
them. URLs carry the absolute path without its leading slash, e.g.
/files/browse/tmp/report -- path-shaped rather than a query param so an HTML
artifact's relative links (style.css, img/x.png) resolve to its siblings.
"""
import mimetypes
import os
import stat
import tempfile
import zipfile
from datetime import datetime
from urllib.parse import quote

import config


class FileAccessError(Exception):
    """Path is outside the browsable roots (or is a protected file)."""


def roots() -> list[str]:
    """Configured roots that exist, as resolved directories."""
    out = []
    for r in config.BROWSE_ROOTS:
        real = os.path.realpath(r)
        if os.path.isdir(real) and real not in out:
            out.append(real)
    return out


def _within(path: str, parent: str) -> bool:
    return path == parent or path.startswith(parent.rstrip(os.sep) + os.sep)


def _protected(real: str) -> bool:
    secrets = os.path.realpath(config.SECRETS_ROOT)
    db_path = os.path.realpath(config.DB_PATH)
    # SQLite keeps -wal/-shm/-journal files next to the DB.
    return _within(real, secrets) or real.startswith(db_path)


def resolve(path: str) -> str:
    """Map a URL path (absolute path minus its leading slash) to a real path
    inside a browsable root, or raise FileAccessError."""
    real = os.path.realpath("/" + path.lstrip("/"))
    if _protected(real) or not any(_within(real, r) for r in roots()):
        raise FileAccessError("That path is outside the browsable folders.")
    if not os.path.exists(real):
        raise FileNotFoundError(real)
    return real


def url(kind: str, real: str) -> str:
    """URL for browse/view/download/zip of an already-resolved path."""
    return f"/files/{kind}/" + quote(real.lstrip("/"))


def browse_url(path: str | None) -> str | None:
    """Browse URL for `path` if it's inside a root (for linking from other
    pages, e.g. an instance's working tree); None otherwise."""
    if not path:
        return None
    try:
        real = resolve(path)
    except (FileAccessError, OSError):
        return None
    return url("browse", real) if os.path.isdir(real) else None


def human_size(n: int | None) -> str:
    if n is None:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def listing(real: str, sort: str = "name") -> list[dict]:
    """Entries of directory `real`, folders first. sort='mtime' puts the most
    recently modified first (handy for /tmp, where new output is what you're
    after)."""
    entries = []
    with os.scandir(real) as it:
        for e in it:
            full = os.path.join(real, e.name)
            # A symlink pointing outside the roots is listed but inert: no
            # stat (its size/mtime would leak info about the target) or links.
            outside = False
            if e.is_symlink():
                try:
                    resolve(full)
                except (FileAccessError, OSError):
                    outside = True
            try:
                if outside:
                    raise OSError
                st = e.stat()  # follows symlinks
                is_dir = stat.S_ISDIR(st.st_mode)
                size, mtime = (None if is_dir else st.st_size), st.st_mtime
            except OSError:  # outside, broken symlink, permission denied, vanished
                is_dir, size, mtime = False, None, None
            entries.append({
                "name": e.name,
                "is_dir": is_dir,
                "is_link": e.is_symlink(),
                "outside": outside,
                "size": size,
                "mtime": mtime,
                "modified": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M") if mtime else "",
                "browse_url": url("browse", full),
                "view_url": url("view", full),
                "download_url": url("download", full),
                "zip_url": url("zip", full),
            })
    if sort == "mtime":
        entries.sort(key=lambda x: x["mtime"] or 0, reverse=True)
    else:
        entries.sort(key=lambda x: x["name"].lower())
    entries.sort(key=lambda x: not x["is_dir"])  # stable: folders first
    return entries


def breadcrumbs(real: str) -> list[dict]:
    """Crumbs from the (innermost) root containing `real` down to `real`."""
    root = max((r for r in roots() if _within(real, r)), key=len)
    crumbs = [{"name": root, "url": url("browse", root)}]
    rel = os.path.relpath(real, root)
    if rel != ".":
        cur = root
        for part in rel.split(os.sep):
            cur = os.path.join(cur, part)
            crumbs.append({"name": part, "url": url("browse", cur)})
    return crumbs


def parent_url(real: str) -> str | None:
    if real in roots():
        return None
    return url("browse", os.path.dirname(real))


_TEXT_PASSTHROUGH = {"text/html", "text/css", "text/javascript"}


def _looks_text(real: str) -> bool:
    try:
        with open(real, "rb") as fh:
            chunk = fh.read(8192)
    except OSError:
        return False
    if b"\0" in chunk:
        return False
    try:
        chunk.decode("utf-8")
    except UnicodeDecodeError as exc:
        # A multi-byte char cut off at the chunk boundary is still text.
        return exc.start >= len(chunk) - 3
    return True


def view_type(real: str) -> str:
    """Content-Type for viewing in the browser. Types browsers would render
    oddly or download (text/markdown, text/x-python, unknown extensions) are
    shown as plain text when the content is text."""
    mime, _ = mimetypes.guess_type(real)
    if mime in _TEXT_PASSTHROUGH:
        return f"{mime}; charset=utf-8"
    if mime and not mime.startswith("text/") and mime != "application/octet-stream":
        return mime
    if _looks_text(real):
        return "text/plain; charset=utf-8"
    return mime or "application/octet-stream"


def zip_dir(real: str):
    """Zip directory `real` (top-level folder name kept inside the archive)
    into a spooled temp file and return it rewound. Symlinks are only
    followed when they stay inside the browsable roots; unreadable files are
    skipped."""
    spool = tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024)
    base = os.path.dirname(real)
    with zipfile.ZipFile(spool, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, filenames in os.walk(real):
            # os.walk doesn't descend into symlinked dirs by default; keep it so.
            for name in filenames:
                full = os.path.join(dirpath, name)
                try:
                    target = resolve(full)
                    if not os.path.isfile(target):
                        continue
                    zf.write(target, os.path.relpath(full, base))
                except (FileAccessError, OSError):
                    continue
    spool.seek(0)
    return spool
