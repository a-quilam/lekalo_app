# utils/pagination.py
"""
Единый механизм пагинации длинных списков Poster (Poster 2.5, spec §14).

Правила пользовательского поведения:

- 0–5 объектов: весь список; ни стрелок, ни номера страницы, ни счётчиков;
- 6+ объектов: пять объектов на страницу; под списком одна строка
  `◀️ | 2/4 | ▶️`; на первой странице нет стрелки влево, на последней —
  вправо; декоративных пустых кнопок нет;
- индикатор `2/4` — неактивная кнопка. Bot API 10.3 знает `disabled`, но
  PTB 22.8 его не поддерживает (`InlineKeyboardButton.__slots__` без поля),
  поэтому индикатор получает безопасный no-op callback `noop:<page>:<total>`:
  обработчик отвечает коротким `answer()` и ничего не меняет и не отправляет.

Модуль намеренно разделён на две части:

- `PageSlice` / `page_items` / `page_count` / `clamp_page` — чистая логика
  без Telegram: её проверяют юнит-тесты;
- `nav_buttons` — чистое описание строки навигации (подпись, callback);
- `nav_keyboard` — тонкая обёртка, создающая `InlineKeyboardButton`.
  Telegram импортируется внутри функции, чтобы чистая логика оставалась
  независимой от библиотеки.

Callback-данные стрелок сохраняют существующий протокол: префикс и СМЕЩЕНИЕ
(`pl_pickp:5`, `tp_page:10`). Обработчики страниц не меняются — они просто
передают смещение в состояние, а рендер приводит его к допустимой странице.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

# Пять записей на страницу даже тогда, когда у записи две кнопки
# («Название | 🗑»): запись — единица разбиения, а не кнопка.
PAGE_SIZE = 5

# Подписи стрелок — живые ключи каталога текстов (spec §14): владелец
# правит их в config/ui_texts.yaml, и строка навигации меняется без правки
# кода. Фолбэк нужен, если каталог недоступен или ключ пуст.
PREV_LABEL_KEY = "buttons.pagination.prev"
NEXT_LABEL_KEY = "buttons.pagination.next"
PREV_FALLBACK = "◀️"
NEXT_FALLBACK = "▶️"

# Префикс безопасного no-op callback индикатора «2/4».
NOOP_PREFIX = "noop"

T = TypeVar("T")


def page_count(total: int, page_size: int = PAGE_SIZE) -> int:
    """Сколько страниц займут `total` объектов (0 для пустого списка)."""
    if page_size <= 0:
        raise ValueError("page_size должен быть положительным")
    total = max(int(total), 0)
    return (total + page_size - 1) // page_size


def clamp_page(page: int, total: int, page_size: int = PAGE_SIZE) -> int:
    """
    Допустимая страница (0-based) для текущего количества объектов.

    Используется при рендере после удаления: страница не уходит в пустоту,
    а последняя исчезнувшая страница заменяется новой последней.
    """
    pages = page_count(total, page_size)
    if pages <= 0:
        return 0
    return min(max(int(page), 0), pages - 1)


@dataclass(frozen=True)
class PageSlice(Generic[T]):
    """
    Результат разбиения списка: объекты страницы и её метаданные.

    Итерируется как кортеж `(items, total_pages, page)` — так удобно
    распаковывать в вызывающем коде, как и описано в контракте.
    """

    items: list[T]
    page: int
    total_pages: int
    total: int
    page_size: int

    @property
    def offset(self) -> int:
        """Смещение страницы — его понимают все существующие обработчики."""
        return self.page * self.page_size

    @property
    def has_pagination(self) -> bool:
        return self.total > self.page_size

    @property
    def has_prev(self) -> bool:
        return self.has_pagination and self.page > 0

    @property
    def has_next(self) -> bool:
        return self.has_pagination and self.page + 1 < self.total_pages

    def __iter__(self):
        return iter((self.items, self.total_pages, self.page))


def page_items(
    items: Sequence[T], page: int, page_size: int = PAGE_SIZE
) -> PageSlice[T]:
    """Объекты запрошенной страницы; `page` безопасно приводится к допустимой."""
    data = list(items)
    total = len(data)
    safe_page = clamp_page(page, total, page_size)
    start = safe_page * page_size
    return PageSlice(
        items=data[start : start + page_size],
        page=safe_page,
        total_pages=page_count(total, page_size),
        total=total,
        page_size=page_size,
    )


def _arrow_label(key: str, fallback: str) -> str:
    """
    Подпись стрелки из каталога текстов с фолбэком.

    Импорт `tr` локальный: чистая логика модуля (и её юнит-тесты) не должна
    зависеть от Telegram-стека каталога при импорте. Пустой ответ или само
    имя ключа означает, что строка недоступна — тогда показываем стрелку.
    """
    from utils.ui_texts import tr

    label = tr(key)
    if not label or label == key:
        return fallback
    return label


def nav_buttons(
    page: int,
    total_pages: int,
    callback_prefix: str,
    *,
    page_size: int = PAGE_SIZE,
    noop_prefix: str = NOOP_PREFIX,
    as_offset: bool = True,
) -> list[tuple[str, str]] | None:
    """
    Одна строка навигации `◀️ | 2/4 | ▶️` как пары (подпись, callback).

    `None` — пагинация не нужна (одна страница и меньше): ни стрелок, ни
    индикатора, ни счётчиков. На первой странице нет левой стрелки, на
    последней — правой; вместо отсутствующей стрелки пустая кнопка НЕ
    создаётся.

    `as_offset=True` — прежний протокол «смещение» (`pl_page:5`), иначе —
    «номер страницы» (`aw_home_page:1`, `tp_page:1`). Значение стороны
    выбирается по существующему обработчику, чтобы не менять контракт.
    """
    pages = int(total_pages)
    if pages <= 1:
        return None
    current = min(max(int(page), 0), pages - 1)

    def value(target: int) -> int:
        return target * page_size if as_offset else target

    row: list[tuple[str, str]] = []
    if current > 0:
        row.append(
            (
                _arrow_label(PREV_LABEL_KEY, PREV_FALLBACK),
                f"{callback_prefix}:{value(current - 1)}",
            )
        )
    # Индикатор — no-op: у PTB 22.8 нет поля `disabled`.
    row.append((f"{current + 1}/{pages}", f"{noop_prefix}:{current + 1}:{pages}"))
    if current + 1 < pages:
        row.append(
            (
                _arrow_label(NEXT_LABEL_KEY, NEXT_FALLBACK),
                f"{callback_prefix}:{value(current + 1)}",
            )
        )
    return row


def nav_keyboard(
    page: int,
    total_pages: int,
    callback_prefix: str,
    *,
    page_size: int = PAGE_SIZE,
    noop_prefix: str = NOOP_PREFIX,
    as_offset: bool = True,
) -> list[Any] | None:
    """Готовая строка `InlineKeyboardButton` (или `None`, если пагинации нет)."""
    row = nav_buttons(
        page,
        total_pages,
        callback_prefix,
        page_size=page_size,
        noop_prefix=noop_prefix,
        as_offset=as_offset,
    )
    if row is None:
        return None
    # Локальный импорт: чистая логика модуля не зависит от Telegram.
    from telegram import InlineKeyboardButton

    return [
        InlineKeyboardButton(label, callback_data=callback_data)
        for label, callback_data in row
    ]


def nav_keyboard_for_offset(
    offset: int,
    total: int,
    callback_prefix: str,
    *,
    page_size: int = PAGE_SIZE,
    noop_prefix: str = NOOP_PREFIX,
) -> list[Any] | None:
    """
    Строка навигации по СМЕЩЕНИЮ — для списков из БД (`limit`/`offset`).

    Callback остаётся прежним `prefix:<offset>`, поэтому существующие
    обработчики страниц менять не нужно: они записывают смещение, а рендер
    снова приводит его к допустимой странице.
    """
    safe_offset = max(int(offset), 0)
    return nav_keyboard(
        safe_offset // page_size,
        page_count(total, page_size),
        callback_prefix,
        page_size=page_size,
        noop_prefix=noop_prefix,
        as_offset=True,
    )


__all__ = [
    "NEXT_FALLBACK",
    "NEXT_LABEL_KEY",
    "NOOP_PREFIX",
    "PAGE_SIZE",
    "PREV_FALLBACK",
    "PREV_LABEL_KEY",
    "PageSlice",
    "clamp_page",
    "nav_buttons",
    "nav_keyboard",
    "nav_keyboard_for_offset",
    "page_count",
    "page_items",
]
