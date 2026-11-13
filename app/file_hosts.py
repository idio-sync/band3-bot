"""Which uploads band3 can download: a port of band3's src/Net/file_hosts.cpp HostOf.

RhythmVerse's own files; files (never folders) on MediaFire, Google Drive and
Dropbox; dingorb songs; bit.ly and tinyurl links. Never mega.nz or an archive.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _split(url: str):
    """(host without www., path, query) of an http(s) URL; None for anything else."""
    scheme, sep, _ = url.partition("://")
    if not sep or scheme.lower() not in ("http", "https"):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return None
    return host, parts.path, parts.query


def _is_archive(path: str) -> bool:
    lower = path.lower()
    # MediaFire's newer links end in the name then /file
    if lower.endswith("/file"):
        lower = lower[:-5]
    return lower.endswith((".zip", ".rar", ".7z"))


def _param(query: str, name: str) -> str:
    return (parse_qs(query, keep_blank_values=True).get(name) or [""])[0]


def drive_file_id(url: str) -> str:
    parts = _split(url)
    if not parts:
        return ""
    _, path, query = parts
    if "/folders/" in path:
        return ""
    at = path.find("/file/d/")
    if at >= 0:
        file_id = path[at + 8:].split("/")[0]
        return file_id if _ID.fullmatch(file_id) else ""
    file_id = _param(query, "id")
    return file_id if _ID.fullmatch(file_id) else ""


def host_of(url: str) -> str:
    """The kind of host band3 downloads the URL from, or "" for none it can."""
    parts = _split(url)
    if not parts:
        return ""
    host, path, query = parts
    if _is_archive(path):
        return ""
    if host == "rhythmverse.co":
        return "rhythmverse" if len(path) > 1 else ""
    if host == "mediafire.com":
        if path.startswith(("/file/", "/download/", "/file_premium/")):
            return "mediafire"
        # the old links: mediafire.com/?<key>
        return "mediafire" if path in ("", "/") and query and "=" not in query else ""
    if host in ("drive.google.com", "docs.google.com", "drive.usercontent.google.com"):
        return "drive" if drive_file_id(url) else ""
    if host in ("dropbox.com", "dl.dropboxusercontent.com", "dl.dropbox.com"):
        return "dropbox" if path.startswith(("/s/", "/scl/fi/")) else ""
    if host == "dingorb.com":
        return "dingo" if _ID.fullmatch(_param(query, "id")) else ""
    if host in ("bit.ly", "tinyurl.com"):
        return "shortlink" if len(path) > 1 else ""
    return ""


def can_download(url: str) -> bool:
    return bool(host_of(url))
