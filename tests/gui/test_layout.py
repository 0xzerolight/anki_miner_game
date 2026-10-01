"""The shared layout and message helpers (UJ-13, UJ-19, UJ-30, UJ-31, UJ-11)."""

import pytest
from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QKeySequence
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from anki_miner_game.gui.widgets.layout import (
    DIALOG_SCREEN_FRACTION,
    SCREEN_MARGIN,
    ErrorLabel,
    available_size,
    clear_on_edit,
    error_label,
    fit_dialog,
    level_pixmap,
    message_label,
    screen_bounded,
    show_message,
)
from anki_miner_game.models.messages import BannerLevel

# Sizes bounded by the screen (UJ-13, UJ-19) -------------------------------------------------------


def test_a_wanted_size_that_fits_is_kept_and_a_larger_one_is_bounded_by_the_screen(qtbot):
    widget = QWidget()
    qtbot.addWidget(widget)
    room = available_size(widget)
    assert room.width() > 200 and room.height() > 200
    assert screen_bounded(widget, QSize(100, 100)) == QSize(100, 100)
    huge = QSize(room.width() * 2, room.height() * 2)
    assert screen_bounded(widget, huge) == QSize(
        room.width() - SCREEN_MARGIN.width(), room.height() - SCREEN_MARGIN.height()
    )
    assert screen_bounded(widget, huge, margin=QSize(0, 0)) == room


# fit_dialog (UJ-30) -------------------------------------------------------------------------------


def two_form_dialog(qtbot, rows: int = 2) -> tuple[QDialog, QScrollArea, QFormLayout, QFormLayout]:
    dialog = QDialog()
    qtbot.addWidget(dialog)
    content = QWidget()
    column = QVBoxLayout(content)
    first, second = QFormLayout(), QFormLayout()
    first.addRow("Port", QSpinBox())
    first.addRow("Gap before the next line", QLineEdit())
    for number in range(rows):
        second.addRow(f"Row {number}", QLineEdit())
    column.addLayout(first)
    column.addLayout(second)
    scroll = QScrollArea()
    scroll.setWidget(content)
    outer = QVBoxLayout(dialog)
    outer.addWidget(scroll)
    outer.addWidget(QLabel("buttons"))
    return dialog, scroll, first, second


def labels(form: QFormLayout) -> list[QLabel]:
    found = []
    for row in range(form.rowCount()):
        item = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
        assert item is not None
        label = item.widget()
        assert isinstance(label, QLabel)
        found.append(label)
    return found


def test_forms_share_one_label_column_and_grow_their_text_fields(qtbot):
    dialog, scroll, first, second = two_form_dialog(qtbot)
    fit_dialog(dialog, scroll=scroll, forms=(first, second))
    widest = max(label.sizeHint().width() for label in labels(first) + labels(second))
    assert {label.minimumWidth() for label in labels(first) + labels(second)} == {widest}
    for form in (first, second):
        assert form.fieldGrowthPolicy() is QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        assert form.labelAlignment() == Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter


def test_the_scroll_area_never_scrolls_sideways_and_has_no_frame(qtbot):
    dialog, scroll, first, second = two_form_dialog(qtbot)
    fit_dialog(dialog, scroll=scroll, forms=(first, second))
    assert scroll.horizontalScrollBarPolicy() is Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert scroll.frameShape() is QFrame.Shape.NoFrame
    assert scroll.widgetResizable()


def test_the_dialog_is_as_wide_as_its_content_and_spin_boxes_keep_their_width(qtbot):
    dialog, scroll, first, second = two_form_dialog(qtbot)
    fit_dialog(dialog, scroll=scroll, forms=(first, second))
    dialog.show()
    qtbot.waitExposed(dialog)
    content = scroll.widget()
    assert content is not None
    assert dialog.width() >= content.sizeHint().width()
    assert dialog.minimumWidth() >= content.minimumSizeHint().width()
    spin = first.itemAt(0, QFormLayout.ItemRole.FieldRole).widget()
    assert spin.width() == spin.sizeHint().width()


