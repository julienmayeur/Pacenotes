from pacenote.srt import SrtTailer, format_clock, format_timestamp, parse_srt, parse_timestamp

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


def test_tailer_only_emits_complete_cues(tmp_path):
    p = tmp_path / "fr.srt"
    t = SrtTailer(p)
    assert t.poll() == []  # file does not exist yet
    p.write_text("1\n00:00:01,000 --> 00:00:02,000\nPremière", encoding="utf-8")
    assert t.poll() == []  # cue may still be growing
    with p.open("a", encoding="utf-8") as fh:
        fh.write(" phrase.\n\n2\n00:00:03,000 --> 00:00:04,000\nSeco")
    cues = t.poll()
    assert [c.text for c in cues] == ["Première phrase."]
    with p.open("a", encoding="utf-8") as fh:
        fh.write("nde.\n\n")
    assert [c.text for c in t.poll()] == ["Seconde."]
    assert t.poll() == []


def test_tailer_handles_utf8_split_across_reads(tmp_path):
    p = tmp_path / "fr.srt"
    data = "1\n00:00:01,000 --> 00:00:02,000\nÉté réussi.\n\n".encode()
    split = data.index("É".encode()) + 1  # cut in the middle of a multibyte char
    p.write_bytes(data[:split])
    t = SrtTailer(p)
    assert t.poll() == []
    with p.open("ab") as fh:
        fh.write(data[split:])
    assert [c.text for c in t.poll()] == ["Été réussi."]


def test_tailer_final_flushes_last_unterminated_cue_and_dedupes(tmp_path):
    p = tmp_path / "en.srt"
    p.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n\n1\n00:00:01,000 --> 00:00:02,000\nHello\n\n"
                 "2\n00:00:03,000 --> 00:00:04,000\nLast one", encoding="utf-8")
    t = SrtTailer(p)
    assert [c.text for c in t.poll()] == ["Hello"]
    assert [c.text for c in t.poll(final=True)] == ["Last one"]
