"""The settings dialog: every ``AppConfig`` field of spec 5 but ``last_game``, which it keeps as it is.

The dialog saves nothing: on Save it emits ``config_saved`` with the new config, and the caller
stores it. The OBS port and password are read from OBS's own websocket settings unless the user
types an override; only a typed password is ever stored (spec 11.1). The hotkey is Windows only
(spec 16); on Linux the dialog says how to bind the CLI verbs instead. Each setup wizard step can be
run again from here (spec 16): the dialog asks with ``setup_step_requested(WizardStep)``, and
``take_setup`` shows what such a step saved.
"""

import os
import sys
from dataclasses import replace
from typing import Final

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from anki_miner_game.gui.hotkey_win import HotkeyError, parse_hotkey
from anki_miner_game.gui.wizard import WizardStep
from anki_miner_game.interfaces.addons import AddonService
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

RESERVED_SOURCE_IDS: Final = frozenset({OBS_SOURCE_ID, "clipboard", "ocr"})
"""Source ids the app uses itself: the OBS light, the clipboard source and the OCR source; a text
source the user adds never gets one."""

OBS_PASSWORD_NOTE: Final = (
    "Leave the port and password to OBS: the app reads them from OBS's WebSocket settings at each "
    "connect and stores none. A password typed here is stored in the app's settings."
)
MAX_CUE_NOTE: Final = "The longest a cue lasts when the next line is slow to come."
END_GAP_NOTE: Final = (
    "Silence between a cue's end and the next cue: Anki Miner's audio padding (0.3 s) plus 50 ms. "
    "Raise it by as much as you raise that padding."
)
VAD_NOTE: Final = "After each session, trim every cue's end to where the voice stops."
FEED_NOTE: Final = (
    "A page on this machine shows each line as it arrives, for dictionary lookups while playing; "
    "texthooker pages can connect to the WebSocket port."
)
SETUP_STEPS: Final = (
    (WizardStep.OBS, "OBS"),
    (WizardStep.SOURCES, "Text sources"),
    (WizardStep.FOLDER, "Output folder"),
    (WizardStep.ADDONS, "Add-ons"),
)
SETUP_NOTE: Final = "Run a step of the setup wizard again."
LINUX_CONTROL_NOTE: Final = (
    "Linux has no global hotkey. In your desktop's keyboard shortcut settings, bind the app's command "
    "followed by --toggle (or --start, --stop, --arm <game>). The command is anki_miner_game for the "
    ".deb, the full path of the .AppImage file for the AppImage, and, for the .tar.gz, the full path "
    "of AnkiMinerGame/anki_miner_game in the folder you extracted it to."
)
"""Only the .deb puts ``anki_miner_game`` on PATH (packaging/nfpm.yaml)."""

_VAD_STATUS_TEXT: Final = {
    AddonStatus.READY: "VAD add-on installed.",
    AddonStatus.MISSING: "The VAD add-on is not installed: the live subtitle is kept. Install it from the setup wizard.",
    AddonStatus.INSTALLING: "The VAD add-on is being installed.",
    AddonStatus.BROKEN: "The VAD add-on is damaged: install it again from the setup wizard.",
}

_HEIGHTS: Final = (1080, 720)
_ON, _NAME, _ADDRESS = range(3)
_ID_ROLE: Final = Qt.ItemDataRole.UserRole


