from __future__ import annotations

import re

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QSyntaxHighlighter, QTextCharFormat
from PySide6.QtWidgets import QApplication, QPlainTextEdit, QTextEdit, QWidget
from hydrohub.ui.theme import DARK, LIGHT, theme_manager


class LineNumberArea(QWidget):
    def __init__(self, editor: "CodeEditor") -> None:
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self.editor.line_number_area_width(), 0)

    def paintEvent(self, event) -> None:  # noqa: N802
        self.editor.paint_line_numbers(event)


class CodeHighlighter(QSyntaxHighlighter):
    def __init__(self, document) -> None:
        super().__init__(document)
        self.language = "text"

    def set_language(self, language: str) -> None:
        self.language = language
        self.rehighlight()

    def highlightBlock(self, text: str) -> None:  # noqa: N802
        comment = QTextCharFormat()
        light = QApplication.instance().property('themeMode') == 'light'
        comment.setForeground(QColor('#767679' if light else '#8f8f93'))
        string = QTextCharFormat()
        string.setForeground(QColor('#9a5b00' if light else '#f0b155'))
        number = QTextCharFormat()
        number.setForeground(QColor('#0f8f89' if light else '#34c7c2'))
        key = QTextCharFormat()
        key.setForeground(QColor('#0f8f89' if light else '#57d8d3'))
        keyword = QTextCharFormat()
        keyword.setForeground(QColor('#0c726d' if light else '#c6c6c8'))

        comment_start = text.find("#")
        if comment_start >= 0:
            self.setFormat(comment_start, len(text) - comment_start, comment)
        for match in re.finditer(r"(['\"])(?:\\.|(?!\1).)*\1", text):
            self.setFormat(match.start(), match.end() - match.start(), string)
        for match in re.finditer(r"(?<![\w.])-?\d+(?:\.\d+)?", text):
            self.setFormat(match.start(), match.end() - match.start(), number)

        if self.language in {"yaml", "json", "toml"}:
            match = re.match(r"^(\s*)([A-Za-z_][\w.-]*)(\s*[:=])", text)
            if match:
                self.setFormat(match.start(2), len(match.group(2)), key)
            for match in re.finditer(r"\b(true|false|null|None)\b", text):
                self.setFormat(match.start(), match.end() - match.start(), keyword)
        elif self.language in {"python", "julia"}:
            words = "def|class|if|else|elif|for|while|return|import|from|in|try|except|finally|with|function|end|using|struct"
            for match in re.finditer(rf"\b({words})\b", text):
                self.setFormat(match.start(), match.end() - match.start(), keyword)


class CodeEditor(QPlainTextEdit):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        font = QFont("JetBrains Mono")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(11)
        self.setFont(font)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self.line_number_area = LineNumberArea(self)
        self.highlighter = CodeHighlighter(self.document())
        theme_manager().changed.connect(self._theme_changed)
        self.blockCountChanged.connect(self._update_gutter_width)
        self.updateRequest.connect(self._update_gutter)
        self.cursorPositionChanged.connect(self._highlight_current_line)
        self._update_gutter_width()
        self._highlight_current_line()

    def _theme_changed(self, _mode):
        self.highlighter.rehighlight()
        self.line_number_area.update()
        self._highlight_current_line()

    def set_language(self, language: str) -> None:
        self.highlighter.set_language(language)

    def line_number_area_width(self) -> int:
        digits = len(str(max(1, self.blockCount())))
        return 16 + self.fontMetrics().horizontalAdvance("9") * digits

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        rect = self.contentsRect()
        self.line_number_area.setGeometry(
            QRect(rect.left(), rect.top(), self.line_number_area_width(), rect.height())
        )

    def paint_line_numbers(self, event) -> None:
        painter = QPainter(self.line_number_area)
        tokens = LIGHT if QApplication.instance().property('themeMode') == 'light' else DARK
        painter.fillRect(event.rect(), QColor(tokens['surface']))
        block = self.firstVisibleBlock()
        number = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.setPen(QColor("#64748b"))
                painter.drawText(
                    0,
                    top,
                    self.line_number_area.width() - 8,
                    self.fontMetrics().height(),
                    Qt.AlignmentFlag.AlignRight,
                    str(number + 1),
                )
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            number += 1

    def _update_gutter_width(self, _count: int = 0) -> None:
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def _update_gutter(self, rect: QRect, dy: int) -> None:
        if dy:
            self.line_number_area.scroll(0, dy)
        else:
            self.line_number_area.update(0, rect.y(), self.line_number_area.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_gutter_width()

    def _highlight_current_line(self) -> None:
        selection = QTextEdit.ExtraSelection()
        tokens = LIGHT if QApplication.instance().property('themeMode') == 'light' else DARK
        selection.format.setBackground(QColor(tokens['soft']))
        selection.format.setProperty(QTextCharFormat.Property.FullWidthSelection, True)
        selection.cursor = self.textCursor()
        selection.cursor.clearSelection()
        self.setExtraSelections([selection])
