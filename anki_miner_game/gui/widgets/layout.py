"""Layout and message helpers the windows and dialogs share (UJ-13, UJ-19, UJ-30, UJ-31, UJ-11).

Sizes follow the screen the widget is on, so nothing is clipped on a 1366x768 laptop or at 150 %.
Errors have one look: the style's critical icon and plain text in the palette's colour (never a
colour token: contrast follows the light or dark theme). Message and error labels are hidden while
they have no text. Put a label in its layout before giving it text: showing a widget that has no
parent yet would open it as a window of its own.
"""

from collections.abc import Sequence
from typing import Final

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QGuiApplication, QPixmap
from PyQt6.QtWidgets import (
    QAbstractButton,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSpinBox,
    QStyle,
    QTableWidget,
    QWidget,
)

from anki_miner_game.models.messages import BannerLevel

SCREEN_MARGIN: Final = QSize(40, 80)
"""Room left for the title bar and the taskbar when a window is sized to the screen."""
DIALOG_SCREEN_FRACTION: Final = 0.9
ICON_PX: Final = 16
UNBOUNDED: Final = QSize(16_777_215, 16_777_215)
"""Qt's ``QWIDGETSIZE_MAX``: what ``available_size`` answers when there is no screen at all."""
_FITTED: Final = "_layout_fitted"
"""Dynamic property ``fit_dialog`` sets, so a later call only grows the dialog."""
_LEVEL_ICON: Final = {
    BannerLevel.INFO: QStyle.StandardPixmap.SP_MessageBoxInformation,
    BannerLevel.WARNING: QStyle.StandardPixmap.SP_MessageBoxWarning,
    BannerLevel.ERROR: QStyle.StandardPixmap.SP_MessageBoxCritical,
}


def available_size(widget: QWidget) -> QSize:
    """The available geometry of the widget's screen; the primary screen's before the widget has one."""
    screen = widget.screen() or QGuiApplication.primaryScreen()
    return UNBOUNDED if screen is None else screen.availableGeometry().size()


def screen_bounded(widget: QWidget, wanted: QSize, *, margin: QSize = SCREEN_MARGIN) -> QSize:
    """``wanted``, no larger than the widget's screen less ``margin`` (UJ-13 main window, UJ-19 wizard)."""
    room = available_size(widget)
    return wanted.boundedTo(QSize(room.width() - margin.width(), room.height() - margin.height()))


def fit_dialog(dialog: QDialog, *, scroll: QScrollArea | None = None, forms: Sequence[QFormLayout] = ()) -> None:
    """UJ-30; call after building and again after a disclosure opens or closes.

    ``scroll``: frameless, widget-resizable, horizontal scroll bar always off. Every form:
    ``ExpandingFieldsGrow``, labels ``AlignLeft | AlignVCenter``, one shared label-column width (the
    widest label over all ``forms``, set as each label's minimum width); spin boxes and combos keep
    their own width. Minimum width = content minimum + chrome + vertical scroll-bar width. Size =
    content size hint + chrome + scroll-bar width, bounded to ``DIALOG_SCREEN_FRACTION`` of
    ``available_size``; a later call only grows the dialog, never shrinks it. A shown dialog that grows
    moves back inside its screen's available geometry: a resize keeps the top-left.
    """
    _align_forms(forms)
    for form in forms:
        form.activate()  # a form in a group box measures its widened labels only now; the group then asks again
    outer = dialog.layout()
    if outer is not None:
        outer.activate()
    hint, minimum = dialog.sizeHint(), dialog.minimumSizeHint()
    bar = 0
    if scroll is not None:
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = scroll.widget()
        if inner is not None:
            style = dialog.style()
            bar = 0 if style is None else style.pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent)
            chrome = _chrome(dialog, scroll)
            content, content_min = inner.sizeHint(), inner.minimumSizeHint()
            hint = QSize(max(hint.width(), content.width() + chrome.width() + bar), content.height() + chrome.height())
            minimum = QSize(max(minimum.width(), content_min.width() + chrome.width() + bar), minimum.height())
    room = available_size(dialog)
    bound = QSize(int(room.width() * DIALOG_SCREEN_FRACTION), int(room.height() * DIALOG_SCREEN_FRACTION))
    dialog.setMinimumWidth(min(minimum.width(), bound.width()))
    size = hint.boundedTo(bound)
    if dialog.property(_FITTED):
        size = size.expandedTo(dialog.size())
    dialog.setProperty(_FITTED, True)
    dialog.resize(size)
    if dialog.isVisible():
        _keep_on_screen(dialog)


