# rich_pages.py
"""
Сборка Rich Message из Rich-каталога `config/rich_content.yaml`.

Одна точка отображения для обеих страниц («Помощь» и «О проекте»):
`help_rich.py` и `about_rich.py` — только тонкие обёртки с публичными
контрактами (`build_*_rich_message`, `*_section_titles`), а структура блоков
собирается здесь по данным `utils.rich_content`.

Разделы — настоящие `InputRichBlockDetails` без `is_open`: по официальной
схеме Bot API отсутствие поля означает «свёрнуто» (§15.2). Никакой второй
копии утверждённых текстов в Python нет: всё берётся из каталога.

Слайд-шоу. Файлы каталог описывает путями внутри проекта, а существование
проверяется В МОМЕНТ СБОРКИ (то есть перед отправкой), а не при импорте
модуля: если оригинального PNG нет, раздел уходит без слайд-шоу, а в лог
пишется предупреждение. Замену картинкам не генерируем (§15.3, §20).
"""

from __future__ import annotations

import logging
from pathlib import Path

from telegram import InputFile, InputMediaPhoto

from rich_message import (
    InputRichBlockBlockQuotation,
    InputRichBlockDetails,
    InputRichBlockDivider,
    InputRichBlockList,
    InputRichBlockListItem,
    InputRichBlockParagraph,
    InputRichBlockPhoto,
    InputRichBlockSectionHeading,
    InputRichBlockSlideshow,
    InputRichBlockTable,
    InputRichBlockTableCell,
    InputRichMessage,
    InputRichTextBold,
    InputRichTextUrl,
    RichBlock,
    RichText,
)
from utils.rich_content import (
    Block,
    BoldPart,
    DetailsBlock,
    DividerBlock,
    LinkPart,
    ListBlock,
    ParagraphBlock,
    QuoteBlock,
    RichPage,
    SlideshowBlock,
    TableBlock,
    TextPart,
    media_path,
)

logger = logging.getLogger(__name__)


def _part_to_rich(part: TextPart) -> str | InputRichTextUrl | InputRichTextBold:
    """Часть абзаца → типизированный `RichText` Bot API."""
    if isinstance(part, LinkPart):
        return InputRichTextUrl(text=part.text, url=part.url)
    if isinstance(part, BoldPart):
        return InputRichTextBold(text=part.text)
    return part


def _rich_text(parts: tuple[TextPart, ...]) -> RichText:
    """
    Содержимое абзаца: одна часть — она сама, несколько — массив `RichText`.

    Массив из одного элемента Bot API тоже принимает, но лишний уровень
    вложенности только запутывает контракт и тесты.
    """
    if len(parts) == 1:
        return _part_to_rich(parts[0])
    return tuple(_part_to_rich(part) for part in parts)


def _table(block: TableBlock) -> InputRichBlockTable:
    """Таблица: первая строка — заголовочная, остальные — данные."""
    header = tuple(
        InputRichBlockTableCell(
            align="left",
            valign="top",
            text=cell,
            is_header=True,
        )
        for cell in block.header
    )
    rows = tuple(
        tuple(
            InputRichBlockTableCell(align="left", valign="top", text=cell)
            for cell in row
        )
        for row in block.rows
    )
    return InputRichBlockTable(
        cells=(header, *rows),
        is_bordered=True,
        is_compact=block.compact,
    )


def _slideshow(block: SlideshowBlock) -> InputRichBlockSlideshow | None:
    """
    Настоящее слайд-шоу из файлов проекта.

    Отсутствующий файл — не ошибка каталога: каталог может появиться раньше
    оригиналов владельца. Тогда кадр пропускается с предупреждением, а если
    не осталось ни одного — пропускается весь блок.
    """
    photos: list[InputRichBlockPhoto] = []
    missing: list[str] = []
    for slide in block.slides:
        path: Path = media_path(slide)
        if not path.is_file():
            missing.append(f"{slide.id}: {slide.file}")
            continue
        photos.append(
            # `InputFile` (а не строка пути) — штатный способ загрузки:
            # `send_rich_message` поднимает файл на верхний уровень запроса.
            # ВАЖНО: `telegram.InputFile(str)` считает строку СОДЕРЖИМОЙ
            # файла и отправляет её UTF-8-байты, то есть текст пути. Поэтому
            # читаем байты PNG и передаём имя файла (вместе с MIME-типом).
            InputRichBlockPhoto(
                photo=InputMediaPhoto(
                    media=InputFile(path.read_bytes(), filename=path.name)
                )
            )
        )
    for item in missing:
        logger.warning(
            "Rich Message: нет файла слайд-шоу (%s) — раздел будет без изображений.",
            item,
        )
    if not photos:
        return None
    return InputRichBlockSlideshow(blocks=tuple(photos))


def _block(block: Block) -> RichBlock | None:
    """Один блок каталога → блок Bot API (`None` — блок пропущен)."""
    if isinstance(block, ParagraphBlock):
        return InputRichBlockParagraph(text=_rich_text(block.parts))
    if isinstance(block, ListBlock):
        return InputRichBlockList(
            items=tuple(
                InputRichBlockListItem(
                    blocks=(InputRichBlockParagraph(text=_rich_text(item)),)
                )
                for item in block.items
            )
        )
    if isinstance(block, TableBlock):
        return _table(block)
    if isinstance(block, DividerBlock):
        return InputRichBlockDivider()
    if isinstance(block, QuoteBlock):
        quote_blocks: list[RichBlock] = [InputRichBlockParagraph(text=block.text)]
        return InputRichBlockBlockQuotation(
            blocks=tuple(quote_blocks),
            credit=block.credit,
        )
    if isinstance(block, DetailsBlock):
        children = tuple(
            child for child in (_block(item) for item in block.blocks) if child
        )
        return InputRichBlockDetails(summary=block.summary, blocks=children)
    if isinstance(block, SlideshowBlock):
        return _slideshow(block)
    raise TypeError(f"Неизвестный блок Rich-каталога: {type(block).__name__}")


def page_blocks(page: RichPage) -> tuple[RichBlock, ...]:
    """
    Страница каталога → полный список блоков Rich Message.

    Структура: главный заголовок, необязательное введение, подсказка о
    свёрнутых разделах, разделитель и разделы `details` без `is_open`.
    """
    blocks: list[RichBlock] = [
        # 1 — самый крупный заголовок официальной схемы Bot API.
        InputRichBlockSectionHeading(text=page.title, size=1),
    ]
    if page.intro:
        blocks.append(InputRichBlockParagraph(text=page.intro))
    if page.hint:
        blocks.append(InputRichBlockParagraph(text=page.hint))
    blocks.append(InputRichBlockDivider())
    for section in page.sections:
        content = tuple(
            child for child in (_block(item) for item in section.blocks) if child
        )
        blocks.append(
            InputRichBlockDetails(
                summary=section.title,
                # is_open не передаётся: отсутствие поля = свёрнуто (§15.2).
                blocks=content,
            )
        )
    return tuple(blocks)


def page_message(page: RichPage) -> InputRichMessage:
    """Готовое сообщение страницы каталога."""
    return InputRichMessage(blocks=page_blocks(page))
