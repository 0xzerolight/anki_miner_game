"""The settings dialog (spec 5 ``AppConfig``; UJ-21..UJ-24, UJ-30, UJ-31, UJ-33, B4-03, B4-07, B5-02)."""

import shlex
from dataclasses import replace

import pytest
from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QKeySequence
from PyQt6.QtWidgets import (
    QFileDialog,
    QGroupBox,
    QHeaderView,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QScrollArea,
)

import anki_miner_game.gui.settings_dialog as settings_module
from anki_miner_game.gui.settings_dialog import (
    BIND_TEXT,
    END_GAP_NOTE,
    HOTKEY_LABEL,
    LINUX_CONTROL_NOTE,
    RESERVED_SOURCE_IDS,
    TRIM_TEXT,
    VIDEO_PRESETS,
    SettingsDialog,
    linux_toggle_command,
    vad_install_text,
)
from anki_miner_game.gui.widgets.layout import available_size
from anki_miner_game.gui.wizard import WizardStep
from anki_miner_game.models.addons import AddonStatus
from anki_miner_game.models.config import (
    AppConfig,
    CueSettings,
    FeedSettings,
    ObsSettings,
    RecordingSettings,
    TextSourceConfig,
    VadSettings,
)
from anki_miner_game.models.constants import MAX_CUE_SECONDS_MAX, MAX_CUE_SECONDS_MIN
from anki_miner_game.models.messages import OBS_SOURCE_ID
from anki_miner_game.text.sources.clipboard_source import CLIPBOARD_SOURCE_ID
from anki_miner_game.text.sources.ocr_source import OCR_SOURCE_ID
from tests.gui.wizard_fakes import glossary_misses


class FakeVad:
    def __init__(
        self, status: AddonStatus = AddonStatus.READY, note: str | None = None, size: int = 96_000_000
    ) -> None:
        self._status = status
        self._note = note
        self._size = size

    def status(self) -> AddonStatus:
        return self._status

    def set_status(self, status: AddonStatus) -> None:
        self._status = status

    @property
    def size_bytes(self) -> int:
        return self._size

    @property
    def note(self) -> str | None:
        return self._note

    async def install(self, progress: object) -> None:
        raise AssertionError("the dialog never installs")


CUSTOM = AppConfig(
    output_root="/data/Game Sessions",
    obs=ObsSettings(host="192.168.1.20", port=4460, password_override="hunter2"),
    text_sources=(
        TextSourceConfig(id="textractor", name="Textractor", uri="localhost:6677", enabled=False),
        TextSourceConfig(id="my-hooker", name="My hooker", uri="127.0.0.1:7000/text"),
    ),
    feed=FeedSettings(enabled=False, ws_port=7678, http_port=7679),
    hotkey="Ctrl+Alt+F10",
    recording=RecordingSettings(max_height=720, fps=60),
    cue=CueSettings(max_cue_seconds=20, end_gap_ms=400),
    vad=VadSettings(enabled=False),
    last_game="steins-gate",
)


def open_dialog(qtbot, cfg: AppConfig = CUSTOM, *, platform: str = "linux", vad: FakeVad | None = None, **kwargs):
    dialog = SettingsDialog(cfg, vad_addon=vad or FakeVad(), platform=platform, **kwargs)
    qtbot.addWidget(dialog)
    return dialog


def save(qtbot, dialog: SettingsDialog) -> AppConfig:
    with qtbot.waitSignal(dialog.config_saved, timeout=1000) as saved:
        dialog.buttons.button(dialog.buttons.StandardButton.Save).click()
    return saved.args[0]


def refused(qtbot, dialog: SettingsDialog) -> str:
    with qtbot.assertNotEmitted(dialog.config_saved):
        dialog.accept()
    assert not dialog.problems_label.isHidden()
    assert dialog.result() != dialog.DialogCode.Accepted.value
    return dialog.problems_label.text()