def _note(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    return label


def _port_spin(value: int) -> QSpinBox:
    spin = QSpinBox()
    spin.setRange(1, 65535)
    spin.setValue(value)
    return spin


def _new_source_id(name: str, taken: set[str]) -> str:
    """An id for a new source: its name in lower case with ``-`` between words, made unique."""
    base = "-".join("".join(ch if ch.isalnum() else " " for ch in name.lower()).split()) or "source"
    candidate, n = base, 2
    while candidate in taken or candidate in RESERVED_SOURCE_IDS:
        candidate, n = f"{base}-{n}", n + 1
    return candidate


def _address(text: str) -> str:
    """A source address without the ``ws://`` scheme the source adds itself (``TextSourceConfig.uri``)."""
    text = text.strip()
    return text.removeprefix("ws://")


class SettingsDialog(QDialog):
    """Edit the app's settings (spec 5); emits ``config_saved(AppConfig)`` on Save."""

    config_saved = pyqtSignal(object)
    setup_step_requested = pyqtSignal(object)
    """A ``WizardStep`` to run again."""

    def __init__(
        self,
        cfg: AppConfig,
        *,
        vad_addon: AddonService,
        platform: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        """``platform`` defaults to ``sys.platform``."""
        super().__init__(parent)
        self._cfg = cfg
        self._windows = (platform or sys.platform) == "win32"
        self.setWindowTitle("Settings")
        content = QWidget()
        column = QVBoxLayout(content)
        column.addWidget(self._build_recordings(cfg))
        column.addWidget(self._build_obs(cfg))
        column.addWidget(self._build_sources(cfg))
        column.addWidget(self._build_subtitles(cfg, vad_addon))
        column.addWidget(self._build_feed(cfg))
        column.addWidget(self._build_control(cfg))
        column.addWidget(self._build_setup())
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        self.problems_label = _note("")
        self.problems_label.hide()
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        outer = QVBoxLayout(self)
        outer.addWidget(scroll)
        outer.addWidget(self.problems_label)
        outer.addWidget(self.buttons)

    # Building -------------------------------------------------------------------------------

    def _build_recordings(self, cfg: AppConfig) -> QWidget:
        box = QGroupBox("Recordings")
        form = QFormLayout(box)
        self.output_edit = QLineEdit(cfg.output_root)
        self.browse_button = QPushButton("Browse…")
        self.browse_button.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.output_edit, 1)
        row.addWidget(self.browse_button)
        form.addRow("Output folder", row)
        self.max_height_combo = QComboBox()
        heights = _HEIGHTS if cfg.recording.max_height in _HEIGHTS else (*_HEIGHTS, cfg.recording.max_height)
        for height in heights:
            self.max_height_combo.addItem(f"{height}p", height)
        self.max_height_combo.setCurrentIndex(self.max_height_combo.findData(cfg.recording.max_height))
        form.addRow("Video height at most", self.max_height_combo)
        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(1, 120)
        self.fps_spin.setSuffix(" fps")
        self.fps_spin.setValue(cfg.recording.fps)
        form.addRow("Frame rate", self.fps_spin)
        return box

    def _build_obs(self, cfg: AppConfig) -> QWidget:
        box = QGroupBox("OBS")
        form = QFormLayout(box)
        self.host_edit = QLineEdit(cfg.obs.host)
        form.addRow("Host", self.host_edit)
        self.port_spin = QSpinBox()
        self.port_spin.setRange(0, 65535)
        self.port_spin.setSpecialValueText("Read from OBS")
        self.port_spin.setValue(cfg.obs.port or 0)
        form.addRow("Port", self.port_spin)
        self.password_edit = QLineEdit(cfg.obs.password_override or "")
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setPlaceholderText("Read from OBS")
        form.addRow("Password", self.password_edit)
        form.addRow(_note(OBS_PASSWORD_NOTE))
        return box

    def _build_sources(self, cfg: AppConfig) -> QWidget:
        box = QGroupBox("Text sources")
        column = QVBoxLayout(box)
        self.sources_table = QTableWidget(0, 3)
        self.sources_table.setHorizontalHeaderLabels(["On", "Name", "Address"])
        if (rows := self.sources_table.verticalHeader()) is not None:
            rows.hide()
        if (header := self.sources_table.horizontalHeader()) is not None:
            header.setSectionResizeMode(_ADDRESS, QHeaderView.ResizeMode.Stretch)
        for source in cfg.text_sources:
            self._add_row(source.id, source.name, source.uri, source.enabled)
        column.addWidget(self.sources_table)
        self.add_source_button = QPushButton("Add")
        self.add_source_button.clicked.connect(lambda: self._add_row(None, "New source", "localhost:", True))
        self.remove_source_button = QPushButton("Remove")
        self.remove_source_button.clicked.connect(self._remove_row)
        row = QHBoxLayout()
        row.addWidget(_note("The address is host:port, as the hooker's WebSocket server listens."), 1)
        row.addWidget(self.add_source_button)
        row.addWidget(self.remove_source_button)
        column.addLayout(row)
        return box

    def _build_subtitles(self, cfg: AppConfig, vad_addon: AddonService) -> QWidget:
        box = QGroupBox("Subtitles")
        form = QFormLayout(box)
        self.max_cue_spin = QSpinBox()
        self.max_cue_spin.setRange(MAX_CUE_SECONDS_MIN, MAX_CUE_SECONDS_MAX)
        self.max_cue_spin.setSuffix(" s")
        self.max_cue_spin.setValue(cfg.cue.max_cue_seconds)
        form.addRow("Longest cue", self.max_cue_spin)
        form.addRow(_note(MAX_CUE_NOTE))
        self.end_gap_spin = QSpinBox()
        self.end_gap_spin.setRange(0, 5000)
        self.end_gap_spin.setSingleStep(50)
        self.end_gap_spin.setSuffix(" ms")
        self.end_gap_spin.setValue(cfg.cue.end_gap_ms)
        form.addRow("Gap before the next cue", self.end_gap_spin)
        form.addRow(_note(END_GAP_NOTE))
        self.vad_check = QCheckBox("Trim cue ends to the voice (VAD)")
        self.vad_check.setChecked(cfg.vad.enabled)
        form.addRow(self.vad_check)
        form.addRow(_note(VAD_NOTE))
        status = " ".join(filter(None, (_VAD_STATUS_TEXT[vad_addon.status()], vad_addon.note)))
        self.vad_status_label = _note(status)
        form.addRow(self.vad_status_label)
        return box

    def _build_feed(self, cfg: AppConfig) -> QWidget:
        box = QGroupBox("Text feed")
        form = QFormLayout(box)
        self.feed_check = QCheckBox("Serve the text feed")
        self.feed_check.setChecked(cfg.feed.enabled)
        form.addRow(self.feed_check)
        self.http_port_spin = _port_spin(cfg.feed.http_port)
        form.addRow("Page port", self.http_port_spin)
        self.ws_port_spin = _port_spin(cfg.feed.ws_port)
        form.addRow("WebSocket port", self.ws_port_spin)
        form.addRow(_note(FEED_NOTE))
        return box

    def _build_control(self, cfg: AppConfig) -> QWidget:
        box = QGroupBox("Start and stop")
        form = QFormLayout(box)
        self.hotkey_edit: QLineEdit | None = None
        if self._windows:
            self.hotkey_edit = QLineEdit(cfg.hotkey)
            form.addRow("Hotkey (Start/Stop while armed)", self.hotkey_edit)
        else:
            form.addRow(_note(LINUX_CONTROL_NOTE))
        return box

    def _build_setup(self) -> QWidget:
        box = QGroupBox("Setup wizard")
        row = QHBoxLayout(box)
        row.addWidget(_note(SETUP_NOTE), 1)
        self.setup_buttons: dict[WizardStep, QPushButton] = {}
        for step, label in SETUP_STEPS:
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, step=step: self.setup_step_requested.emit(step))
            row.addWidget(button)
            self.setup_buttons[step] = button
        return box

    def take_setup(self, cfg: AppConfig) -> None:
        """A setup step run from here saved ``cfg``: show its output folder and OBS password.

        The other fields keep what the form shows; ``cfg`` becomes the base for the fields the form
        does not show.
        """
        self._cfg = cfg
        self.output_edit.setText(cfg.output_root)
        self.password_edit.setText(cfg.obs.password_override or "")

    def _add_row(self, source_id: str | None, name: str, uri: str, enabled: bool) -> None:
        row = self.sources_table.rowCount()
        self.sources_table.insertRow(row)
        on = QTableWidgetItem()
        on.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        on.setCheckState(Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked)
        self.sources_table.setItem(row, _ON, on)
        name_item = QTableWidgetItem(name)
        name_item.setData(_ID_ROLE, source_id)
        self.sources_table.setItem(row, _NAME, name_item)
        self.sources_table.setItem(row, _ADDRESS, QTableWidgetItem(uri))

    def _remove_row(self) -> None:
        row = self.sources_table.currentRow()
        if row >= 0:
            self.sources_table.removeRow(row)

    def _browse(self) -> None:
        start = os.path.expanduser(self.output_edit.text().strip())
        picker = QFileDialog(self, "Output folder", start)
        picker.setFileMode(QFileDialog.FileMode.Directory)
        picker.setOption(QFileDialog.Option.ShowDirsOnly)
        picker.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        picker.fileSelected.connect(self.output_edit.setText)
        picker.open()  # Not exec(): a nested event loop in a native picker can block the app.

    # The config -----------------------------------------------------------------------------

    def _cell(self, row: int, column: int) -> QTableWidgetItem:
        item = self.sources_table.item(row, column)
        if item is None:  # _add_row fills every cell
            raise LookupError(f"text source table cell {row},{column} is empty")
        return item

    def _text_sources(self) -> tuple[TextSourceConfig, ...]:
        rows = range(self.sources_table.rowCount())
        taken = {sid for row in rows if isinstance(sid := self._cell(row, _NAME).data(_ID_ROLE), str)}
        sources: list[TextSourceConfig] = []
        for row in rows:
            name = self._cell(row, _NAME).text().strip()
            source_id = self._cell(row, _NAME).data(_ID_ROLE)
            if not isinstance(source_id, str):
                source_id = _new_source_id(name, taken)
                taken.add(source_id)
            sources.append(
                TextSourceConfig(
                    id=source_id,
                    name=name,
                    uri=_address(self._cell(row, _ADDRESS).text()),
                    enabled=self._cell(row, _ON).checkState() == Qt.CheckState.Checked,
                )
            )
        return tuple(sources)

    def config(self) -> AppConfig:
        """The config as the form stands; ``problems`` says whether it can be saved."""
        cfg = self._cfg
        return replace(
            cfg,
            output_root=self.output_edit.text().strip(),
            obs=ObsSettings(
                host=self.host_edit.text().strip(),
                port=self.port_spin.value() or None,
                password_override=self.password_edit.text() or None,
            ),
            text_sources=self._text_sources(),
            feed=FeedSettings(
                enabled=self.feed_check.isChecked(),
                ws_port=self.ws_port_spin.value(),
                http_port=self.http_port_spin.value(),
            ),
            hotkey=cfg.hotkey if self.hotkey_edit is None else self.hotkey_edit.text().strip(),
            recording=RecordingSettings(max_height=self.max_height_combo.currentData(), fps=self.fps_spin.value()),
            cue=CueSettings(max_cue_seconds=self.max_cue_spin.value(), end_gap_ms=self.end_gap_spin.value()),
            vad=VadSettings(enabled=self.vad_check.isChecked()),
        )

    def problems(self, cfg: AppConfig) -> list[str]:
        """Why ``cfg`` cannot be saved; empty when it can."""
        problems: list[str] = []
        if not cfg.output_root:
            problems.append("the output folder is empty")
        if not cfg.obs.host:
            problems.append("the OBS host is empty")
        for number, source in enumerate(cfg.text_sources, start=1):
            if not source.name:
                problems.append(f"text source {number} has no name")
            if not source.uri:
                problems.append(f"text source {number} ({source.name}) has no address")
        if cfg.feed.ws_port == cfg.feed.http_port:
            problems.append("the text feed needs two different ports")
        if self.hotkey_edit is not None:
            try:
                parse_hotkey(cfg.hotkey)
            except HotkeyError as exc:
                problems.append(f"the hotkey is not valid ({exc})")
        return problems

    def accept(self) -> None:
        cfg = self.config()
        problems = self.problems(cfg)
        if problems:
            text = "; ".join(problems)
            self.problems_label.setText(f"Cannot save: {text}.")
            self.problems_label.show()
            return
        self.problems_label.hide()
        self.config_saved.emit(cfg)
        super().accept()
