import dataclasses
import json
from types import SimpleNamespace

import pytest

from anki_miner_game.models import profile as profile_module
from anki_miner_game.models.codec import dump_document, load_document
from anki_miner_game.models.constants import START_SHIFT_MS
from anki_miner_game.models.profile import (
    AudioMode,
    AudioSettings,
    AutoSettings,
    CaptureKind,
    CaptureSettings,
    FilterSettings,
    GameProfile,
    OcrEngine,
    OcrSettings,
    TextMode,
    default_audio_mode,
    default_ocr_engine,
    is_safe_slug,
    validate,
)


def _hook_profile(**changes) -> GameProfile:
    base = GameProfile(slug="steins-gate", title="Steins;Gate", audio=AudioSettings(mode=AudioMode.DESKTOP))
    return dataclasses.replace(base, **changes)


def test_spec_defaults(monkeypatch):
    monkeypatch.setattr(profile_module, "sys", SimpleNamespace(platform="linux"))
    profile = GameProfile(slug="g", title="G")
    assert profile.text_mode is TextMode.HOOK
    assert profile.source_ids is None
    assert profile.clipboard is False
    assert profile.capture == CaptureSettings(kind=CaptureKind.AUTO, window=None)
    assert profile.filters == FilterSettings(speaker_strip=True, typewriter_merge=False)
    assert profile.ocr == OcrSettings(engine=OcrEngine.MEIKIOCR, language="ja", window_title=None, rects=None)
    assert profile.auto == AutoSettings(
        enabled=False, start_on_first_line=False, stop_idle_minutes=10, stop_on_window_close=True
    )


@pytest.mark.parametrize(
    ("platform", "audio", "engine"),
    [("win32", AudioMode.APP, OcrEngine.ONEOCR), ("linux", AudioMode.DESKTOP, OcrEngine.MEIKIOCR)],
)
def test_platform_defaults(monkeypatch, platform, audio, engine):
    assert default_audio_mode(platform) is audio
    assert default_ocr_engine(platform) is engine
    monkeypatch.setattr(profile_module, "sys", SimpleNamespace(platform=platform))
    profile = GameProfile(slug="g", title="G")
    assert profile.audio.mode is audio
    assert profile.ocr.engine is engine


def test_start_shift_has_one_entry_per_text_mode():
    assert set(START_SHIFT_MS) == {mode.value for mode in TextMode}
    assert START_SHIFT_MS[TextMode.HOOK] == -400
    assert START_SHIFT_MS[TextMode.OCR] == -1250


def test_frozen_and_replace():
    profile = _hook_profile()
    with pytest.raises(dataclasses.FrozenInstanceError):
        profile.title = "x"  # type: ignore[misc]
    renamed = dataclasses.replace(profile, title="Steins;Gate 0")
    assert renamed.title == "Steins;Gate 0"
    assert profile.title == "Steins;Gate"


def test_valid_hook_profile_has_no_problems():
    assert validate(_hook_profile(source_ids=("textractor",), clipboard=True)) == []


def test_valid_ocr_profile_has_no_problems():
    assert validate(_hook_profile(text_mode=TextMode.OCR, source_ids=None)) == []
    assert validate(_hook_profile(text_mode=TextMode.OCR, source_ids=())) == []


def test_ocr_mode_rejects_hook_sources():
    assert validate(_hook_profile(text_mode=TextMode.OCR, source_ids=("textractor",))) == [
        "OCR mode cannot use hook sources"
    ]


def test_ocr_mode_rejects_clipboard():
    assert validate(_hook_profile(text_mode=TextMode.OCR, clipboard=True)) == ["OCR mode cannot use the clipboard"]


def test_app_audio_needs_a_window():
    no_window = _hook_profile(audio=AudioSettings(mode=AudioMode.APP))
    assert validate(no_window) == ["application audio needs a pinned window"]
    pinned = dataclasses.replace(
        no_window, capture=CaptureSettings(kind=CaptureKind.GAME, window="Game:UnityWndClass:game.exe")
    )
    assert validate(pinned) == []


def test_slug_title_and_idle_minutes_are_checked():
    problems = validate(_hook_profile(slug="../x", title="  ", auto=AutoSettings(stop_idle_minutes=-1)))
    assert problems == [
        "slug '../x' cannot be a file name",
        "title is empty",
        "auto stop idle minutes cannot be negative",
    ]


@pytest.mark.parametrize(
    "slug",
    [
        "",
        ".",
        "..",
        ".hidden",
        "a/b",
        "a\\b",
        "c:d",
        "a\0b",
        "a\tb",
        "a\x7fb",
        "a<b",
        "a>b",
        'a"b',
        "x|y",
        "a?b",
        "a*b",
        "name.",
        "name ",
        "CON",
        "con",
        "Nul",
        "aux.game",
        "prn ",
        "COM1",
        "lpt9",
        "com¹",
    ],
)
def test_unsafe_slugs(slug):
    assert is_safe_slug(slug) is False


@pytest.mark.parametrize(
    "slug", ["steins-gate", "persona-5", "シュタインズゲート", "a.b", "console", "com10", "nul-code"]
)
def test_safe_slugs(slug):
    assert is_safe_slug(slug) is True


def test_json_round_trip_with_schema():
    profile = GameProfile(
        slug="zero-escape",
        title="Zero Escape",
        text_mode=TextMode.OCR,
        source_ids=None,
        clipboard=False,
        capture=CaptureSettings(kind=CaptureKind.WINDOW, window="Zero:Class:zero.exe"),
        audio=AudioSettings(mode=AudioMode.APP),
        filters=FilterSettings(speaker_strip=False, typewriter_merge=True),
        ocr=OcrSettings(engine=OcrEngine.GLENS, language="ja", window_title="Zero", rects="0,0,640,480"),
        auto=AutoSettings(enabled=True, start_on_first_line=True, stop_idle_minutes=0, stop_on_window_close=False),
    )
    text = dump_document(profile)
    data = json.loads(text)
    assert data["schema"] == 1
    assert data["text_mode"] == "ocr"
    assert data["capture"] == {"kind": "window", "window": "Zero:Class:zero.exe"}
    assert load_document(GameProfile, text) == profile


def test_json_round_trip_keeps_hook_source_list():
    profile = _hook_profile(source_ids=("textractor", "luna"))
    data = json.loads(dump_document(profile))
    assert data["source_ids"] == ["textractor", "luna"]
    assert load_document(GameProfile, dump_document(profile)) == profile