def source_rows(dialog: SettingsDialog) -> int:
    return dialog.sources_table.rowCount()


# The whole config -----------------------------------------------------------------------------------


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_every_field_survives_the_dialog(qtbot, platform: str) -> None:
    for cfg in (CUSTOM, AppConfig()):
        dialog = open_dialog(qtbot, cfg, platform=platform)
        assert dialog.config() == cfg


def test_save_emits_the_edited_config(qtbot) -> None:
    dialog = open_dialog(qtbot)
    dialog.video_combo.setCurrentIndex(0)
    dialog.max_cue_spin.setValue(30)
    dialog.vad_check.setChecked(True)

    assert save(qtbot, dialog) == replace(
        CUSTOM,
        recording=RecordingSettings(max_height=1080, fps=30),
        cue=CueSettings(max_cue_seconds=30, end_gap_ms=400),
        vad=VadSettings(enabled=True),
    )
    assert dialog.result() == dialog.DialogCode.Accepted.value


# Layout (UJ-21, UJ-30) --------------------------------------------------------------------------------


def test_two_groups_and_a_collapsed_advanced_part(qtbot) -> None:
    dialog = open_dialog(qtbot)
    titles = {box.title() for box in dialog.findChildren(QGroupBox)}
    assert titles == {
        "Recordings",
        "While playing",
        "Text hookers",
        "OBS connection",
        "Subtitle timing",
        "Text feed ports",
    }
    assert dialog.advanced_button.text() == "Advanced"
    assert dialog.advanced.isHidden()
    for widget in (dialog.sources_table, dialog.port_spin, dialog.max_cue_spin, dialog.ws_port_spin):
        assert dialog.advanced.isAncestorOf(widget)
    for widget in (dialog.output_edit, dialog.video_combo, dialog.vad_check, dialog.feed_check):
        assert not dialog.advanced.isAncestorOf(widget)
    dialog.advanced_button.click()
    assert not dialog.advanced.isHidden()
    dialog.advanced_button.click()
    assert dialog.advanced.isHidden()


def test_the_dialog_fits_the_screen_without_a_horizontal_scroll_bar(qtbot) -> None:
    dialog = open_dialog(qtbot)
    dialog.show()
    scroll = dialog.findChild(QScrollArea)
    assert scroll is not None
    assert scroll.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    screen = available_size(dialog)
    assert dialog.width() <= screen.width() and dialog.height() <= screen.height()
    dialog.advanced_button.click()
    assert dialog.height() <= screen.height()


def test_the_labels_use_the_glossary(qtbot) -> None:
    dialog = open_dialog(qtbot, platform="win32")
    labels = {label.text() for label in dialog.findChildren(QLabel)}
    for text in ("Save sessions in", "Video", "Port", "Password", "Longest line", "Gap before the next line"):
        assert text in labels
    assert dialog.vad_check.text() == TRIM_TEXT == "Trim each line's end to where the voice stops"
    assert dialog.feed_check.text() == "Show the lines on a web page for dictionary lookups"


def test_no_user_facing_settings_text_uses_words_outside_the_glossary() -> None:
    assert glossary_misses(settings_module) == []


# OBS connection ---------------------------------------------------------------------------------------


def test_the_obs_host_is_kept_but_not_shown(qtbot) -> None:
    dialog = open_dialog(qtbot)
    assert "192.168.1.20" not in [edit.text() for edit in dialog.findChildren(QLineEdit)]
    assert dialog.config().obs.host == "192.168.1.20"


def test_a_stored_empty_obs_host_is_still_refused(qtbot) -> None:
    dialog = open_dialog(qtbot, replace(CUSTOM, obs=replace(CUSTOM.obs, host="")))
    assert "OBS host" in refused(qtbot, dialog)


