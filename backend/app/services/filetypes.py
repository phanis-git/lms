"""File type allow-lists and signature (magic number) checks."""

import os

from app.models.enums import MediaKind

VIDEO = {".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime"}
IMAGE = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}
BINARY_DOCS = {
    ".pdf": "application/pdf",
    ".zip": "application/zip",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
TEXT_DOCS = dict.fromkeys(
    [
        ".txt",
        ".md",
        ".py",
        ".sh",
        ".yml",
        ".yaml",
        ".json",
        ".tf",
        ".java",
        ".sql",
        ".js",
        ".ts",
        ".cfg",
        ".conf",
        ".ini",
        ".xml",
        ".html",
        ".css",
        ".csv",
        ".properties",
        ".groovy",
        ".dockerfile",
    ],
    "text/plain",
)
DOCS = {**BINARY_DOCS, **TEXT_DOCS, **IMAGE}


def resolve_type(filename: str, kind: MediaKind) -> str | None:
    name = filename.lower()
    ext = os.path.splitext(name)[1] or (".dockerfile" if name in ("dockerfile", "jenkinsfile") else "")
    table = {MediaKind.VIDEO: VIDEO, MediaKind.IMAGE: IMAGE, MediaKind.DOCUMENT: DOCS}[kind]
    return table.get(ext)


def signature_ok(content_type: str, head: bytes) -> bool:
    if content_type in ("video/mp4", "video/quicktime"):
        return len(head) >= 12 and head[4:8] in (b"ftyp", b"moov", b"mdat", b"wide", b"free")
    if content_type == "video/webm":
        return head.startswith(b"\x1a\x45\xdf\xa3")
    if content_type == "image/png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/jpeg":
        return head.startswith(b"\xff\xd8\xff")
    if content_type == "image/gif":
        return head.startswith((b"GIF87a", b"GIF89a"))
    if content_type == "image/webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if content_type == "application/pdf":
        return head.startswith(b"%PDF")
    if content_type.startswith("application/"):
        return head.startswith(b"PK\x03\x04")
    if content_type == "text/plain":
        if b"\x00" in head:
            return False
        # The 64-byte window may cut a multi-byte character, so allow a truncated tail.
        for cut in range(4):
            try:
                head[: len(head) - cut].decode("utf-8")
                return True
            except UnicodeDecodeError:
                continue
        return False
    return False
