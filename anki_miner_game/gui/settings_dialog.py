"""The settings dialog: every ``AppConfig`` field of spec 5 but ``last_game``, which it keeps as it is.

Two short groups, Recordings and While playing, and a collapsed Advanced part (UJ-21). The dialog saves
nothing: on Save it emits ``config_saved`` with the new config, and the caller stores it. The OBS port
and password are read from OBS's own WebSocket settings unless the user types an override; only a typed
password is ever stored (spec 11.1). ``obs.host`` is not shown: the stored value is kept and still
checked. The hotkey is Windows only (spec 16); on Linux the dialog says how to bind the CLI verbs.
Setup jobs sit where they are needed (UJ-22): Set up OBS… and Install…/Repair… ask for one wizard page
with ``setup_step_requested(WizardStep)``, Test… asks for the Game text page over the table's current
rows with ``test_sources_requested``; ``take_setup`` shows the OBS password such a step saved.
"""

import os
import shlex
import sys
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Final

from PyQt6.QtCore import QDir, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QKeySequence
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
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from anki_miner_game.gui.hotkey_win import HotkeyError, parse_hotkey
from anki_miner_game.gui.strings import SET_UP_OBS, install_elsewhere_text, size_mb
from anki_miner_game.gui.widgets.layout import clear_on_edit, error_label, fit_dialog, message_label, show_message
from anki_miner_game.gui.wizard import WizardStep, native_path
from anki_miner_game.interfaces.addons import AddonService
from anki_miner_game.models.addons import AddonStatus
from anki_miner_game.models.config import (
    AppConfig,
    CueSettings,
    FeedSettings,
    RecordingSettings,
    TextSourceConfig,
    VadSettings,
)
from anki_miner_game.models.constants import MAX_CUE_SECONDS_MAX, MAX_CUE_SECONDS_MIN
from anki_miner_game.models.messages import OBS_SOURCE_ID

RESERVED_SOURCE_IDS: Final = frozenset({OBS_SOURCE_ID, "clipboard", "ocr"})
"""Source ids the app uses itself: the OBS light, the clipboard source and the OCR source; a text
source the user adds never gets one."""

VIDEO_PRESETS: Final = (
    ("1080p, 30 fps", 1080, 30),
    ("720p, 30 fps (smaller files)", 720, 30),
    ("1080p, 60 fps", 1080, 60),
    ("720p, 60 fps", 720, 60),
)
"""UJ-23: the Video choices (label, max height, fps); a stored pair outside them is an extra item."""
TRIM_TEXT: Final = "Trim each line's end to where the voice stops"
FEED_TEXT: Final = "Show the lines on a web page for dictionary lookups"
FEED_PAGE_URL: Final = "http://127.0.0.1:{port}/"
"""The feed page's address, as ``FeedServer.page_url`` gives it (the GUI may not import ``feed``)."""
HOTKEY_LABEL: Final = "Start/stop recording hotkey"
END_GAP_NOTE: Final = (
    "The gap is Anki Miner's audio padding (0.3 s) plus 50 ms; if you raise that padding, raise the gap as much."
)
LINUX_CONTROL_NOTE: Final = (
    "Linux has no global hotkey. In your desktop's keyboard shortcut settings, bind the app's command "
    "followed by --toggle (or --start, --stop, --arm <game>). The command is anki_miner_game for the "
    ".deb, the full path of the .AppImage file for the AppImage, and, for the .tar.gz, the full path "
    "of AnkiMinerGame/anki_miner_game in the folder you extracted it to."
)
"""Only the .deb puts ``anki_miner_game`` on PATH (packaging/nfpm.yaml)."""
DEB_DIR: Final = PurePosixPath("/opt/anki-miner-game")
"""Where the .deb puts the bundle; it links ``/usr/bin/anki_miner_game`` to its launcher (packaging/nfpm.yaml)."""
COMMAND: Final = "anki_miner_game"
BIND_TEXT: Final = "Bind this command to a key in your desktop's keyboard settings:"
_PORTABLE: Final = QKeySequence.SequenceFormat.PortableText
BUSY_FOLDER_TEXT: Final = "Press Done playing before changing the folder."
"""D-05: the session being recorded or about to be finishes in the folder it started in."""


def same_folder(a: str, b: str) -> bool:
    """Whether two folder texts name one folder once ``~`` is expanded and the path normalised."""

    def norm(path: str) -> str:
        return os.path.normcase(os.path.normpath(os.path.expanduser(path)))

    return norm(a) == norm(b)