def test_obs_port_and_password_are_read_from_obs_unless_overridden(qtbot) -> None:
    dialog = open_dialog(qtbot)
    assert dialog.password_edit.echoMode() is QLineEdit.EchoMode.Password

    dialog.port_spin.setValue(0)
    dialog.password_edit.setText("")

    assert dialog.port_spin.text() == "Read from OBS"
    assert dialog.config().obs == ObsSettings(host="192.168.1.20", port=None, password_override=None)


# Subtitle timing ------------------------------------------------------------------------------------


def test_max_cue_seconds_is_bounded(qtbot) -> None:
    dialog = open_dialog(qtbot)

    assert (dialog.max_cue_spin.minimum(), dialog.max_cue_spin.maximum()) == (MAX_CUE_SECONDS_MIN, MAX_CUE_SECONDS_MAX)
    assert "audio padding (0.3 s)" in END_GAP_NOTE
    assert END_GAP_NOTE in {label.text() for label in dialog.findChildren(QLabel)}


# Text hookers ---------------------------------------------------------------------------------------


def test_the_hooker_table_fits_its_columns_and_names_the_address_format(qtbot) -> None:
    table = open_dialog(qtbot).sources_table
    header = table.horizontalHeader()
    assert [table.horizontalHeaderItem(i).text() for i in range(3)] == ["On", "Name", "Address (host:port)"]
    assert header.sectionResizeMode(0) == QHeaderView.ResizeMode.ResizeToContents
    assert header.sectionResizeMode(1) == QHeaderView.ResizeMode.ResizeToContents
    assert header.sectionResizeMode(2) == QHeaderView.ResizeMode.Stretch


def test_a_new_source_is_selected_with_its_name_being_edited_and_the_table_grows(qtbot) -> None:
    dialog = open_dialog(qtbot)
    dialog.show()
    dialog.advanced_button.click()
    before = dialog.sources_table.maximumHeight()

    dialog.add_source_button.click()

    row = source_rows(dialog) - 1
    assert (dialog.sources_table.currentRow(), dialog.sources_table.currentColumn()) == (row, 1)
    editor = dialog.sources_table.viewport().findChild(QLineEdit)
    assert editor is not None and editor.text() == "New source"
    assert dialog.sources_table.maximumHeight() == before + dialog.sources_table.rowHeight(row)


def test_a_new_text_source_gets_a_free_id_from_its_name(qtbot) -> None:
    dialog = open_dialog(qtbot)

    dialog.add_source_button.click()
    row = source_rows(dialog) - 1
    dialog.sources_table.item(row, 1).setText("My Hooker")
    dialog.sources_table.item(row, 2).setText("ws://localhost:7001")

    added = dialog.config().text_sources[-1]
    assert added == TextSourceConfig(id="my-hooker-2", name="My Hooker", uri="localhost:7001", enabled=True)


@pytest.mark.parametrize("name", ["OBS", "Clipboard", "ocr", "!!!"])
def test_a_new_source_never_takes_a_reserved_id(qtbot, name: str) -> None:
    dialog = open_dialog(qtbot)
    dialog.add_source_button.click()
    row = source_rows(dialog) - 1
    dialog.sources_table.item(row, 1).setText(name)
    dialog.sources_table.item(row, 2).setText("localhost:7001")

    new_id = dialog.config().text_sources[-1].id

    assert new_id not in RESERVED_SOURCE_IDS
    assert new_id not in {source.id for source in CUSTOM.text_sources}


def test_the_reserved_ids_are_the_apps_own_sources() -> None:
    assert frozenset({OBS_SOURCE_ID, CLIPBOARD_SOURCE_ID, OCR_SOURCE_ID}) == RESERVED_SOURCE_IDS


def test_renaming_a_source_keeps_its_id(qtbot) -> None:
    dialog = open_dialog(qtbot)

    dialog.sources_table.item(1, 1).setText("Renamed")

    assert dialog.config().text_sources[1] == replace(CUSTOM.text_sources[1], name="Renamed")


