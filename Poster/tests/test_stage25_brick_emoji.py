# tests/test_stage25_brick_emoji.py
"""
Poster 2.5 §9: единый эмодзи кирпичиков 🧩 и индивидуальные эмодзи системных.

Проверяется только оформление показа: каталог `brick_types` не меняется,
коды и названия кирпичиков остаются прежними. Статическая проверка каталога
текстов ловит возврат 🧱 в актуальные пользовательские строки.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from brick_types import (
    SYSTEM_BRICK_EMOJIS,
    SYSTEM_BRICKS,
    brick_title_display,
    system_brick_emoji,
)

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "config" / "ui_texts.yaml"

# Ожидаемые индивидуальные эмодзи системных кирпичиков (Poster 2.5 §9).
EXPECTED_EMOJIS = {
    "название": "🏷",
    "дата": "📅",
    "время_начала": "🕒",
    "время_окончания": "🕒",
    "место": "📍",
    "ссылка_места": "🔗",
    "описание": "📝",
    "контакт": "☎️",
    "адрес": "🗺",
    "город": "🌆",
}


class SystemBrickEmojiTests(unittest.TestCase):
    """Отображение названия системного кирпичика — простой код→эмодзи."""

    def test_every_system_brick_has_its_own_emoji(self) -> None:
        self.assertEqual(SYSTEM_BRICK_EMOJIS, EXPECTED_EMOJIS)
        self.assertEqual(
            sorted(SYSTEM_BRICK_EMOJIS),
            sorted(item.code for item in SYSTEM_BRICKS),
            "эмодзи заданы ровно для действующего каталога системных кирпичиков",
        )

    def test_display_title_prefixes_only_system_bricks(self) -> None:
        for code, emoji in EXPECTED_EMOJIS.items():
            with self.subTest(code=code):
                self.assertEqual(
                    brick_title_display("Название", code), f"{emoji} Название"
                )
        # Пользовательский кирпичик остаётся без эмодзи, код не меняется.
        self.assertEqual(brick_title_display("Цена", "цена"), "Цена")
        self.assertEqual(system_brick_emoji("цена"), "")
        self.assertEqual(system_brick_emoji(None), "")

    def test_display_title_does_not_touch_empty_or_html_input(self) -> None:
        self.assertEqual(brick_title_display("", "дата"), "📅 ")
        self.assertEqual(brick_title_display("<b>Дата</b>", "дата"), "📅 <b>Дата</b>")

    def test_catalog_has_no_old_brick_emoji(self) -> None:
        """Актуальные пользовательские строки каталога — только 🧩."""
        text = CATALOG.read_text(encoding="utf-8")
        self.assertNotIn("\U0001f9f1", text, "🧱 остался в пользовательском тексте")
        self.assertIn("🧩 Кирпичики", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