def folder_problem(text: str) -> str | None:
    """B4-03: the setup wizard's former folder rules; creates the folder when it can."""
    folder = Path(text).expanduser()
    if not folder.is_absolute():
        return "Choose a full path, such as the one Browse gives."
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"This folder cannot be created: {exc.strerror or exc}."
    if not os.access(folder, os.W_OK):
        return "This folder is not writable. Choose another one."
    return None


_SOURCE_HEADERS: Final = ("On", "Name", "Address (host:port)")
_ON, _NAME, _ADDRESS = range(3)
_ID_ROLE: Final = Qt.ItemDataRole.UserRole


def linux_toggle_command(
    *, environ: Mapping[str, str] | None = None, frozen: bool | None = None, executable: str | None = None
) -> str | None:
    """UJ-24: the command a Linux user binds to start and stop recording, for this install; ``None``
    when running from source, where the dialog explains every package instead (``LINUX_CONTROL_NOTE``).

    The AppImage is ``$APPIMAGE``; the .deb's bundle under ``/opt/anki-miner-game/`` has ``anki_miner_game``
    on PATH; the .tar.gz's launcher sits beside the bundle's executable (its libstdc++ check), else the
    executable itself.
    """
    environ = os.environ if environ is None else environ
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    executable = sys.executable if executable is None else executable
    if not frozen:
        return None
    appimage = environ.get("APPIMAGE")
    if appimage:
        return f"{shlex.quote(appimage)} --toggle"
    if PurePosixPath(executable).is_relative_to(DEB_DIR):
        return f"{COMMAND} --toggle"
    launcher = Path(executable).with_name(COMMAND)
    program = launcher if launcher.is_file() else Path(executable)
    return f"{shlex.quote(str(program))} --toggle"


def vad_install_text(size_bytes: int) -> str:
    """UJ-21: the line shown instead of the trim check while the voice-trimming add-on is not ready."""
    return f"Trimming each line to the voice needs the voice-trimming add-on (about {size_mb(size_bytes)} MB)."


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


def _hotkey_problem(text: str) -> str | None:
    """One sentence about a hotkey the app cannot register, or ``None`` (UJ-31: no nested sentence)."""
    if not text.strip():
        return "No start/stop recording hotkey is set."
    try:
        parse_hotkey(text)
    except HotkeyError:
        return f"The hotkey {text} cannot be used: press one key together with Ctrl, Shift, Alt or Win."
    return None


