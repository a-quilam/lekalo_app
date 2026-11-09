# help_rich.py
"""
Rich Message «Помощь» (этап D, финальное задание 2026-11-08 §13, §15).

Содержимое не живёт в Python: единственный источник — каталог
`config/rich_content.yaml` (раздел `help`), который загружает и строго
проверяет `utils/rich_content.py`, а собирает `rich_pages.py`. Здесь остаются
только публичные контракты, на которые опираются `handlers/main_menu.py` и
тесты:

- `build_help_rich_message()` — готовый `InputRichMessage`;
- `help_section_titles()` — названия девяти разделов в порядке справки.

Почему так: §15.1 запрещает вторую копию утверждённых текстов. Правка
формулировок, заголовков, порядка разделов, ссылок, таблицы, цитат и
слайд-шоу делается только в YAML.

Разделы — настоящие свёрнутые `InputRichBlockDetails`; отдельного раздела об
условных частях в справке нет (§17): инструкция живёт в административном
редакторе шаблона.
"""

from __future__ import annotations

from rich_message import InputRichMessage
from rich_pages import page_message
from utils.rich_content import load_catalog

__all__ = ["build_help_rich_message", "help_section_titles"]


def build_help_rich_message() -> InputRichMessage:
    """
    Собирает справку из каталога (девять свёрнутых разделов, §13).

    Ошибка каталога — `RichContentError`: вызывающий слой покажет короткую
    плашку сбоя. Старый текст на замену НЕ подставляется (§15.1).
    """
    return page_message(load_catalog().help)


def help_section_titles() -> tuple[str, ...]:
    """Названия разделов в порядке справки. Для проверок и тестов."""
    return tuple(section.title for section in load_catalog().help.sections)
