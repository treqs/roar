"""The get download/upload spinner status-line formatters."""

from __future__ import annotations

from roar.application.get.transfer import _download_progress_message
from roar.application.publish.put_execution import _upload_progress_message


def test_download_progress_message():
    assert _download_progress_message(0, 100, 0) == "Downloading 0/100 files · 0.00 GB"
    assert _download_progress_message(47, 100, 4_310_000_000) == "Downloading 47/100 files · 4.31 GB"


def test_upload_progress_message():
    assert _upload_progress_message(0, 100, 0) == "Uploading 0/100 files · 0.00 GB"
    assert _upload_progress_message(12, 100, 1_100_000_000) == "Uploading 12/100 files · 1.10 GB"