def test_a_group_whose_labels_the_shared_column_widens_opens_wide_enough(qtbot):
    """A form in its own group box measures again once its labels widen (P5's request, G9): a group with
    a short label and a wide field would otherwise open squeezed by the difference."""
    dialog = QDialog()
    qtbot.addWidget(dialog)
    content = QWidget()
    column = QVBoxLayout(content)
    narrow_box, wide_box = QGroupBox("Narrow"), QGroupBox("Wide")
    narrow = QFormLayout(narrow_box)
    narrow.addRow("Gap before the next line", QSpinBox())
    wide = QFormLayout(wide_box)
    field = QLineEdit()
    field.setMinimumWidth(300)
    wide.addRow("Port", field)
    column.addWidget(narrow_box)
    column.addWidget(wide_box)
    scroll = QScrollArea()
    scroll.setWidget(content)
    QVBoxLayout(dialog).addWidget(scroll)
    fit_dialog(dialog, scroll=scroll, forms=(narrow, wide))
    dialog.show()
    qtbot.waitExposed(dialog)
    assert scroll.viewport().width() >= content.minimumSizeHint().width()
    assert dialog.minimumWidth() >= content.minimumSizeHint().width()


def test_a_tall_dialog_is_bounded_by_the_screen(qtbot):
    dialog, scroll, first, second = two_form_dialog(qtbot, rows=200)
    fit_dialog(dialog, scroll=scroll, forms=(first, second))
    room = available_size(dialog)
    assert dialog.height() <= int(room.height() * DIALOG_SCREEN_FRACTION)
    assert dialog.width() <= int(room.width() * DIALOG_SCREEN_FRACTION)


def test_a_later_fit_only_grows_the_dialog(qtbot):
    dialog, scroll, first, second = two_form_dialog(qtbot)
    fit_dialog(dialog, scroll=scroll, forms=(first, second))
    bigger = dialog.size() + QSize(50, 50)
    dialog.resize(bigger)
    fit_dialog(dialog, scroll=scroll, forms=(first, second))
    assert dialog.width() >= bigger.width() and dialog.height() >= bigger.height()


