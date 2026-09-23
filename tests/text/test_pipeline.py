"""Text pipeline, spec 8.2 (18.1 pipeline row)."""

import pytest

from anki_miner_game.models.constants import MAX_LINE_CHARS, TYPEWRITER_WINDOW_S
from anki_miner_game.models.lines import GameLine
from anki_miner_game.models.messages import LineReceived
from anki_miner_game.models.pipeline import (
    DROP_COUNTER,
    Accepted,
    Dropped,
    DropReason,
    PipelineResult,
    Replaced,
)
from anki_miner_game.models.profile import FilterSettings
from anki_miner_game.session.journal import Journal, LineRecord, read_journal, timed_lines
from anki_miner_game.text.pipeline import TextPipeline
from anki_miner_game.text.sources.websocket_source import parse_frame


def _pipe(*, speaker_strip: bool = True, typewriter_merge: bool = False) -> TextPipeline:
    return TextPipeline(FilterSettings(speaker_strip=speaker_strip, typewriter_merge=typewriter_merge))


def _msg(raw: str, t_mono: float = 10.0, source_id: str = "agent") -> LineReceived:
    return LineReceived(raw=raw, t_mono=t_mono, source_id=source_id)


def _text(result: PipelineResult) -> str:
    assert isinstance(result, Accepted), result
    return result.line.text


def _dropped(result: PipelineResult, reason: DropReason, counter: str) -> None:
    assert result == Dropped(reason)
    assert DROP_COUNTER[reason] == counter


# --- accepted line shape ---------------------------------------------------


def test_accepted_line_keeps_raw_t_mono_and_source():
    result = _pipe().process(_msg(" こんにちは\n", t_mono=12.5, source_id="textractor"))
    assert result == Accepted(GameLine(text="こんにちは", raw=" こんにちは\n", t_mono=12.5, source_id="textractor"))


# --- step 1: NFC -----------------------------------------------------------


def test_step1_nfc_composes_dakuten():
    decomposed = "が"  # か + combining dakuten
    assert _text(_pipe().process(_msg(decomposed))) == "が"


# --- step 2: control and zero-width characters ------------------------------


def test_step2_removes_cc_and_cf_characters():
    raw = "ab​c\u0007d﻿e‪f‍"
    assert _text(_pipe().process(_msg(raw))) == "abcdef"


def test_step2_removes_tab_and_carriage_return_but_keeps_newline():
    assert _text(_pipe().process(_msg("a\tb\r\nc"))) == "ab c"


def test_step2_line_of_only_zero_width_is_empty():
    _dropped(_pipe().process(_msg("​‍﻿")), DropReason.EMPTY, "no_letters")


def test_step2_removes_lone_surrogates():
    # A hooker that cuts a UTF-16 pair in half sends "\ud83d" in its JSON; json.loads keeps it.
    assert _text(_pipe().process(_msg(parse_frame('{"sentence": "abc\\ud83d"}')))) == "abc"
    _dropped(_pipe().process(_msg("\udfff")), DropReason.EMPTY, "no_letters")


def test_step2_accepted_text_can_be_journalled(tmp_path):
    result = _pipe().process(_msg(parse_frame('{"sentence": "こん\\ud83dにちは"}')))
    assert isinstance(result, Accepted)

    journal = Journal(tmp_path / "session.lines.jsonl")
    journal.append(LineRecord(offset_ms=0, text=result.line.text, source=result.line.source_id))
    journal.close()

    assert timed_lines(read_journal(tmp_path / "session.lines.jsonl"))[0].text == "こんにちは"


# --- step 3: whitespace ----------------------------------------------------


def test_step3_newlines_to_spaces_collapse_and_strip():
    assert _text(_pipe().process(_msg("  一行目\n\n二行目   三 \n"))) == "一行目 二行目 三"


def test_step3_collapses_ideographic_space():
    assert _text(_pipe().process(_msg("　あ　　い　"))) == "あ い"


# --- step 4: speaker strip -------------------------------------------------


def test_step4_strips_one_leading_speaker_group_and_whitespace():
    assert _text(_pipe().process(_msg("【太郎】　「行こう」"))) == "「行こう」"


def test_step4_strips_only_one_group():
    assert _text(_pipe().process(_msg("【太郎】【花子】はい"))) == "【花子】はい"


def test_step4_leaves_non_leading_group():
    assert _text(_pipe().process(_msg("はい【太郎】"))) == "はい【太郎】"


