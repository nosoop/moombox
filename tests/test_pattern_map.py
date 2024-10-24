#!/usr/bin/python3

import re

import pytest
from moombox.config import PatternMap
from moombox.feed_monitor import get_pattern_matches


@pytest.mark.parametrize(
    "input, expected",
    [
        # ensure exotic unicode gets substituted with ASCII
        (
            "【UNARCHIVE KARAOKE】TO ALL THE LONELY BUT LOVELY AXELOTLS OUT THERE♡【𝐑𝐄𝐁𝐑𝐎𝐀𝐃𝐂𝐀𝐒𝐓】",
            (
                "rebroadcast",
                "karaoke",
                "unarchived",
            ),
        ),
        # spaces used in between single characters
        (
            "【K A R A O K E】rock",
            ("karaoke",),
        ),
    ],
)
def test_patterns(input: str, expected: tuple):
    test_pattern: PatternMap = {
        "unarchived": re.compile(r"(?i)(\W|^)unar?chived?"),
        "karaoke": re.compile(r"(?i)(\W|^)karaoke"),
        "rebroadcast": re.compile(r"(?i)(\W|^)re-?broadcast"),
    }
    assert set(expected) == get_pattern_matches(test_pattern, input)