def test_a_shown_dialog_that_grows_stays_on_the_screen(qtbot):
    """A resize keeps the top-left: Settings opened at its usual place grew past the taskbar when Advanced
    opened, and Save was off the screen (P8)."""
    dialog, scroll, first, second = two_form_dialog(qtbot)
    advanced = QWidget()
    more = QFormLayout(advanced)
    wide = QLineEdit()
    more.addRow("Wide", wide)
    for number in range(60):
        more.addRow(f"More {number}", QLineEdit())
    advanced.hide()
    content = scroll.widget()
    assert content is not None
    content.layout().addWidget(advanced)
    fit_dialog(dialog, scroll=scroll, forms=(first, second, more))
    dialog.show()
    qtbot.waitExposed(dialog)
    screen = dialog.screen()
    assert screen is not None
    room = screen.availableGeometry()
    frame, before = dialog.frameGeometry(), dialog.size()
    dialog.move(room.right() - frame.width() - 10, room.bottom() - frame.height() - 10)
    wide.setMinimumWidth(room.width() // 2)
    advanced.show()  # the disclosure opens
    fit_dialog(dialog, scroll=scroll, forms=(first, second, more))
    assert dialog.height() > before.height() and dialog.width() > before.width()
    assert room.contains(dialog.frameGeometry())


def test_a_dialog_without_a_scroll_area_is_sized_to_its_hint(qtbot):
    dialog = QDialog()
    qtbot.addWidget(dialog)
    form = QFormLayout(dialog)
    form.addRow("Title", QLineEdit())
    fit_dialog(dialog, forms=(form,))
    assert dialog.size() == dialog.sizeHint()


# Errors (UJ-31) -----------------------------------------------------------------------------------


def holder(qtbot) -> tuple[QWidget, QVBoxLayout]:
    widget = QWidget()
    qtbot.addWidget(widget)
    return widget, QVBoxLayout(widget)


def test_an_error_label_is_hidden_until_it_has_text_and_hides_again_when_cleared(qtbot):
    _, column = holder(qtbot)
    error = error_label()
    column.addWidget(error)
    assert error.isHidden() and error.text() == ""
    error.set_error("Cannot save: the title is empty.")
    assert not error.isHidden() and error.text() == "Cannot save: the title is empty."
    error.clear()
    assert error.isHidden() and error.text() == ""


def test_an_error_is_plain_wrapping_selectable_text_beside_the_critical_icon(qtbot):
    _, column = holder(qtbot)
    error = ErrorLabel("<b>not bold</b>")
    column.addWidget(error)
    assert error.label.textFormat() is Qt.TextFormat.PlainText
    assert error.label.wordWrap()
    assert error.label.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse
    assert error.styleSheet() == "" and error.label.styleSheet() == ""  # the palette's text colour
    pixmap = error.icon.pixmap()
    assert pixmap is not None and not pixmap.isNull()
    assert pixmap.deviceIndependentSize().toSize() == QSize(16, 16)


def test_an_error_label_made_with_text_shows_once_in_a_layout(qtbot):
    widget, column = holder(qtbot)
    error = ErrorLabel("Cannot save: the title is empty.")
    column.addWidget(error)
    widget.show()
    qtbot.waitExposed(widget)
    assert error.isVisible()


def inputs(qtbot) -> tuple[QWidget, dict[str, QWidget]]:
    root, column = holder(qtbot)
    made: dict[str, QWidget] = {
        "line": QLineEdit(),
        "spin": QSpinBox(),
        "combo": QComboBox(),
        "check": QCheckBox("On"),
        "table": QTableWidget(1, 1),
        "keys": QKeySequenceEdit(),
    }
    made["combo"].addItems(["one", "two"])
    made["table"].setItem(0, 0, QTableWidgetItem("a"))
    for widget in made.values():
        column.addWidget(widget)
    return root, made


EDITS = {
    "line": lambda w, qtbot: qtbot.keyClicks(w, "x"),
    "spin": lambda w, qtbot: w.setValue(5),
    "combo": lambda w, qtbot: w.activated.emit(1),
    "check": lambda w, qtbot: w.click(),
    "table": lambda w, qtbot: w.item(0, 0).setText("b"),
    "keys": lambda w, qtbot: w.setKeySequence(QKeySequence("Alt+F9")),
}


@pytest.mark.parametrize("kind", list(EDITS))
def test_an_error_clears_at_the_next_edit_of_any_input(qtbot, kind):
    root, made = inputs(qtbot)
    error = error_label()
    root.layout().addWidget(error)
    clear_on_edit(error, root)
    error.set_error("Cannot save: the title is empty.")
    EDITS[kind](made[kind], qtbot)
    assert error.isHidden() and error.text() == ""


# Message labels (UJ-30) and level icons (UJ-11) ---------------------------------------------------


def test_a_message_label_shows_only_while_it_has_text(qtbot):
    _, column = holder(qtbot)
    label = message_label()
    column.addWidget(label)
    assert label.isHidden()
    assert label.textFormat() is Qt.TextFormat.PlainText and label.wordWrap()
    show_message(label, "OBS could not list its windows.")
    assert not label.isHidden() and label.text() == "OBS could not list its windows."
    show_message(label, "")
    assert label.isHidden()


@pytest.mark.parametrize("level", list(BannerLevel))
def test_each_banner_level_has_its_own_16_px_style_icon(qtbot, level):
    widget = QWidget()
    qtbot.addWidget(widget)
    pixmap = level_pixmap(level, widget)
    assert not pixmap.isNull()
    assert pixmap.deviceIndependentSize().toSize() == QSize(16, 16)
    others = [level_pixmap(other, widget).toImage() for other in BannerLevel if other is not level]
    assert pixmap.toImage() not in others
