#!/usr/bin/python3

import moombox.extractor
import pytest


@pytest.mark.parametrize(
    "expected,input",
    [
        ("dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("dQw4w9WgXcQ", "https://youtu.be/dQw4w9WgXcQ"),
        ("dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
        ("dQw4w9WgXcQ", "https://www.youtube.com/live/dQw4w9WgXcQ"),
        ("dQw4w9WgXcQ", "https://www.youtube.com/shorts/dQw4w9WgXcQ"),
    ],
)
def test_video_id_extraction(input: str, expected: str):
    assert expected == moombox.extractor.extract_video_id_from_string(input)
