from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

from models import TranslationItem


class TranslationEditModel(QAbstractTableModel):
    HEADERS = ("WOLF 代码", "类型", "原文", "译文")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items: list[TranslationItem] = []
        self.edits: dict[str, str] = {}

    def rowCount(self, _parent=QModelIndex()) -> int:
        return len(self.items)

    def columnCount(self, _parent=QModelIndex()) -> int:
        return len(self.HEADERS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.HEADERS[section]
        return super().headerData(section, orientation, role)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.items):
            return None
        item = self.items[index.row()]
        translation = self.edits.get(item.key, item.translation)
        values = (item.code, item.type, item.original, translation)
        if role == Qt.DisplayRole:
            return values[index.column()].replace("\r", "").replace("\n", " / ")
        if role == Qt.ToolTipRole:
            return values[index.column()]
        if role == Qt.UserRole:
            return item.key
        return None

    def set_items(self, items: list[TranslationItem]) -> None:
        self.beginResetModel()
        self.items = items
        self.edits.clear()
        self.endResetModel()

    def item(self, row: int) -> TranslationItem | None:
        return self.items[row] if 0 <= row < len(self.items) else None

    def translation(self, row: int) -> str:
        item = self.item(row)
        return self.edits.get(item.key, item.translation) if item else ""

    def set_translation(self, row: int, text: str) -> None:
        item = self.item(row)
        if not item:
            return
        if text == item.translation:
            self.edits.pop(item.key, None)
        else:
            self.edits[item.key] = text
        changed = self.index(row, 3)
        self.dataChanged.emit(changed, changed, [Qt.DisplayRole, Qt.ToolTipRole])

    def set_translations(self, replacements: dict[int, str]) -> None:
        if not replacements:
            return
        for row, text in replacements.items():
            item = self.item(row)
            if not item:
                continue
            if text == item.translation:
                self.edits.pop(item.key, None)
            else:
                self.edits[item.key] = text
        first = min(replacements)
        last = max(replacements)
        self.dataChanged.emit(
            self.index(first, 3),
            self.index(last, 3),
            [Qt.DisplayRole, Qt.ToolTipRole],
        )

    def discard_edits(self) -> None:
        if not self.edits:
            return
        self.beginResetModel()
        self.edits.clear()
        self.endResetModel()
