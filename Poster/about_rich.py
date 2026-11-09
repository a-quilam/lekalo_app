# about_rich.py
"""
Rich Message «О проекте» (этап D, финальное задание 2026-11-08 §14, §15).

Содержимое не живёт в Python: единственный источник — каталог
`config/rich_content.yaml` (раздел `about`), загрузка и строгая проверка —
`utils/rich_content.py`, сборка блоков — `rich_pages.py`. Здесь остаются
публичные контракты:

- `build_about_rich_message()` — готовый `InputRichMessage`;
- `about_section_titles()` — названия шести разделов в порядке экрана;
- адреса проекта (`GITHUB_URL`, `AUTHOR_URL`, `RANDOM_CULTURE_URL`) — их
  проверяют тесты и приёмка. Сами ссылки в сообщении берутся из YAML;
  константы обязаны совпадать с каталогом, это закреплено тестом, чтобы
  адрес не разъехался в двух местах.

Художественной обложки нет (§14); разделы — свёрнутые
`InputRichBlockDetails`; просьба о поддержке — жирный абзац про СБП и две
кликабельные ссылки (§14.6).
"""

from __future__ import annotations

from rich_message import InputRichMessage
from rich_pages import page_message
from utils.rich_content import load_catalog

__all__ = [
    "AUTHOR_URL",
    "GITHUB_URL",
    "RANDOM_CULTURE_URL",
    "about_section_titles",
    "build_about_rich_message",
]

# —— адреса проекта ——————————————————————————————————————————————————
#
# Единственный источник содержимого — `config/rich_content.yaml`; эти
# константы нужны тестам и приёмке как «якоря» адресов. Тест каталога
# сверяет их со ссылками YAML, поэтому расхождение не пройдёт незамеченным.
GITHUB_URL = "https://github.com/a-quilam/lekalo_app"
AUTHOR_URL = "https://t.me/a_quilam"
RANDOM_CULTURE_URL = "https://t.me/random_culture"


def build_about_rich_message() -> InputRichMessage:
    """
    Собирает «О проекте» из каталога (шесть свёрнутых разделов, §14).

    Ошибка каталога — `RichContentError`: вызывающий слой покажет короткую
    плашку сбоя, старый текст на замену НЕ подставляется (§15.1).
    """
    return page_message(load_catalog().about)


def about_section_titles() -> tuple[str, ...]:
    """Названия разделов в порядке экрана. Для проверок и тестов."""
    return tuple(section.title for section in load_catalog().about.sections)