def test_a_source_can_be_turned_on_and_removed(qtbot) -> None:
    dialog = open_dialog(qtbot)
    dialog.sources_table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    dialog.sources_table.setCurrentCell(1, 1)

    dialog.remove_source_button.click()

    assert dialog.config().text_sources == (replace(CUSTOM.text_sources[0], enabled=True),)


def test_test_asks_for_the_game_text_page_with_the_rows_as_they_stand(qtbot) -> None:
    dialog = open_dialog(qtbot)
    dialog.sources_table.item(0, 0).setCheckState(Qt.CheckState.Checked)

    with qtbot.waitSignal(dialog.test_sources_requested, timeout=1000) as asked:
        dialog.test_sources_button.click()

    assert dialog.test_sources_button.text() == "Test…"
    assert asked.args == [dialog.config().text_sources]
    assert asked.args[0][0].enabled is True


# Invalid settings (UJ-31) ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("edit", "problem"),
    [
        (lambda d: d.output_edit.setText("  "), "output folder"),
        (lambda d: d.sources_table.item(0, 1).setText(""), "name"),
        (lambda d: d.sources_table.item(1, 2).setText(" "), "address"),
        (lambda d: d.http_port_spin.setValue(d.ws_port_spin.value()), "different ports"),
    ],
)
def test_invalid_settings_are_not_saved_and_say_why(qtbot, edit, problem: str) -> None:
    dialog = open_dialog(qtbot)
    edit(dialog)

    assert problem in refused(qtbot, dialog)


def test_the_problems_line_clears_at_the_next_edit(qtbot) -> None:
    dialog = open_dialog(qtbot)
    dialog.http_port_spin.setValue(dialog.ws_port_spin.value())
    refused(qtbot, dialog)

    dialog.end_gap_spin.setValue(450)

    assert dialog.problems_label.isHidden()
    assert dialog.problems_label.text() == ""


# Start and stop (Task 8 replaces the Windows field) ---------------------------------------------------


PORTABLE = QKeySequence.SequenceFormat.PortableText


def test_windows_records_the_hotkey_by_key_press(qtbot) -> None:
    dialog = open_dialog(qtbot, platform="win32")
    edit = dialog.hotkey_edit
    assert isinstance(edit, QKeySequenceEdit)
    assert edit.maximumSequenceLength() == 1
    assert edit.isClearButtonEnabled()
    assert edit.keySequence().toString(PORTABLE) == "Ctrl+Alt+F10"
    assert dialog._playing_form.labelForField(edit).text() == HOTKEY_LABEL == "Start/stop recording hotkey"

    edit.setKeySequence(QKeySequence("Alt+F9"))

    assert save(qtbot, dialog).hotkey == "Alt+F9"


@pytest.mark.parametrize("stored", ["Win+F9", "ctrl+shift+f8"])
def test_an_untouched_hotkey_is_kept_as_stored(qtbot, stored: str) -> None:
    dialog = open_dialog(qtbot, replace(CUSTOM, hotkey=stored), platform="win32")
    assert save(qtbot, dialog).hotkey == stored


@pytest.mark.parametrize(
    ("press", "problem"),
    [
        (lambda edit: edit.setKeySequence(QKeySequence("F8")), "The hotkey F8 cannot be used"),
        (lambda edit: edit.clear(), "No start/stop recording hotkey is set."),
    ],
)
def test_a_hotkey_the_app_cannot_use_is_refused_in_one_sentence(qtbot, press, problem: str) -> None:
    dialog = open_dialog(qtbot, platform="win32")
    press(dialog.hotkey_edit)

    text = refused(qtbot, dialog)

    assert problem in text
    assert "Hotkey '" not in text