def test_step4_strips_empty_group():
    assert _text(_pipe().process(_msg("【】はい"))) == "はい"


def test_step4_leading_whitespace_before_group_still_strips():
    assert _text(_pipe().process(_msg("\n 【太郎】はい"))) == "はい"


def test_step4_unclosed_group_is_kept():
    assert _text(_pipe().process(_msg("【太郎はい"))) == "【太郎はい"


def test_step4_disabled_keeps_group():
    assert _text(_pipe(speaker_strip=False).process(_msg("【太郎】はい"))) == "【太郎】はい"


# --- step 4: colon-prefixed speaker (Agent hooker form) ---------------------


def test_step4_strips_colon_prefixed_speaker_before_kagi_quote():
    raw = "倫太郎: 「ああ、ドクター中鉢は抜け駆けをした。たっぷりとその考えについて聞かせてもらうつもりさ」"
    assert (
        _text(_pipe().process(_msg(raw)))
        == "「ああ、ドクター中鉢は抜け駆けをした。たっぷりとその考えについて聞かせてもらうつもりさ」"
    )


def test_step4_fullwidth_colon_prefixed_speaker_is_kept():
    # Agent emits an ASCII colon and one ASCII space; a full-width colon is Japanese prose
    # punctuation (narration, a label), never Agent's speaker prefix.
    assert _text(_pipe().process(_msg("太郎：「はい」"))) == "太郎：「はい」"


def test_step4_fullwidth_colon_narration_before_quote_is_kept():
    assert _text(_pipe().process(_msg("太郎はこう言った：「行くぞ」"))) == "太郎はこう言った：「行くぞ」"


def test_step4_fullwidth_colon_label_before_quote_is_kept():
    assert _text(_pipe().process(_msg("警告：「セーブを忘れずに」"))) == "警告：「セーブを忘れずに」"


def test_step4_strips_colon_prefixed_speaker_before_double_bracket_quote():
    assert _text(_pipe().process(_msg("太郎: 『はい』"))) == "『はい』"


def test_step4_strips_colon_prefixed_speaker_before_fullwidth_paren():
    assert _text(_pipe().process(_msg("太郎: （はい）"))) == "（はい）"


def test_step4_strips_colon_prefixed_speaker_before_double_quote():
    assert _text(_pipe().process(_msg('太郎: "はい"'))) == '"はい"'


def test_step4_bracket_group_form_still_stripped_alongside_colon_form():
    assert _text(_pipe().process(_msg("【太郎】「行こう」"))) == "「行こう」"


def test_step4_colon_line_without_following_quote_is_kept():
    assert _text(_pipe().process(_msg("注意：これは危険だ"))) == "注意：これは危険だ"


def test_step4_ascii_colon_without_following_quote_is_kept():
    assert _text(_pipe().process(_msg("A: B"))) == "A: B"


def test_step4_overlong_name_before_colon_is_kept():
    raw = "あ" * 17 + ": 「はい」"
    assert _text(_pipe().process(_msg(raw))) == raw


def test_step4_colon_prefix_disabled_keeps_prefix():
    assert _text(_pipe(speaker_strip=False).process(_msg("太郎: 「はい」"))) == "太郎: 「はい」"


def test_order_colon_speaker_strip_before_duplicate_check():
    pipe = _pipe()
    pipe.process(_msg("太郎: 「はい」", 1.0))
    _dropped(pipe.process(_msg("花子: 「はい」", 2.0)), DropReason.DUPLICATE, "duplicate")


# --- step 5: empty ---------------------------------------------------------


@pytest.mark.parametrize("raw", ["", "   ", "\n\n", "　"])
def test_step5_drops_empty_under_no_letters(raw):
    _dropped(_pipe().process(_msg(raw)), DropReason.EMPTY, "no_letters")


# --- step 6: no letters ----------------------------------------------------


@pytest.mark.parametrize("raw", ["……", "123", "！？", "１２３", "- 42 -", "♪♪"])
def test_step6_drops_lines_without_letters(raw):
    _dropped(_pipe().process(_msg(raw)), DropReason.NO_LETTERS, "no_letters")


@pytest.mark.parametrize("raw", ["あ", "ー", "1a", "……え？", "Ok"])
def test_step6_one_letter_is_enough(raw):
    assert isinstance(_pipe().process(_msg(raw)), Accepted)


# --- step 7: junk ----------------------------------------------------------


