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
            {
                "rebroadcast",
                "karaoke",
                "unarchived",
            },
        ),
        # spaces used in between single characters
        (
            "【K A R A O K E】rock",
            {
                "karaoke",
            },
        ),
        # Zalgo text, encoded to Python unicode literals using "unicode_escape" encoding
        (
            (
                "\u3010"
                "U\u0337\u030d\u0300\u0301\u0309\u0344\u030c\u0343\u0351\u0358\u0307\u0358\u0315\u030a\u0318\u0333\u0345\u0359"
                "N\u0338\u0313\u0352\u0301\u035d\u0323\u0323\u0329\u0339\u031c\u0356\u0330\u032b\u0319\u0324\u0329\u031f"
                "A\u0338\u0305\u0346\u030d\u0308\u0350\u0312\u0312\u0357\u0302\u034c\u031f\u0354\u031d\u0330\u031e\u0326\u032f\u0347\u0322"
                "R\u0334\u030e\u0323"
                "C\u0334\u031a\u0351\u035d\u034a\u0305\u0300\u0312\u0305\u0312\u0345\u031c\u0316\u0318"
                "H\u0335\u034a\u0350\u0307\u0341\u0358\u0314\u0304\u0352\u034c\u0313\u0352\u0349\u032a\u0339"
                "I\u0336\u0342\u034b\u0351\u033f\u033f\u0328\u0328\u0316\u032e\u0329\u032b\u0355\u033c\u033a\u0325\u0345\u031d\u034e\u0347"
                "V\u0334\u0308\u0344\u035d\u035d\u0357\u0307\u030b\u0308\u030a\u0306\u0344\u0303\u0315\u0313\u0313\u0323"
                "E\u0334\u0358\u0306\u033d\u0342\u033e\u0319\u0354\u032c\u0316\u035a\u0324\u0321\u0327\u033b\u0320\u031d\u034d\u0326\u032e"
                "D\u0334\u033d\u035b\u035d\u0310\u0352\u030e\u033a\u0355\u032d\u032e\u0348"
                " \u0338\u030a\u0346\u033e\u0312\u034a\u030a\u0301\u034b\u0344\u0301\u0306\u0309\u034b\u031b\u0358\u033b\u0355\u032b\u031d\u032a\u032c\u0332"
                "K\u0336\u0343\u0308\u0313\u030d\u0308\u033f\u030e\u031b\u0341\u0304\u034b\u0308\u034c\u0342\u0313\u0321\u0317\u035c\u0332\u0326\u032e\u032c\u0356\u0347\u032e"
                "A\u0338\u0340\u031a\u031b\u033d\u0352\u0357\u0309\u030d\u0308\u030f\u0321\u0333\u0353\u0345\u0332\u034d\u0316\u0324\u0339\u035c"
                "R\u0334\u0315\u033e\u033d\u0351\u034b\u030a\u033f\u0310\u030e\u0304\u0306\u030e\u033e\u0318\u0317\u0326\u0359\u0348\u032e\u035c\u0324"
                "A\u0337\u030d\u035d\u0313\u0306\u0313\u030f\u0360\u033d\u0340\u0346\u0308\u0312\u033c\u0329\u032f\u0356\u0330\u0317\u0353\u0329\u0320\u0356\u031d\u0332"
                "O\u0337\u0303\u0307\u0309\u0357\u0358\u031b\u0344\u0312\u033e\u0330\u0353\u0333\u031e\u0359\u0339\u032f\u0332\u0348\u035c\u0328\u0348\u0329"
                "K\u0335\u0311\u034c\u033e\u0344\u0300\u033c\u032c"
                "E\u0335\u033f\u0300\u0344\u0304\u0312\u031b\u0350\u0303\u030e\u030e\u0343\u0316\u034d\u031e\u0347\u033b\u0316\u0355\u033c\u0339\u035a\u035c\u0324\u032d\u031f\u0359"
                "\u3011 SPOOKY SCARY"
            ),
            {
                "karaoke",
                "unarchived",
            },
        ),
    ],
)
def test_patterns(input: str, expected: set):
    test_pattern: PatternMap = {
        "unarchived": re.compile(r"(?i)(\W|^)unar?chived?"),
        "karaoke": re.compile(r"(?i)(\W|^)karaoke"),
        "rebroadcast": re.compile(r"(?i)(\W|^)re-?broadcast"),
    }
    assert expected == get_pattern_matches(test_pattern, input)
