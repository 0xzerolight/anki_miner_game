"""Recent sessions (spec 16 item 5): name, duration, cue count and VAD state; **Open folder**.

A row is one session manifest under the output folder: in its game folder once placed, in
``_incoming/`` while ``finalise_pending``. A manifest still ``recording`` is no finished session and
is not listed. The rows are the ``RECENT_LIMIT`` manifests written last, newest session first.

VAD **Re-run** and **Restore untrimmed subtitle** live in a row's context menu (master plan ruling on
spec 16 and 13.3) and go to ``VadJobs``, which only queues; ``Presenter.vad_progress`` and
``vad_finished`` then drive the row. A session with a job asked for or running in this run offers
neither until that job ends.

Interrupted passes (``VadJobs`` docstring): the first ``load`` hands each listed manifest whose
``state`` is ``vad_running`` or whose ``vad.state`` is ``queued`` to ``VadJobs.rerun``, once per
launch, so load before the session actor can finalise (and ``queue``) anything. A pass pending at a
quit was queued last, so its manifest is among the ones written last and is listed.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from PyQt6.QtCore import QPoint, Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QMenu,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from anki_miner_game.gui.widgets import clock_text
from anki_miner_game.interfaces.addons import VadJobs
from anki_miner_game.models.codec import DecodeError
from anki_miner_game.models.manifest import ManifestState, SessionManifest, VadState, from_json

log = logging.getLogger(__name__)

RECENT_LIMIT: Final = 30
MANIFEST_SUFFIX: Final = ".session.json"
RERUN_TEXT: Final = "Re-run VAD"
RESTORE_TEXT: Final = "Restore untrimmed subtitle"
COLUMNS: Final = ("Session", "Duration", "Cues", "VAD", "")

UrlOpener = Callable[[QUrl], object]
"""``QDesktopServices.openUrl`` by default; the composition may pass one with a cleaned environment."""

_PLACED: Final = (ManifestState.READY, ManifestState.VAD_RUNNING)
_VAD_TEXT: Final = {
    VadState.QUEUED: "VAD queued",
    VadState.DONE: "VAD done",
    VadState.FAILED: "VAD failed",
    VadState.UNAVAILABLE: "VAD unavailable",
    VadState.RESTORED: "Untrimmed subtitle",
}
_VAD_RUNNING_TEXT: Final = "VAD running"


@dataclass(frozen=True)
class SessionRow:
    manifest_path: Path
    manifest: SessionManifest

    @property
    def name(self) -> str:
        files = self.manifest.files
        return Path(files.video).stem if files is not None else f"{self.manifest.game.title} (not moved yet)"

    @property
    def duration(self) -> str:
        try:
            started = datetime.fromisoformat(self.manifest.started_at)
            stopped = datetime.fromisoformat(self.manifest.stopped_at or "")
        except ValueError:
            return ""
        return clock_text((stopped - started).total_seconds())

    @property
    def has_subtitle(self) -> bool:
        """Placed in its game folder with a subtitle: what a VAD job works on (``VadTrimmer``)."""
        files = self.manifest.files
        return (
            self.manifest.state in _PLACED
            and files is not None
            and files.subtitle is not None
            and bool(self.manifest.live_cues)
        )

    @property
    def interrupted(self) -> bool:
        """A pass left pending or running by the last run (``VadJobs`` docstring)."""
        vad = self.manifest.vad
        return self.manifest.state is ManifestState.VAD_RUNNING or (vad is not None and vad.state is VadState.QUEUED)

    @property
    def vad_text(self) -> str:
        m = self.manifest
        if m.state is ManifestState.FINALISE_PENDING:
            return "Not moved yet; retried at next launch"
        if not self.has_subtitle:
            return "No subtitle"
        if m.state is ManifestState.VAD_RUNNING:
            return _VAD_RUNNING_TEXT
        if m.vad is None:
            return "No VAD pass"
        text = _VAD_TEXT[m.vad.state]
        return f"{text}: {m.vad.message}" if m.vad.message else text


def read_session(path: Path) -> SessionRow | None:
    """The row for one manifest; ``None`` when it cannot be read (logged)."""
    try:
        return SessionRow(path, from_json(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, DecodeError) as exc:
        log.warning("session manifest %s left out of the recent sessions: %s", path, exc)
        return None


def scan_sessions(root: Path, limit: int = RECENT_LIMIT) -> list[SessionRow]:
    """The finished sessions under ``root`` among the manifests written last, newest session first."""
    found: list[tuple[float, Path]] = []
    try:
        for path in root.glob(f"*/*{MANIFEST_SUFFIX}"):
            try:
                found.append((path.stat().st_mtime, path))
            except OSError:
                continue
    except OSError as exc:
        log.warning("cannot list the sessions in %s: %s", root, exc)
    rows: list[SessionRow] = []
    for _mtime, path in sorted(found, reverse=True):
        row = read_session(path)
        if row is not None and row.manifest.state is not ManifestState.RECORDING:
            rows.append(row)
            if len(rows) == limit:
                break
    return sorted(rows, key=lambda row: row.manifest.started_at, reverse=True)


class RecentSessions(QWidget):
    """The recent-session rows; see the module docstring."""

    def __init__(
        self,
        *,
        vad_jobs: VadJobs | None = None,
        open_url: UrlOpener = QDesktopServices.openUrl,
        limit: int = RECENT_LIMIT,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._vad_jobs = vad_jobs
        self._open_url = open_url
        self._limit = limit
        self._root: Path | None = None
        self._loaded_once = False
        self._rows: list[SessionRow] = []
        self._busy: set[Path] = set()
        """Sessions with a job asked for or running in this run."""
        self._progress: dict[Path, str] = {}
        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(COLUMNS))
        self.tree.setHeaderLabels(COLUMNS)
        self.tree.setRootIsDecorated(False)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        header = self.tree.header()
        if header is not None:
            header.setStretchLastSection(False)
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            for column in range(1, len(COLUMNS)):
                header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tree)

    # --- loading --------------------------------------------------------------------------------

    def load(self, root: Path) -> None:
        """List the sessions under ``root``; the first call hands interrupted passes to ``rerun``."""
        self._root = root
        self._rows = scan_sessions(root, self._limit)
        if not self._loaded_once:
            self._loaded_once = True
            if self._vad_jobs is not None:
                for row in self._rows:
                    if row.interrupted:
                        self._vad_jobs.rerun(row.manifest_path)
                        self._busy.add(row.manifest_path)
        self._fill()

    def reload(self) -> None:
        """List again from the last ``load``'s folder (after a finalise, say)."""
        if self._root is not None:
            self.load(self._root)

    def rows(self) -> list[SessionRow]:
        return list(self._rows)

    # --- VAD jobs -------------------------------------------------------------------------------

    def vad_progress(self, manifest_path: Path, done_ms: int, total_ms: int | None) -> None:
        if not self._listed(manifest_path):
            return
        self._busy.add(manifest_path)
        self._progress[manifest_path] = f"VAD {min(100, done_ms * 100 // total_ms)}%" if total_ms else _VAD_RUNNING_TEXT
        self._fill()

    def vad_finished(self, manifest_path: Path, state: VadState) -> None:
        """The job wrote its outcome into the manifest first (``VadJobs``): read the row back."""
        self._busy.discard(manifest_path)
        self._progress.pop(manifest_path, None)
        for i, row in enumerate(self._rows):
            if row.manifest_path == manifest_path:
                self._rows[i] = read_session(manifest_path) or row
        self._fill()

    def _ask(self, row: SessionRow, rerun: bool) -> None:
        jobs = self._vad_jobs
        if jobs is None:
            return
        (jobs.rerun if rerun else jobs.restore)(row.manifest_path)
        self._busy.add(row.manifest_path)
        self._fill()

    # --- the widget -----------------------------------------------------------------------------

    def cells(self) -> list[tuple[str, str, str, str]]:
        """``(name, duration, cues, VAD)`` per row, as shown."""
        shown: list[tuple[str, str, str, str]] = []
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if item is not None:
                shown.append((item.text(0), item.text(1), item.text(2), item.text(3)))
        return shown

    def item(self, index: int) -> QTreeWidgetItem:
        item = self.tree.topLevelItem(index)
        if item is None:
            raise IndexError(index)
        return item

    def open_button(self, manifest_path: Path) -> QPushButton:
        for i, row in enumerate(self._rows):
            if row.manifest_path == manifest_path:
                button = self.tree.itemWidget(self.item(i), len(COLUMNS) - 1)
                if isinstance(button, QPushButton):
                    return button
        raise KeyError(manifest_path)

    def menu_for(self, manifest_path: Path) -> QMenu:
        """The row's context menu: Re-run VAD and Restore untrimmed subtitle, each only when it can run."""
        row = next(row for row in self._rows if row.manifest_path == manifest_path)
        ready = self._vad_jobs is not None and row.has_subtitle and not self._pending(row)
        vad = row.manifest.vad
        menu = QMenu(self)
        rerun = menu.addAction(RERUN_TEXT)
        restore = menu.addAction(RESTORE_TEXT)
        if rerun is not None:
            rerun.setEnabled(ready)
            rerun.triggered.connect(lambda _checked=False: self._ask(row, rerun=True))
        if restore is not None:
            restore.setEnabled(ready and vad is not None and vad.state is VadState.DONE)
            restore.triggered.connect(lambda _checked=False: self._ask(row, rerun=False))
        return menu

    def _pending(self, row: SessionRow) -> bool:
        return row.manifest_path in self._busy or row.interrupted

    def _listed(self, manifest_path: Path) -> bool:
        return any(row.manifest_path == manifest_path for row in self._rows)

    def _fill(self) -> None:
        self.tree.clear()
        for row in self._rows:
            path = row.manifest_path
            if path in self._progress:
                vad = self._progress[path]
            elif path in self._busy:
                vad = _VAD_TEXT[VadState.QUEUED]
            else:
                vad = row.vad_text
            item = QTreeWidgetItem([row.name, row.duration, str(len(row.manifest.live_cues)), vad, ""])
            item.setToolTip(0, str(path.parent))
            item.setToolTip(3, vad)
            self.tree.addTopLevelItem(item)
            button = QPushButton("Open folder")
            button.clicked.connect(lambda _checked=False, folder=path.parent: self._open_folder(folder))
            self.tree.setItemWidget(item, len(COLUMNS) - 1, button)

    def _open_folder(self, folder: Path) -> None:
        self._open_url(QUrl.fromLocalFile(str(folder)))

    def _context_menu(self, pos: QPoint) -> None:
        item = self.tree.itemAt(pos)
        if item is None:
            return
        index = self.tree.indexOfTopLevelItem(item)
        if 0 <= index < len(self._rows):
            menu = self.menu_for(self._rows[index].manifest_path)
            viewport = self.tree.viewport()
            menu.exec(viewport.mapToGlobal(pos) if viewport is not None else pos)
            menu.deleteLater()
