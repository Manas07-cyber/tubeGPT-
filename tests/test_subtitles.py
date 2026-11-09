"""SRT / VTT / panel parsing with realistic caption samples."""
import pytest

VID = "dQw4w9WgXcQ"


def parse(tr, text, warnings=None):
    segs, source = tr.parse_transcript_text(text, warnings)
    # Whatever the parser produces must be a document the loader accepts (round-trip invariant).
    tr.validate_doc(tr.make_doc(segs, source=source, video_id=VID))
    return segs


def texts(segs):
    return [s["text"] for s in segs]


# ---------------------------------------------------------------- SRT basics
def test_srt_with_identifiers_multiline_cues_and_crlf(tr):
    srt = ("1\r\n00:00:01,000 --> 00:00:03,000\r\nWelcome to the lecture.\r\nToday: graphs.\r\n\r\n"
           "2\r\n00:00:03,500 --> 00:00:06,000\r\nLet's begin.\r\n")
    segs = parse(tr, srt)
    assert texts(segs) == ["Welcome to the lecture. Today: graphs.", "Let's begin."]
    assert segs[0]["start"] == 1.0 and segs[1]["end"] == 6.0


def test_srt_without_identifiers(tr):
    segs = parse(tr, "00:00:01,000 --> 00:00:02,000\nOne\n\n00:00:02,000 --> 00:00:03,000\nTwo\n")
    assert texts(segs) == ["One", "Two"]


def test_srt_missing_blank_lines_does_not_leak_identifiers_into_text(tr):
    srt = "1\n00:00:01,000 --> 00:00:02,000\nFirst line\n2\n00:00:02,000 --> 00:00:03,000\nSecond line\n3\n00:00:03,000 --> 00:00:04,000\nThird\n"
    assert texts(parse(tr, srt)) == ["First line", "Second line", "Third"]


def test_numbers_inside_dialogue_are_not_mistaken_for_identifiers(tr):
    srt = "1\n00:00:01,000 --> 00:00:02,000\nThe answer is\n42\n\n2\n00:00:02,500 --> 00:00:03,000\nYes.\n"
    assert texts(parse(tr, srt)) == ["The answer is 42", "Yes."]


def test_bom_and_leading_blank_lines(tr):
    assert texts(parse(tr, "﻿\n\n1\n00:00:01,000 --> 00:00:02,000\nHi\n")) == ["Hi"]


@pytest.mark.parametrize("start,end,s,e", [
    ("00:00:01.5", "00:00:02,25", 1.5, 2.25), ("00:00:01,5", "00:00:02.250", 1.5, 2.25),
    ("00:01.000", "00:02.500", 1.0, 2.5), ("1:02:03,456", "1:02:04,000", 3723.456, 3724.0),
    ("100:00:00.000", "100:00:01.000", 360000.0, 360001.0),
])
def test_timestamp_variants(tr, start, end, s, e):
    segs = parse(tr, f"{start} --> {end}\nHello\n")
    assert segs[0]["start"] == pytest.approx(s) and segs[0]["end"] == pytest.approx(e)


# ---------------------------------------------------------------- malformed or odd timing
def test_cues_with_impossible_clock_values_are_skipped_and_reported(tr):
    srt = ("1\n00:00:01,000 --> 00:00:02,000\nGood\n\n"
           "2\n00:75:99,000 --> 00:76:00,000\nBad minutes\n\n"
           "3\n00:00:75,000 --> 00:00:76,000\nBad seconds\n\n"
           "4\n00:00:05,000 --> 00:00:06,000\nAlso good\n")
    warnings = []
    segs = parse(tr, srt, warnings)
    assert texts(segs) == ["Good", "Also good"]
    assert warnings == ["Skipped 2 caption cue(s) with malformed timestamps."]


def test_end_before_start_is_corrected_and_reported(tr):
    warnings = []
    segs = parse(tr, "00:00:05,000 --> 00:00:03,000\nBackwards\n", warnings)
    assert segs[0]["start"] == segs[0]["end"] == 5.0 and "corrected" in warnings[0]


def test_out_of_order_cues_are_sorted_stably_by_start(tr):
    srt = ("00:00:10,000 --> 00:00:11,000\nThird\n\n00:00:01,000 --> 00:00:02,000\nFirst\n\n"
           "00:00:01,000 --> 00:00:02,500\nFirst B\n\n00:00:05,000 --> 00:00:06,000\nSecond\n")
    warnings = []
    segs = parse(tr, srt, warnings)
    assert texts(segs) == ["First", "First B", "Second", "Third"] and warnings


