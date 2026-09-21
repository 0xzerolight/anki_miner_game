"""Window strings and video-source priority shared by provisioning and auto mode (spec 11.3, 12, 22)."""

import pytest

from anki_miner_game.obs.provision import (
    WindowTarget,
    get_video_source_priority,
    parse_obs_window_target,
    window_class_exe,
)


def test_a_windows_window_string_splits_into_title_class_and_exe():
    assert parse_obs_window_target("Steins;Gate:UnityWndClass:SteinsGate.exe") == WindowTarget(
        title="Steins;Gate", window_class="UnityWndClass", exe="SteinsGate.exe"
    )


def test_escaped_colons_and_hashes_are_decoded_in_obs_order():
    # "#3A" first, then "#22": a literal "#3A" in a title arrives as "#223A".
    target = parse_obs_window_target("Re#3AZero #2213 #223A:Cls#3A1:game#22.exe")
    assert target == WindowTarget(title="Re:Zero #13 #3A", window_class="Cls:1", exe="game#.exe")


def test_parts_are_kept_verbatim_without_stripping():
    assert parse_obs_window_target(" Title :Cls:x.exe") == WindowTarget(" Title ", "Cls", "x.exe")


@pytest.mark.parametrize(
    "value",
    [
        "",
        "no separators",
        "two:parts",
        "four:parts:in:all",
        "0x3a00007\r\nSteins;Gate\r\nsteinsgate",
        "0x1\r\nA:B\r\nc:d",
    ],
)
def test_anything_but_a_three_part_windows_string_is_not_a_target(value):
    assert parse_obs_window_target(value) is None


def test_class_and_exe_compare_case_insensitively():
    assert window_class_exe("Title:UnityWndClass:Game.EXE") == ("unitywndclass", "game.exe")
    assert window_class_exe("Other title:UNITYWNDCLASS:game.exe") == window_class_exe("T:unitywndclass:GAME.exe")
    assert window_class_exe("0x1\r\nname\r\nclass") is None


@pytest.mark.parametrize(
    ("kind", "priority"),
    [("game_capture", 0), ("window_capture", 1), ("monitor_capture", 2), ("xcomposite_input", 999), (None, 999)],
)
def test_video_source_priority_prefers_game_capture(kind, priority):
    assert get_video_source_priority(kind) == priority
