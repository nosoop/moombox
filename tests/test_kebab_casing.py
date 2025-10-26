#!/usr/bin/python3

import pytest
from moombox.tasks import DownloadStatus


@pytest.mark.parametrize(
    "input,expected",
    [
        (DownloadStatus.UNKNOWN, "status--unknown"),
        (DownloadStatus.UNAVAILABLE, "status--unavailable"),
        (DownloadStatus.WAITING, "status--waiting"),
        (DownloadStatus.DOWNLOADING, "status--downloading"),
        (DownloadStatus.MUXING, "status--muxing"),
        (DownloadStatus.FINISHED, "status--finished"),
        (DownloadStatus.ERROR, "status--error"),
        (DownloadStatus.CANCELLED, "status--cancelled"),
    ],
)
def test_status_html_classname(input: DownloadStatus, expected: str):
    assert input.html_classname == expected