def test_overlapping_captions_from_two_speakers_are_both_kept_in_time_order(tr):
    srt = "00:00:00,000 --> 00:00:05,000\nHello there.\n\n00:00:03,000 --> 00:00:06,000\nGeneral Kenobi.\n"
    segs = parse(tr, srt)
    assert texts(segs) == ["Hello there.", "General Kenobi."]


def test_empty_cues_are_skipped(tr):
    srt = "1\n00:00:01,000 --> 00:00:02,000\n\n2\n00:00:02,000 --> 00:00:03,000\n   \n\n3\n00:00:03,000 --> 00:00:04,000\n<i></i>\n\n4\n00:00:04,000 --> 00:00:05,000\nReal\n"
    assert texts(parse(tr, srt)) == ["Real"]


def test_exact_duplicate_cues_are_dropped(tr):
    srt = "00:00:01,000 --> 00:00:02,000\nSame\n\n00:00:01,000 --> 00:00:02,000\nSame\n"
    assert texts(parse(tr, srt)) == ["Same"]


# ---------------------------------------------------------------- repeated speech must survive
def test_repeated_single_line_cues_are_legitimate_speech(tr):
    srt = ("1\n00:00:01,000 --> 00:00:02,000\nNo.\n\n2\n00:00:02,100 --> 00:00:03,000\nNo.\n\n"
           "3\n00:00:03,100 --> 00:00:04,000\nNo way!\n")
    assert texts(parse(tr, srt)) == ["No.", "No.", "No way!"]


def test_repeated_words_inside_one_cue_are_untouched(tr):
    assert texts(parse(tr, "00:00:01,000 --> 00:00:03,000\nI said no, no, no, no.\n")) == ["I said no, no, no, no."]


def test_same_sentence_said_again_later_is_kept(tr):
    srt = ("00:00:01,000 --> 00:00:02,000\nThank you.\n\n00:01:30,000 --> 00:01:31,000\nIt works.\n\n"
           "00:03:00,000 --> 00:03:01,000\nThank you.\n")
    assert texts(parse(tr, srt)) == ["Thank you.", "It works.", "Thank you."]


def test_repeat_after_a_pause_is_kept_even_with_two_lines(tr):
    srt = "00:00:00,000 --> 00:00:02,000\nA\nB\n\n00:00:05,000 --> 00:00:07,000\nB\nC\n"
    assert texts(parse(tr, srt)) == ["A B", "B C"]


# ---------------------------------------------------------------- rolling auto-captions
ROLLING_VTT = """WEBVTT
Kind: captions
Language: en

00:00:00.160 --> 00:00:02.230 align:start position:0%
 
hello<00:00:00.480><c> everyone</c><00:00:01.040><c> welcome</c>

00:00:02.230 --> 00:00:02.240 align:start position:0%
hello everyone welcome
 

00:00:02.240 --> 00:00:04.710 align:start position:0%
hello everyone welcome
to<00:00:02.480><c> the</c><00:00:02.800><c> channel</c>

00:00:04.710 --> 00:00:04.720 align:start position:0%
to the channel
 

00:00:04.720 --> 00:00:07.000 align:start position:0%
to the channel
today<00:00:05.000><c> we</c><00:00:05.200><c> cover</c><00:00:05.680><c> timestamps</c>

00:00:07.000 --> 00:00:07.010 align:start position:0%
today we cover timestamps
 
"""


def test_youtube_rolling_auto_captions_become_clean_lines(tr):
    segs = parse(tr, ROLLING_VTT)
    assert texts(segs) == ["hello everyone welcome", "to the channel", "today we cover timestamps"]
    assert [s["start"] for s in segs] == [0.16, 2.24, 4.72]


def test_three_line_rolling_windows(tr):
    vtt = ("WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nA\nB\nC\n\n00:00:02.000 --> 00:00:04.000\nB\nC\nD\n\n"
           "00:00:04.000 --> 00:00:06.000\nC\nD\nE\n")
    assert texts(parse(tr, vtt)) == ["A B C", "D", "E"]


def test_window_that_only_repeats_previous_lines_is_dropped(tr):
    vtt = "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nA\nB\n\n00:00:02.000 --> 00:00:03.000\nB\n"
    assert texts(parse(tr, vtt)) == ["A B"]


# ---------------------------------------------------------------- VTT specifics
VTT = """WEBVTT - lecture captions

NOTE This comment is not a cue.
It spans two lines.

STYLE
::cue { color: white }

REGION
id:r1
width:40%

intro
00:00:01.000 --> 00:00:03.000 align:start position:10% line:90%
<v Professor>Welcome to <b>discrete</b> mathematics.</v>

2
00:03.500 --> 00:06.000
Today: <c.yellow>finite state machines</c>.

00:01:05.250 --> 00:01:09.000 region:r1
Done.
"""


