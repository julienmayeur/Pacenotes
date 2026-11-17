from pacenote.transcription.srt import format_clock, format_timestamp, parse_srt, parse_timestamp

SAMPLE = """1
00:00:01,200 --> 00:00:04,500
Bonjour, merci
de prendre ce temps.

2
00:01:02,050 --> 00:01:05,000
Deuxième ligne.
"""


def test_timestamps_roundtrip():
    assert parse_timestamp("00:01:02,050") == 62.05
    assert parse_timestamp("01:00:00.5") == 3600.5
    assert format_timestamp(62.05) == "00:01:02,050"
    assert format_clock(62.9) == "01:02"


def test_parse_srt_joins_multiline_text_and_handles_crlf_and_bom():
    cues = parse_srt("﻿" + SAMPLE.replace("\n", "\r\n"))
    assert [c.index for c in cues] == [1, 2]
    assert cues[0].text == "Bonjour, merci de prendre ce temps."
    assert cues[1].start == 62.05


def test_parse_srt_skips_malformed_blocks():
    cues = parse_srt("garbage\n\n3\nnot a time --> nope\ntext\n\n" + SAMPLE)
    assert len(cues) == 2