def test_linux_shows_the_one_command_to_bind(qtbot, monkeypatch) -> None:
    monkeypatch.setattr(
        settings_module, "linux_toggle_command", lambda: "/home/you/Apps/AnkiMinerGame.AppImage --toggle"
    )
    dialog = open_dialog(qtbot, platform="linux")

    assert dialog.hotkey_edit is None
    assert dialog.control_label.text() == (
        "Bind this command to a key in your desktop's keyboard settings: "
        "/home/you/Apps/AnkiMinerGame.AppImage --toggle"
    )
    assert dialog.control_label.text().startswith(BIND_TEXT)
    assert dialog.control_label.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse
    assert dialog.config().hotkey == CUSTOM.hotkey


def test_linux_from_source_explains_every_package(qtbot, monkeypatch) -> None:
    monkeypatch.setattr(settings_module, "linux_toggle_command", lambda: None)
    dialog = open_dialog(qtbot, platform="linux")
    assert dialog.control_label.text() == LINUX_CONTROL_NOTE


def test_the_command_is_the_appimage_when_running_as_one() -> None:
    command = linux_toggle_command(
        environ={"APPIMAGE": "/home/you/Apps/AnkiMinerGame.AppImage"},
        frozen=True,
        executable="/tmp/.mount_AnkiMi/usr/bin/AnkiMinerGame",
    )
    assert command == "/home/you/Apps/AnkiMinerGame.AppImage --toggle"


def test_the_deb_command_is_on_path() -> None:
    command = linux_toggle_command(environ={}, frozen=True, executable="/opt/anki-miner-game/AnkiMinerGame")
    assert command == "anki_miner_game --toggle"


def test_the_tarball_command_is_its_launcher_quoted(tmp_path) -> None:
    bundle = tmp_path / "My Games" / "AnkiMinerGame"
    bundle.mkdir(parents=True)
    (bundle / "anki_miner_game").write_text("#!/bin/sh\n", encoding="utf-8")

    command = linux_toggle_command(environ={}, frozen=True, executable=str(bundle / "AnkiMinerGame"))

    assert command == f"{shlex.quote(str(bundle / 'anki_miner_game'))} --toggle"
    assert command.startswith("'")


def test_a_frozen_build_without_a_launcher_names_its_executable(tmp_path) -> None:
    exe = tmp_path / "AnkiMinerGame"
    assert linux_toggle_command(environ={}, frozen=True, executable=str(exe)) == f"{shlex.quote(str(exe))} --toggle"


def test_a_source_run_has_no_single_command() -> None:
    assert (
        linux_toggle_command(environ={"APPIMAGE": "/x.AppImage"}, frozen=False, executable="/usr/bin/python3") is None
    )


# Voice trimming (UJ-21, UJ-22a) ---------------------------------------------------------------------


def test_with_the_add_on_ready_the_trim_check_shows(qtbot) -> None:
    dialog = open_dialog(qtbot, vad=FakeVad(AddonStatus.READY))
    assert not dialog.vad_check.isHidden()
    assert dialog.vad_install_row.isHidden()


@pytest.mark.parametrize("status", [AddonStatus.MISSING, AddonStatus.BROKEN, AddonStatus.INSTALLING])
def test_without_the_add_on_an_install_line_replaces_the_trim_check(qtbot, status: AddonStatus) -> None:
    dialog = open_dialog(qtbot, replace(CUSTOM, vad=VadSettings(enabled=True)), vad=FakeVad(status))

    assert dialog.vad_check.isHidden()
    assert not dialog.vad_install_row.isHidden()
    assert dialog.vad_install_label.text() == vad_install_text(96_000_000)
    assert vad_install_text(96_000_000) == (
        "Trimming each line to the voice needs the voice-trimming add-on (about 96 MB)."
    )
    assert dialog.vad_install_button.text() == ("Repair…" if status is AddonStatus.BROKEN else "Install…")
    assert dialog.config().vad.enabled is True  # kept as stored