def test_step7_drops_lines_longer_than_max():
    _dropped(_pipe().process(_msg("あ" * (MAX_LINE_CHARS + 1))), DropReason.JUNK, "junk")


def test_step7_max_length_is_kept():
    assert _text(_pipe().process(_msg("あ" * MAX_LINE_CHARS))) == "あ" * MAX_LINE_CHARS


def test_step7_length_is_measured_after_normalisation():
    raw = "あ" * MAX_LINE_CHARS + "​" * 5 + "   \n"
    assert _text(_pipe().process(_msg(raw))) == "あ" * MAX_LINE_CHARS


# --- step 8: duplicate -----------------------------------------------------


def test_step8_drops_identical_to_previous_accepted():
    pipe = _pipe()
    assert isinstance(pipe.process(_msg("はい", 1.0)), Accepted)
    _dropped(pipe.process(_msg("はい", 50.0)), DropReason.DUPLICATE, "duplicate")


def test_step8_same_line_from_another_source_is_duplicate():
    pipe = _pipe()
    pipe.process(_msg("はい", 1.0, "agent"))
    _dropped(pipe.process(_msg("はい", 1.1, "clipboard")), DropReason.DUPLICATE, "duplicate")


def test_step8_compares_only_with_the_previous_accepted_line():
    pipe = _pipe()
    pipe.process(_msg("はい", 1.0))
    pipe.process(_msg("いいえ", 2.0))
    assert _text(pipe.process(_msg("はい", 3.0))) == "はい"


def test_step8_compares_normalised_text():
    pipe = _pipe()
    pipe.process(_msg("はい", 1.0))
    _dropped(pipe.process(_msg(" は​い\n", 2.0)), DropReason.DUPLICATE, "duplicate")


def test_step8_dropped_lines_do_not_become_previous():
    pipe = _pipe()
    pipe.process(_msg("はい", 1.0))
    pipe.process(_msg("……", 2.0))
    _dropped(pipe.process(_msg("はい", 3.0)), DropReason.DUPLICATE, "duplicate")


def test_first_line_is_never_a_duplicate():
    assert isinstance(_pipe().process(_msg("はい")), Accepted)


# --- order between steps 4-8 -----------------------------------------------


def test_order_speaker_strip_before_empty_check():
    _dropped(_pipe().process(_msg("【太郎】")), DropReason.EMPTY, "no_letters")


def test_order_speaker_strip_before_letter_check():
    _dropped(_pipe().process(_msg("【太郎】……")), DropReason.NO_LETTERS, "no_letters")


def test_order_speaker_strip_before_length_check():
    raw = "【太郎】" + "あ" * MAX_LINE_CHARS
    assert _text(_pipe().process(_msg(raw))) == "あ" * MAX_LINE_CHARS


def test_order_speaker_strip_before_duplicate_check():
    pipe = _pipe()
    pipe.process(_msg("【太郎】はい", 1.0))
    _dropped(pipe.process(_msg("【花子】はい", 2.0)), DropReason.DUPLICATE, "duplicate")


def test_order_letter_check_before_length_check():
    _dropped(_pipe().process(_msg("…" * (MAX_LINE_CHARS + 1))), DropReason.NO_LETTERS, "no_letters")


def test_order_empty_before_duplicate():
    pipe = _pipe()
    pipe.process(_msg("はい", 1.0))
    _dropped(pipe.process(_msg("", 2.0)), DropReason.EMPTY, "no_letters")


def test_order_letter_check_before_duplicate():
    pipe = _pipe()
    pipe.process(_msg("はい……", 1.0))
    _dropped(pipe.process(_msg("……", 2.0)), DropReason.NO_LETTERS, "no_letters")


def test_order_length_check_before_duplicate():
    pipe = _pipe()
    long_line = "あ" * (MAX_LINE_CHARS + 1)
    _dropped(pipe.process(_msg(long_line, 1.0)), DropReason.JUNK, "junk")
    _dropped(pipe.process(_msg(long_line, 2.0)), DropReason.JUNK, "junk")


def test_order_duplicate_before_typewriter_merge():
    pipe = _pipe(typewriter_merge=True)
    pipe.process(_msg("えっと", 1.0))
    _dropped(pipe.process(_msg("えっと", 1.5)), DropReason.DUPLICATE, "duplicate")


# --- step 9: typewriter merge ----------------------------------------------


def test_step9_off_by_default():
    assert FilterSettings().typewriter_merge is False