def test_vtt_header_notes_style_regions_settings_and_identifiers(tr):
    segs = parse(tr, VTT)
    assert texts(segs) == ["Welcome to discrete mathematics.", "Today: finite state machines.", "Done."]
    assert [s["start"] for s in segs] == [1.0, 3.5, 65.25]


# ---------------------------------------------------------------- text cleaning
def test_html_formatting_is_stripped(tr):
    srt = ('00:00:01,000 --> 00:00:02,000\n<i>italic</i> <b>bold</b> <u>under</u> '
           '<font color="#ffff00">yellow</font> <c.red>red</c> <00:00:01.500>tick <br/>end\n')
    assert texts(parse(tr, srt)) == ["italic bold under yellow red tick end"]


@pytest.mark.parametrize("raw,expected", [
    ("Tom &amp; Jerry", "Tom & Jerry"), ("5 &lt; 6 &gt; 4", "5 < 6 > 4"), ("say &quot;hi&quot;", 'say "hi"'),
    ("it&#39;s", "it's"), ("a&nbsp;b", "a b"), ("&lt;b&gt;literal&lt;/b&gt;", "<b>literal</b>"),
])
def test_html_entities_are_decoded_once(tr, raw, expected):
    assert texts(parse(tr, f"00:00:01,000 --> 00:00:02,000\n{raw}\n")) == [expected]


@pytest.mark.parametrize("spoken", ["if a < b and c > d then", "5<6 and 7>3", "x <= y >= z", "use -> and <- arrows"])
def test_plain_angle_brackets_in_speech_survive(tr, spoken):
    assert texts(parse(tr, f"00:00:01,000 --> 00:00:02,000\n{spoken}\n")) == [spoken]


def test_unicode_scripts_emoji_and_joiners_are_preserved(tr):
    text = "नमस्ते दुनिया ನಮಸ್ಕಾರ 😀 क्‍ष"      # includes a zero-width joiner, needed by Indic scripts
    assert texts(parse(tr, f"00:00:01,000 --> 00:00:02,000\n{text}\n")) == [text]


def test_unexpected_whitespace_and_invisible_characters_are_normalised(tr):
    raw = "  Hello \t  world​\xa0 again  "
    assert texts(parse(tr, f"00:00:01,000 --> 00:00:02,000\n{raw}\n")) == ["Hello world again"]


def test_unicode_line_separator_inside_a_cue_does_not_split_it(tr):
    srt = "00:00:01,000 --> 00:00:02,000\nline one line two\n\n00:00:02,000 --> 00:00:03,000\nnext\n"
    assert texts(parse(tr, srt)) == ["line one line two", "next"]


# ---------------------------------------------------------------- panel text and plain text
def test_panel_text_with_impossible_times_treats_them_as_words(tr):
    panel = "0:00 Intro\n0:15 Next\n12:75 pm is later\n1:00 End\n"
    segs = parse(tr, panel)
    assert [(s["start"], s["text"]) for s in segs] == [(0.0, "Intro"), (15.0, "Next 12:75 pm is later"), (60.0, "End")]


def test_panel_with_crlf_and_unicode_separators(tr):
    segs = parse(tr, "0:00\r\nनमस्ते\r\n0:05\r\nदुनिया\r\n")
    assert texts(segs) == ["नमस्ते", "दुनिया"]


def test_plain_text_keeps_every_line_in_order(tr):
    segs = parse(tr, "alpha\n\n  beta  \ngamma\r\ndelta")
    assert texts(segs) == ["alpha", "beta", "gamma", "delta"] and all(s["start"] is None for s in segs)


# ---------------------------------------------------------------- reporting through the CLI
def test_cli_reports_skipped_cues_on_stderr_but_still_saves_the_rest(run, tmp_path):
    f = tmp_path / "a.srt"
    f.write_text("00:00:01,000 --> 00:00:02,000\nGood\n\n00:00:75,000 --> 00:00:76,000\nBad\n", encoding="utf-8")
    code, out, err = run("parse", str(f), "--out", str(tmp_path / "o.json"))
    assert code == 0 and "WARNING: Skipped 1 caption cue(s) with malformed timestamps." in err
    assert "segments: 1" in out


def test_large_subtitle_file_parses_in_reasonable_time(tr):
    cues = "".join(f"{i}\n00:{i // 60 % 60:02d}:{i % 60:02d},000 --> 00:{i // 60 % 60:02d}:{i % 60:02d},900\n"
                   f"line number {i}\n\n" for i in range(20_000))
    segs, source = tr.parse_transcript_text(cues)
    assert len(segs) > 10_000 and "caption file" in source
