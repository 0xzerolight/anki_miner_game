"""The settings dialog (spec 5 ``AppConfig``)."""

from dataclasses import replace

import pytest
from PyQt6.QtWidgets import QFileDialog, QLineEdit

from anki_miner_game.gui.settings_dialog import (
    END_GAP_NOTE,
    LINUX_CONTROL_NOTE,
    OBS_PASSWORD_NOTE,
    RESERVED_SOURCE_IDS,
    SettingsDialog,
)
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


class FakeVad:
    def __init__(self, status: AddonStatus = AddonStatus.READY, note: str | None = None) -> None:
        self._status = status
        self._note = note

    def status(self) -> AddonStatus:
        return self._status

    @property
    def size_bytes(self) -> int:
        return 1

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


def open_dialog(qtbot, cfg: AppConfig = CUSTOM, *, platform: str = "linux", vad: FakeVad | None = None):
    dialog = SettingsDialog(cfg, vad_addon=vad or FakeVad(), platform=platform)
    qtbot.addWidget(dialog)
    return dialog


def source_rows(dialog: SettingsDialog) -> int:
    return dialog.sources_table.rowCount()


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_every_field_survives_the_dialog(qtbot, platform: str) -> None:
    for cfg in (CUSTOM, AppConfig()):
        dialog = open_dialog(qtbot, cfg, platform=platform)
        assert dialog.config() == cfg


def test_save_emits_the_edited_config(qtbot) -> None:
    dialog = open_dialog(qtbot)
    dialog.output_edit.setText("  /data/Other  ")
    dialog.max_height_combo.setCurrentIndex(dialog.max_height_combo.findData(1080))
    dialog.max_cue_spin.setValue(30)
    dialog.vad_check.setChecked(True)

    with qtbot.waitSignal(dialog.config_saved) as saved:
        dialog.buttons.button(dialog.buttons.StandardButton.Save).click()

    assert saved.args == [
        replace(
            CUSTOM,
            output_root="/data/Other",
            recording=RecordingSettings(max_height=1080, fps=60),
            cue=CueSettings(max_cue_seconds=30, end_gap_ms=400),
            vad=VadSettings(enabled=True),
        )
    ]
    assert dialog.result() == dialog.DialogCode.Accepted.value


def test_obs_port_and_password_are_read_from_obs_unless_overridden(qtbot) -> None:
    dialog = open_dialog(qtbot)
    assert dialog.password_edit.echoMode() is QLineEdit.EchoMode.Password

    dialog.port_spin.setValue(0)
    dialog.password_edit.setText("")

    assert dialog.port_spin.text() == "Read from OBS"
    assert dialog.config().obs == ObsSettings(host="192.168.1.20", port=None, password_override=None)
    assert "stores none" in OBS_PASSWORD_NOTE


def test_max_cue_seconds_is_bounded(qtbot) -> None:
    dialog = open_dialog(qtbot)

    assert (dialog.max_cue_spin.minimum(), dialog.max_cue_spin.maximum()) == (MAX_CUE_SECONDS_MIN, MAX_CUE_SECONDS_MAX)
    assert "audio padding" in END_GAP_NOTE


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
    from PyQt6.QtCore import Qt

    dialog = open_dialog(qtbot)
    dialog.sources_table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    dialog.sources_table.setCurrentCell(1, 1)

    dialog.remove_source_button.click()

    assert dialog.config().text_sources == (replace(CUSTOM.text_sources[0], enabled=True),)


@pytest.mark.parametrize(
    ("edit", "problem"),
    [
        (lambda d: d.output_edit.setText("  "), "output folder"),
        (lambda d: d.host_edit.setText(""), "OBS host"),
        (lambda d: d.sources_table.item(0, 1).setText(""), "name"),
        (lambda d: d.sources_table.item(1, 2).setText(" "), "address"),
        (lambda d: d.http_port_spin.setValue(d.ws_port_spin.value()), "different ports"),
    ],
)
def test_invalid_settings_are_not_saved_and_say_why(qtbot, edit, problem: str) -> None:
    dialog = open_dialog(qtbot)
    edit(dialog)

    with qtbot.assertNotEmitted(dialog.config_saved):
        dialog.accept()

    assert problem in dialog.problems_label.text()
    assert not dialog.problems_label.isHidden()
    assert dialog.result() != dialog.DialogCode.Accepted.value


def test_windows_edits_the_hotkey_and_checks_it(qtbot) -> None:
    dialog = open_dialog(qtbot, platform="win32")
    assert dialog.hotkey_edit is not None
    dialog.hotkey_edit.setText("Ctrl+Shift+F8")
    assert dialog.config().hotkey == "Ctrl+Shift+F8"

    dialog.hotkey_edit.setText("Ctrl+Nonsense")

    with qtbot.assertNotEmitted(dialog.config_saved):
        dialog.accept()
    assert "hotkey" in dialog.problems_label.text()


def test_linux_shows_how_to_bind_the_cli_verbs_instead_of_a_hotkey(qtbot) -> None:
    from PyQt6.QtWidgets import QLabel

    dialog = open_dialog(qtbot, platform="linux")

    assert dialog.hotkey_edit is None
    assert LINUX_CONTROL_NOTE in [label.text() for label in dialog.findChildren(QLabel)]
    assert "--toggle" in LINUX_CONTROL_NOTE
    assert dialog.config().hotkey == CUSTOM.hotkey


@pytest.mark.parametrize(
    ("status", "note", "shown"),
    [
        (AddonStatus.READY, None, "installed"),
        (AddonStatus.MISSING, None, "not installed"),
        (AddonStatus.BROKEN, "Something platform-specific.", "Something platform-specific."),
    ],
)
def test_the_vad_addon_status_and_note_are_shown(qtbot, status: AddonStatus, note: str | None, shown: str) -> None:
    dialog = open_dialog(qtbot, vad=FakeVad(status, note))

    assert shown in dialog.vad_status_label.text()


def test_browse_fills_the_output_folder(qtbot, tmp_path) -> None:
    dialog = open_dialog(qtbot)

    dialog.browse_button.click()
    picker = dialog.findChild(QFileDialog)
    assert picker is not None and picker.isVisible()
    picker.fileSelected.emit(str(tmp_path))
    picker.close()

    assert dialog.config().output_root == str(tmp_path)