def test_step9_off_keeps_e_then_etto_as_two_lines():
    pipe = _pipe()
    first = pipe.process(_msg("え", 1.0))
    second = pipe.process(_msg("えっと…", 1.5))
    assert _text(first) == "え"
    assert _text(second) == "えっと…"


def test_step9_on_merges_e_then_etto():
    pipe = _pipe(typewriter_merge=True)
    assert _text(pipe.process(_msg("え", 1.0))) == "え"
    result = pipe.process(_msg("えっと…", 1.5))
    assert isinstance(result, Replaced)
    assert result.line.text == "えっと…"
    assert result.line.raw == "えっと…"


def test_step9_replaced_keeps_previous_arrival_time_so_offset_is_kept():
    pipe = _pipe(typewriter_merge=True)
    pipe.process(_msg("こん", 1.0, "agent"))
    result = pipe.process(_msg("こんにちは", 2.5, "agent"))
    assert isinstance(result, Replaced)
    # The kept line's t_mono is the first frame's: the record clock maps it to the same offset.
    assert result.line.t_mono == 1.0
    assert result.line.source_id == "agent"


def test_step9_window_boundary_by_t_mono():
    pipe = _pipe(typewriter_merge=True)
    pipe.process(_msg("こん", 1.0))
    assert isinstance(pipe.process(_msg("こんに", 1.0 + TYPEWRITER_WINDOW_S)), Replaced)


def test_step9_outside_window_is_a_new_line():
    pipe = _pipe(typewriter_merge=True)
    pipe.process(_msg("こん", 1.0))
    result = pipe.process(_msg("こんにちは", 1.0 + TYPEWRITER_WINDOW_S + 0.001))
    assert _text(result) == "こんにちは"
    assert result.line.t_mono == 1.0 + TYPEWRITER_WINDOW_S + 0.001


def test_step9_window_runs_from_the_latest_merged_frame():
    pipe = _pipe(typewriter_merge=True)
    pipe.process(_msg("こ", 0.0))
    assert isinstance(pipe.process(_msg("こん", 1.5)), Replaced)
    assert isinstance(pipe.process(_msg("こんに", 3.0)), Replaced)
    result = pipe.process(_msg("こんにちは", 4.5))
    assert isinstance(result, Replaced)
    assert result.line.t_mono == 0.0
    assert result.line.text == "こんにちは"


def test_step9_merged_text_becomes_the_previous_line():
    pipe = _pipe(typewriter_merge=True)
    pipe.process(_msg("こん", 1.0))
    pipe.process(_msg("こんにちは", 1.5))
    _dropped(pipe.process(_msg("こんにちは", 1.8)), DropReason.DUPLICATE, "duplicate")


def test_step9_not_a_prefix_is_a_new_line():
    pipe = _pipe(typewriter_merge=True)
    pipe.process(_msg("はい", 1.0))
    assert isinstance(pipe.process(_msg("いいえ", 1.5)), Accepted)


def test_step9_shorter_prefix_of_previous_is_a_new_line():
    pipe = _pipe(typewriter_merge=True)
    pipe.process(_msg("えっと", 1.0))
    assert _text(pipe.process(_msg("え", 1.5))) == "え"


def test_step9_compares_text_after_speaker_strip():
    pipe = _pipe(typewriter_merge=True)
    pipe.process(_msg("【太郎】こん", 1.0))
    result = pipe.process(_msg("【太郎】こんにちは", 1.5))
    assert isinstance(result, Replaced)
    assert result.line.text == "こんにちは"


def test_step9_no_previous_line_is_accepted():
    assert isinstance(_pipe(typewriter_merge=True).process(_msg("え", 1.0)), Accepted)


# --- reset: the previous accepted line is forgotten -------------------------------


def test_reset_turns_a_typewriter_continuation_into_a_new_line():
    # The actor resets when the previous accepted line will not be journalled (armed, paused, split).
    pipe = _pipe(typewriter_merge=True)
    pipe.process(_msg("こんに", 1.0))
    pipe.reset()
    result = pipe.process(_msg("こんにちは", 1.5))
    assert _text(result) == "こんにちは"
    assert result.line.t_mono == 1.5


def test_reset_lets_the_next_session_start_with_the_last_line_of_the_previous_one():
    pipe = _pipe()
    pipe.process(_msg("またね", 1.0))
    pipe.reset()
    assert _text(pipe.process(_msg("またね", 100.0))) == "またね"