def _keep_on_screen(window: QWidget) -> None:
    """Move ``window`` so its frame lies inside its screen's available geometry, as far as it fits."""
    screen = window.screen()
    if screen is None:
        return
    room, frame = screen.availableGeometry(), window.frameGeometry()
    x = max(room.left(), min(frame.left(), room.left() + room.width() - frame.width()))
    y = max(room.top(), min(frame.top(), room.top() + room.height() - frame.height()))
    if (x, y) != (frame.left(), frame.top()):
        window.move(x, y)  # a window's position is its frame's


def _chrome(dialog: QDialog, scroll: QScrollArea) -> QSize:
    """What the dialog adds around ``scroll``: its margins across, and every other row's height."""
    outer = dialog.layout()
    margins = outer.contentsMargins() if outer is not None else None
    across = 0 if margins is None else margins.left() + margins.right()
    return QSize(across, max(0, dialog.sizeHint().height() - scroll.sizeHint().height()))


def _align_forms(forms: Sequence[QFormLayout]) -> None:
    labels: list[QLabel] = []
    for form in forms:
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        for row in range(form.rowCount()):
            item = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
            label = item.widget() if item is not None else None
            if isinstance(label, QLabel):
                labels.append(label)
    width = max((label.sizeHint().width() for label in labels), default=0)
    for label in labels:
        label.setMinimumWidth(width)


def level_pixmap(level: BannerLevel, widget: QWidget, px: int = ICON_PX) -> QPixmap:
    """``QStyle`` ``SP_MessageBoxInformation`` / ``SP_MessageBoxWarning`` / ``SP_MessageBoxCritical`` at ``px``."""
    style = widget.style()
    if style is None:
        return QPixmap()
    return style.standardIcon(_LEVEL_ICON[level]).pixmap(px, px)


def _set_shown(widget: QWidget, shown: bool) -> None:
    """Hide; or show, unless the widget has no parent yet (it then shows with the parent it gets)."""
    if not shown:
        widget.hide()
    elif not widget.isWindow():
        widget.show()


class ErrorLabel(QWidget):
    """UJ-31: the one error style: the ``QStyle`` critical icon at 16 px and plain, wrapping, selectable
    text in the palette's ``WindowText``. Hidden while it has no text."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.icon = QLabel()
        self.icon.setPixmap(level_pixmap(BannerLevel.ERROR, self))
        self.label = QLabel()
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(self.label, 1)
        self.set_error(text)

    def set_error(self, text: str) -> None:
        """Show ``text``; ``""`` hides the label."""
        self.label.setText(text)
        _set_shown(self, bool(text))

    def clear(self) -> None:
        self.set_error("")

    def text(self) -> str:
        return self.label.text()


def error_label(text: str = "") -> ErrorLabel:
    return ErrorLabel(text)


def clear_on_edit(error: ErrorLabel, root: QWidget) -> None:
    """UJ-31: ``error`` clears on the next edit of any input under ``root``: ``QLineEdit.textEdited``,
    ``QSpinBox.valueChanged``, ``QComboBox.activated``, checkable ``QAbstractButton.toggled``,
    ``QTableWidget.itemChanged``, ``QKeySequenceEdit.keySequenceChanged``. Inputs created later are not
    covered: call it again for them."""

    def clear(*_args: object) -> None:
        error.clear()

    for edit in root.findChildren(QLineEdit):
        edit.textEdited.connect(clear)
    for spin in root.findChildren(QSpinBox):
        spin.valueChanged.connect(clear)
    for combo in root.findChildren(QComboBox):
        combo.activated.connect(clear)
    for button in root.findChildren(QAbstractButton):
        if button.isCheckable():
            button.toggled.connect(clear)
    for table in root.findChildren(QTableWidget):
        table.itemChanged.connect(clear)
    for keys in root.findChildren(QKeySequenceEdit):
        keys.keySequenceChanged.connect(clear)


def message_label() -> QLabel:
    """UJ-30: a plain-text, wrapping, selectable label, hidden while empty."""
    label = QLabel()
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.hide()
    return label


def show_message(label: QLabel, text: str) -> None:
    """Set ``label``'s text and show it only while the text is not empty."""
    label.setText(text)
    _set_shown(label, bool(text))