class SettingsDialog(QDialog):
    """Edit the app's settings (spec 5); emits ``config_saved(AppConfig)`` on Save."""

    config_saved = pyqtSignal(object)
    """The new ``AppConfig``, on Save."""
    setup_step_requested = pyqtSignal(object)
    """A ``WizardStep`` to open alone: ``OBS`` (Set up OBS…) or ``ADDONS`` (Install…/Repair…)."""
    test_sources_requested = pyqtSignal(object)
    """The table's current rows (``tuple[TextSourceConfig, ...]``) for the Game text page (Test…)."""

    def __init__(
        self,
        cfg: AppConfig,
        *,
        vad_addon: AddonService,
        busy: Callable[[], bool] = lambda: False,
        platform: str | None = None,
        open_url: Callable[[QUrl], object] = QDesktopServices.openUrl,
        parent: QWidget | None = None,
    ) -> None:
        """``platform`` defaults to ``sys.platform``; ``open_url`` opens the feed page's link. ``busy`` says
        whether a game is ready or recording; it is asked at Save (D-05)."""
        super().__init__(parent)
        self._cfg = cfg
        self._vad_addon = vad_addon
        self._busy = busy
        self._open_url = open_url
        self._windows = (platform or sys.platform) == "win32"
        self.setWindowTitle("Settings")
        content = QWidget()
        column = QVBoxLayout(content)
        column.addWidget(self._build_recordings(cfg))
        column.addWidget(self._build_playing(cfg))
        self.advanced_button = QToolButton()
        self.advanced_button.setText("Advanced")
        self.advanced_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.advanced_button.setArrowType(Qt.ArrowType.RightArrow)
        self.advanced_button.setAutoRaise(True)
        self.advanced_button.clicked.connect(self._toggle_advanced)
        column.addWidget(self.advanced_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.advanced = self._build_advanced(cfg)
        self.advanced.hide()  # collapsed at every open
        column.addWidget(self.advanced)
        column.addStretch(1)
        self._scroll = QScrollArea()
        self._scroll.setWidget(content)
        self.problems_label = error_label()
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        outer = QVBoxLayout(self)
        outer.addWidget(self._scroll)
        outer.addWidget(self.problems_label)
        outer.addWidget(self.buttons)
        self._forms = (self._recordings_form, self._playing_form, self._obs_form, self._timing_form)
        self.feed_check.toggled.connect(self._feed_changed)
        self.http_port_spin.valueChanged.connect(self._feed_changed)
        self._feed_changed()
        self.refresh_addons()
        clear_on_edit(self.problems_label, self)  # UJ-31: a stale "Cannot save" goes at the next edit
        fit_dialog(self, scroll=self._scroll, forms=self._forms)

    # Building -------------------------------------------------------------------------------

    def _build_recordings(self, cfg: AppConfig) -> QWidget:
        box = QGroupBox("Recordings")
        form = self._recordings_form = QFormLayout(box)
        self._shown_root = native_path(cfg.output_root)  # UJ-33: the real folder, not "~/..."
        self.output_edit = QLineEdit(self._shown_root)
        self.browse_button = QPushButton("Browse…")
        self.browse_button.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.output_edit, 1)
        row.addWidget(self.browse_button)
        form.addRow("Save sessions in", row)
        self.video_combo = QComboBox()
        stored = (cfg.recording.max_height, cfg.recording.fps)
        presets = [(label, (height, fps)) for label, height, fps in VIDEO_PRESETS]
        if stored not in [data for _label, data in presets]:
            presets.append((f"{stored[0]}p, {stored[1]} fps", stored))  # a hand-edited pair is kept
        for label, data in presets:
            self.video_combo.addItem(label, data)
        # Not findData: PyQt compares Python item data by identity, so an equal tuple is not found.
        self.video_combo.setCurrentIndex([data for _label, data in presets].index(stored))
        form.addRow("Video", self.video_combo)
        self.vad_check = QCheckBox(TRIM_TEXT)
        self.vad_check.setChecked(cfg.vad.enabled)
        form.addRow(self.vad_check)
        self.vad_install_label = message_label()
        self.vad_install_button = QPushButton()
        self.vad_install_button.clicked.connect(lambda: self.setup_step_requested.emit(WizardStep.ADDONS))
        self.vad_install_row = QWidget()
        line = QHBoxLayout(self.vad_install_row)
        line.setContentsMargins(0, 0, 0, 0)
        line.addWidget(self.vad_install_label, 1)
        line.addWidget(self.vad_install_button, 0, Qt.AlignmentFlag.AlignTop)
        form.addRow(self.vad_install_row)
        return box

    def _build_playing(self, cfg: AppConfig) -> QWidget:
        box = QGroupBox("While playing")
        form = self._playing_form = QFormLayout(box)
        self.feed_check = QCheckBox(FEED_TEXT)
        self.feed_check.setChecked(cfg.feed.enabled)
        form.addRow(self.feed_check)
        self.feed_link = QLabel()
        self.feed_link.setTextFormat(Qt.TextFormat.RichText)
        self.feed_link.setOpenExternalLinks(False)
        self.feed_link.linkActivated.connect(lambda href: self._open_url(QUrl(href)))
        form.addRow(self.feed_link)
        self.hotkey_edit: QKeySequenceEdit | None = None
        self._hotkey_edited = False
        if self._windows:
            self.hotkey_edit = QKeySequenceEdit(QKeySequence.fromString(cfg.hotkey, _PORTABLE))
            self.hotkey_edit.setMaximumSequenceLength(1)
            self.hotkey_edit.setClearButtonEnabled(True)
            self.hotkey_edit.keySequenceChanged.connect(self._hotkey_changed)
            form.addRow(HOTKEY_LABEL, self.hotkey_edit)
        else:
            command = linux_toggle_command()
            self.control_label = _note(f"{BIND_TEXT} {command}" if command else LINUX_CONTROL_NOTE)
            self.control_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            form.addRow(self.control_label)
        return box

    def _build_advanced(self, cfg: AppConfig) -> QWidget:
        advanced = QWidget()
        column = QVBoxLayout(advanced)
        column.setContentsMargins(0, 0, 0, 0)
        column.addWidget(self._build_sources(cfg))
        column.addWidget(self._build_obs(cfg))
        column.addWidget(self._build_timing(cfg))
        column.addWidget(self._build_feed_ports(cfg))
        return advanced

    def _build_sources(self, cfg: AppConfig) -> QWidget:
        box = QGroupBox("Text hookers")
        column = QVBoxLayout(box)
        self.sources_table = QTableWidget(0, len(_SOURCE_HEADERS))
        self.sources_table.setHorizontalHeaderLabels(list(_SOURCE_HEADERS))
        self.sources_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        if (rows := self.sources_table.verticalHeader()) is not None:
            rows.hide()
        if (header := self.sources_table.horizontalHeader()) is not None:
            header.setSectionResizeMode(_ON, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(_NAME, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(_ADDRESS, QHeaderView.ResizeMode.Stretch)
        for source in cfg.text_sources:
            self._add_row(source.id, source.name, source.uri, source.enabled)
        self._fit_table()
        column.addWidget(self.sources_table)
        self.test_sources_button = QPushButton("Test…")
        self.test_sources_button.clicked.connect(lambda: self.test_sources_requested.emit(self._text_sources()))
        self.add_source_button = QPushButton("Add")
        self.add_source_button.clicked.connect(self._add_new_row)
        self.remove_source_button = QPushButton("Remove")
        self.remove_source_button.clicked.connect(self._remove_row)
        row = QHBoxLayout()
        row.addStretch(1)
        for button in (self.test_sources_button, self.add_source_button, self.remove_source_button):
            row.addWidget(button)
        column.addLayout(row)
        return box

    def _build_obs(self, cfg: AppConfig) -> QWidget:
        box = QGroupBox("OBS connection")
        form = self._obs_form = QFormLayout(box)
        self.port_spin = QSpinBox()
        self.port_spin.setRange(0, 65535)
        self.port_spin.setSpecialValueText("Read from OBS")
        self.port_spin.setValue(cfg.obs.port or 0)
        form.addRow("Port", self.port_spin)
        self.password_edit = QLineEdit(cfg.obs.password_override or "")
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setPlaceholderText("Read from OBS")
        self.setup_obs_button = QPushButton(SET_UP_OBS)
        self.setup_obs_button.clicked.connect(lambda: self.setup_step_requested.emit(WizardStep.OBS))
        row = QHBoxLayout()
        row.addWidget(self.password_edit, 1)
        row.addWidget(self.setup_obs_button)
        form.addRow("Password", row)
        return box

    def _build_timing(self, cfg: AppConfig) -> QWidget:
        box = QGroupBox("Subtitle timing")
        form = self._timing_form = QFormLayout(box)
        self.max_cue_spin = QSpinBox()
        self.max_cue_spin.setRange(MAX_CUE_SECONDS_MIN, MAX_CUE_SECONDS_MAX)
        self.max_cue_spin.setSuffix(" s")
        self.max_cue_spin.setValue(cfg.cue.max_cue_seconds)
        form.addRow("Longest line", self.max_cue_spin)
        self.end_gap_spin = QSpinBox()
        self.end_gap_spin.setRange(0, 5000)
        self.end_gap_spin.setSingleStep(50)
        self.end_gap_spin.setSuffix(" ms")
        self.end_gap_spin.setValue(cfg.cue.end_gap_ms)
        form.addRow("Gap before the next line", self.end_gap_spin)
        form.addRow(_note(END_GAP_NOTE))
        return box

    def _build_feed_ports(self, cfg: AppConfig) -> QWidget:
        self.feed_ports = QGroupBox("Text feed ports")
        row = QHBoxLayout(self.feed_ports)
        self.http_port_spin = _port_spin(cfg.feed.http_port)
        self.ws_port_spin = _port_spin(cfg.feed.ws_port)
        row.addWidget(QLabel("Page"))
        row.addWidget(self.http_port_spin)
        row.addSpacing(16)
        row.addWidget(QLabel("WebSocket"))
        row.addWidget(self.ws_port_spin)
        row.addStretch(1)
        return self.feed_ports

    # Reacting -------------------------------------------------------------------------------

    def refresh_addons(self) -> None:
        """Read the voice-trimming add-on's status again (after an install the wizard ran over this dialog)."""
        status = self._vad_addon.status()
        ready = status is AddonStatus.READY
        self.vad_check.setVisible(ready)  # hidden, it keeps the stored vad.enabled
        self.vad_install_row.setVisible(not ready)
        show_message(self.vad_install_label, "" if ready else vad_install_text(self._vad_addon.size_bytes))
        self.vad_install_button.setText(install_elsewhere_text(status))

    def take_setup(self, cfg: AppConfig) -> None:
        """A setup step run from here saved ``cfg``: show its OBS password.

        The other fields keep what the form shows; ``cfg`` becomes the base for the fields the form
        does not show.
        """
        self._cfg = cfg
        self.password_edit.setText(cfg.obs.password_override or "")

    def _hotkey_changed(self, _keys: QKeySequence) -> None:
        """Only a chord the user pressed replaces the stored one: Qt cannot show every stored spelling."""
        self._hotkey_edited = True

    def _feed_changed(self, *_args: object) -> None:
        on = self.feed_check.isChecked()
        url = FEED_PAGE_URL.format(port=self.http_port_spin.value())
        self.feed_link.setText(f'<a href="{url}">{url}</a>')
        self.feed_link.setVisible(on)
        self.feed_ports.setEnabled(on)

    def _toggle_advanced(self) -> None:
        opened = self.advanced.isHidden()
        self.advanced.setVisible(opened)
        self.advanced_button.setArrowType(Qt.ArrowType.DownArrow if opened else Qt.ArrowType.RightArrow)
        fit_dialog(self, scroll=self._scroll, forms=self._forms)

    def _add_row(self, source_id: str | None, name: str, uri: str, enabled: bool) -> int:
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
        return row

    def _add_new_row(self) -> None:
        row = self._add_row(None, "New source", "localhost:", True)
        self._fit_table()
        self.sources_table.setCurrentCell(row, _NAME)
        self.sources_table.editItem(self._cell(row, _NAME))

    def _remove_row(self) -> None:
        row = self.sources_table.currentRow()
        if row >= 0:
            self.sources_table.removeRow(row)
            self._fit_table()

    def _fit_table(self) -> None:
        """UJ-21: the table is as tall as its rows, so the dialog, not the table, scrolls."""
        table = self.sources_table
        header = table.horizontalHeader()
        header_height = header.sizeHint().height() if header is not None else 0
        rows = sum(table.rowHeight(row) for row in range(table.rowCount()))
        table.setFixedHeight(2 * table.frameWidth() + header_height + rows)

    def _browse(self) -> None:
        start = os.path.expanduser(self.output_edit.text().strip())
        picker = QFileDialog(self, "Output folder", start)
        picker.setFileMode(QFileDialog.FileMode.Directory)
        picker.setOption(QFileDialog.Option.ShowDirsOnly)
        picker.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        picker.fileSelected.connect(self._chosen)
        picker.open()  # Not exec(): a nested event loop in a native picker can block the app.

    def _chosen(self, folder: str) -> None:
        self.output_edit.setText(QDir.toNativeSeparators(folder))
        self.problems_label.clear()  # a chosen folder is an edit; setText emits no textEdited

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
        max_height, fps = self.video_combo.currentData()
        return replace(
            cfg,
            output_root=self._output_root(),
            obs=replace(
                cfg.obs, port=self.port_spin.value() or None, password_override=self.password_edit.text() or None
            ),
            text_sources=self._text_sources(),
            feed=FeedSettings(
                enabled=self.feed_check.isChecked(),
                ws_port=self.ws_port_spin.value(),
                http_port=self.http_port_spin.value(),
            ),
            hotkey=(
                self.hotkey_edit.keySequence().toString(_PORTABLE)
                if self.hotkey_edit is not None and self._hotkey_edited
                else cfg.hotkey
            ),
            recording=RecordingSettings(max_height=max_height, fps=fps),
            cue=CueSettings(max_cue_seconds=self.max_cue_spin.value(), end_gap_ms=self.end_gap_spin.value()),
            vad=VadSettings(enabled=self.vad_check.isChecked()),
        )

    def _output_root(self) -> str:
        """The field's folder; while it still shows the stored folder, the stored text itself (UJ-33)."""
        typed = self.output_edit.text().strip()
        return self._cfg.output_root if typed == self._shown_root else typed

    def problems(self, cfg: AppConfig) -> list[str]:
        """Why ``cfg`` cannot be saved, one sentence each; empty when it can."""
        problems: list[str] = []
        if not cfg.output_root:
            problems.append("The output folder is empty.")
        elif not same_folder(cfg.output_root, self._cfg.output_root):
            if self._busy():
                problems.append(BUSY_FOLDER_TEXT)
            elif (problem := folder_problem(cfg.output_root)) is not None:
                problems.append(problem)
        if not cfg.obs.host:
            problems.append("The OBS host in the settings file is empty.")
        for number, source in enumerate(cfg.text_sources, start=1):
            if not source.name:
                problems.append(f"Text source {number} has no name.")
            if not source.uri:
                problems.append(f"Text source {number} ({source.name}) has no address.")
        if cfg.feed.ws_port == cfg.feed.http_port:
            problems.append("The text feed needs two different ports.")
        if self.hotkey_edit is not None and (problem := _hotkey_problem(cfg.hotkey)) is not None:
            problems.append(problem)
        return problems

    def accept(self) -> None:
        cfg = self.config()
        problems = self.problems(cfg)
        if problems:
            self.problems_label.set_error("Cannot save. " + " ".join(problems))
            return
        self.problems_label.clear()
        self.config_saved.emit(cfg)
        super().accept()
