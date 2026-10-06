"""Shared sizing rules for resizable desktop workspaces."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QFormLayout, QFrame, QScrollArea, QLayout


def scroll_panel(content):
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setWidget(content)
    scroll.setMinimumHeight(140)
    if content.layout():
        content.layout().setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
    return scroll


def configure_forms(root):
    for form in root.findChildren(QFormLayout):
        form.setVerticalSpacing(14)
        form.setHorizontalSpacing(16)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)


def compact_combo(combo: QComboBox, width: int = 280, visible_characters: int = 24):
    combo.setMaximumWidth(width)
    combo.setMinimumContentsLength(visible_characters)
    combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    combo.view().setMaximumWidth(width)
    combo.view().setTextElideMode(Qt.TextElideMode.ElideMiddle)
    return combo
