"""Uploaded-file helpers shared by the two ingest paths.

``hash_upload`` was ``results.ingest._hash_upload``, where it hashed result bundles.
The manual submit form needed the same thing for ``TestResultAttachment.sha256`` (a
column that had existed all along with no writer anywhere: the create call passed
``submission`` and ``file`` and nothing else, the admin marked it readonly, and
approval did not backfill it, so every attachment ever uploaded stored an empty
digest). Copying five lines would have been easy and wrong; this is the second
caller, so it moves here.
"""
from __future__ import annotations

import hashlib
from pathlib import PurePosixPath

from django.conf import settings
from django.core.exceptions import ValidationError

# One mebibyte. Big enough that a 4 GiB bundle is not thirty thousand reads, small
# enough that a chunk is never a problem to hold.
_CHUNK = 1024 * 1024


def hash_upload(upload) -> str:
    """The SHA-256 of an uploaded file, read in chunks.

    Chunked because an ``UploadedFile`` may be a multi-gigabyte bundle spooled to
    disk, and ``.read()`` with no argument would pull all of it into memory.

    Seeks to 0 both before and after. The leading seek is load-bearing: a caller cannot
    know whether something upstream has already consumed the stream, and a digest taken
    from the middle of a file is silently wrong rather than obviously wrong.

    The trailing one is defensive, and deliberately not claimed as more than that.
    Every current consumer re-seeks for itself before reading - ``ingest._extract``
    opens with ``bundle_file.seek(0)`` and Django's ``File.chunks()`` starts with the
    same - so removing it changes no observable behaviour today, which was checked
    rather than assumed. It stays because "hash it" should not leave a stream consumed
    for the next reader, and the failure it prevents (a file that saves as zero bytes
    while its digest looks perfectly correct) is one that would be found in production
    rather than in review.
    """
    digest = hashlib.sha256()
    upload.seek(0)
    for chunk in iter(lambda: upload.read(_CHUNK), b""):
        digest.update(chunk)
    upload.seek(0)
    return digest.hexdigest()


# --- evidence uploads ----------------------------------------------------------
#
# What a submitter may attach to a manual hardware submission. The field was a bare
# ``FileField(required=False)`` with no validators of any kind, so an ``.html``, an
# ``.svg``, or an ``MZ``-headered ``.exe`` all stored happily and were then served from
# the application's own origin, linked from the reviewer's page.
#
# This is the narrower half of a pair. The nginx ``/media/`` block forces
# ``Content-Disposition: attachment`` and ``nosniff``, which is what actually
# neutralizes a hostile upload and does not depend on an allowlist being complete.
# This list keeps obviously wrong things out of the store in the first place and gives
# the submitter an error at upload time rather than a reviewer a surprise later.
#
# Not an allowlist of *content*: nothing here opens the file. A declared submission
# has no manifest to verify against, which is exactly why its tier is capped at
# community rather than these validators being asked to carry weight they cannot.

EVIDENCE_EXTENSIONS = frozenset({
    # Text the reviewer will actually read.
    ".txt", ".log", ".out", ".md", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml",
    ".ini", ".conf", ".cfg",
    # Documents. A vendor's own test report is usually a PDF.
    ".pdf",
    # Screenshots. Deliberately no ``.svg``: it is a document that can carry script,
    # not an image, and it is the single most common way an "image upload" becomes XSS.
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
    # Collections of the above. Not inspected, just stored.
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".zst", ".7z",
})

# Attachments are logs and screenshots, not result bundles - those go through
# ``POST /api/v1/results/`` and its own much larger ceiling. Overridable because the
# right number is a deployment question.
DEFAULT_MAX_EVIDENCE_MB = 25


def max_evidence_bytes() -> int:
    return int(
        getattr(settings, "LUMINA_MAX_EVIDENCE_MB", DEFAULT_MAX_EVIDENCE_MB)
    ) * 1024 * 1024


def validate_evidence_file(upload) -> None:
    """Reject an attachment this application should not be storing.

    Raises ``ValidationError``, so it works as a form-field validator and produces a
    message next to the field rather than a 500.

    The extension checked is the **last** one, because that is what nginx maps to a
    content type and therefore what a browser would act on. ``notes.exe.txt`` is a
    text file as far as anything serving it is concerned, so it passes; the reverse,
    ``notes.txt.exe``, does not.
    """
    name = getattr(upload, "name", "") or ""
    suffix = PurePosixPath(name).suffix.lower()
    if not suffix:
        raise ValidationError(
            "Attachments need a file extension so the type is unambiguous. "
            "Rename it to end in .txt or .log if it is plain text."
        )
    if suffix not in EVIDENCE_EXTENSIONS:
        raise ValidationError(
            f"{suffix} files cannot be attached. Allowed: logs and text "
            "(.txt, .log, .json, .csv, .xml), documents (.pdf), screenshots "
            "(.png, .jpg, .gif, .webp), and archives of those (.zip, .tar.gz, .zst)."
        )
    size = getattr(upload, "size", None)
    limit = max_evidence_bytes()
    if size is not None and size > limit:
        raise ValidationError(
            f"That file is {size // (1024 * 1024)} MB. Attachments are limited to "
            f"{limit // (1024 * 1024)} MB each - upload a validation run instead if "
            "you have full test output."
        )