def test_refresh_addons_reads_the_add_on_again(qtbot) -> None:
    vad = FakeVad(AddonStatus.MISSING)
    dialog = open_dialog(qtbot, vad=vad)

    vad.set_status(AddonStatus.READY)
    dialog.refresh_addons()

    assert not dialog.vad_check.isHidden()
    assert dialog.vad_install_row.isHidden()


@pytest.mark.parametrize(
    ("button", "step", "text"),
    [("vad_install_button", WizardStep.ADDONS, "Install…"), ("setup_obs_button", WizardStep.OBS, "Set up OBS…")],
)
def test_setup_buttons_ask_for_their_wizard_page(qtbot, button: str, step: WizardStep, text: str) -> None:
    dialog = open_dialog(qtbot, vad=FakeVad(AddonStatus.MISSING))
    assert getattr(dialog, button).text() == text

    with qtbot.waitSignal(dialog.setup_step_requested, timeout=1000) as asked:
        getattr(dialog, button).click()

    assert asked.args == [step]


# Video (UJ-23) ----------------------------------------------------------------------------------------


def test_video_is_one_choice_of_four(qtbot) -> None:
    dialog = open_dialog(qtbot, AppConfig())
    assert VIDEO_PRESETS == (
        ("1080p, 30 fps", 1080, 30),
        ("720p, 30 fps (smaller files)", 720, 30),
        ("1080p, 60 fps", 1080, 60),
        ("720p, 60 fps", 720, 60),
    )
    assert [dialog.video_combo.itemText(i) for i in range(dialog.video_combo.count())] == [
        label for label, _, _ in VIDEO_PRESETS
    ]
    assert dialog.video_combo.currentText() == "1080p, 30 fps"


def test_a_stored_video_setting_outside_the_choices_is_kept(qtbot) -> None:
    dialog = open_dialog(qtbot, replace(CUSTOM, recording=RecordingSettings(max_height=900, fps=24)))

    assert dialog.video_combo.currentText() == "900p, 24 fps"
    assert dialog.video_combo.count() == len(VIDEO_PRESETS) + 1
    assert dialog.config().recording == RecordingSettings(max_height=900, fps=24)


# The text feed ----------------------------------------------------------------------------------------


def test_the_feed_page_link_and_ports_follow_the_feed_check(qtbot) -> None:
    opened: list[QUrl] = []
    dialog = open_dialog(qtbot, open_url=opened.append)  # CUSTOM: feed off, page port 7679
    assert dialog.feed_link.isHidden()
    assert not dialog.feed_ports.isEnabled()

    dialog.feed_check.setChecked(True)

    assert not dialog.feed_link.isHidden()
    assert dialog.feed_ports.isEnabled()
    assert "http://127.0.0.1:7679/" in dialog.feed_link.text()
    dialog.http_port_spin.setValue(7680)
    assert "http://127.0.0.1:7680/" in dialog.feed_link.text()
    dialog.feed_link.linkActivated.emit("http://127.0.0.1:7680/")
    assert opened == [QUrl("http://127.0.0.1:7680/")]


# Output folder ----------------------------------------------------------------------------------------


def test_browse_fills_the_output_folder(qtbot, tmp_path) -> None:
    dialog = open_dialog(qtbot)

    dialog.browse_button.click()
    picker = dialog.findChild(QFileDialog)
    assert picker is not None and picker.isVisible()
    picker.fileSelected.emit(str(tmp_path))
    picker.close()

    assert dialog.config().output_root == str(tmp_path)


# A wizard step run from here ---------------------------------------------------------------------------


def test_the_password_a_setup_step_saved_shows_and_the_other_edits_stay(qtbot) -> None:
    dialog = open_dialog(qtbot)
    dialog.max_cue_spin.setValue(25)

    dialog.take_setup(replace(CUSTOM, obs=replace(CUSTOM.obs, password_override="typed")))

    cfg = dialog.config()
    assert cfg.obs.password_override == "typed"
    assert cfg.cue.max_cue_seconds == 25
