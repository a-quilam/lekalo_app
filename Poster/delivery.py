# delivery.py
"""
Poster 2.5: единая Telegram-доставка цельного готового анонса.

Один логический пост (spec §19–22):

- без фото — одно `sendMessage` с текстом и (если передана) клавиатурой;
- 1 фото — одно `sendPhoto`: текст подписью и клавиатура сразу в нём;
- 2–10 фото — ОДИН `sendMediaGroup`: текст ровно один раз, caption ПЕРВОГО
  элемента, остальные элементы без подписи; >10 фото — группы по 10 и
  возможный хвост из одного фото отдельным `sendPhoto` (подпись уже ушла с
  первой группой, хвост — без текста);
- `sendMediaGroup` не принимает общую inline-клавиатуру, поэтому для 2+ фото
  клавиатура в контент НЕ передаётся: вызывающий код отправляет одну
  отдельную панель управления после доставки контента и хранит её id в
  `announcement_instances.panel_message_id`. Тело доставки о панели не знает
  и её id не возвращает (`needs_control_panel`/`content_carries_keyboard` —
  точка правды для вызывающего);
- текст не режется на части: лимиты (4096 без фото, 1024 с фото после
  разбора entities) проверяются ДО вызова доставки, а не «лечатся»
  автосокращением.

Модуль намеренно не импортирует легаси-мастер (`handlers/post_creation.py`)
и ничего из диалога: только Telegram + отпечаток payload. Это убирает
циклический импорт и оставляет один сборщик прогресса для живого и
готового поста.

Каждая подтверждённая операция сразу попадает в `progress`/`on_progress`
через `ids`; при сбое в прогресс ничего не добавляется — падение не
считается успехом. Продолжение (`progress["step"]`) досылает только
недостающее.

Контракт для вызывающего (C2b):

- `send_announcement_post(bot, chat_id, text, photos, *, reply_markup=None,
  progress=None, on_progress=None, live=False) -> list[int]` возвращает id
  ТОЛЬКО контента (по id на каждое вложение альбома). Панель управления в
  этот список не входит; `publication_progress["ids"]` описывает только
  контент, а `mark_announcement_publication_sent` получает ровно их;
- `progress["total"] == len(media_batches(len(photos))) or 1` — число
  API-вызовов контента, а не сообщений: 11 фото → два вызова (группа 10 +
  хвост), 3 фото → один;
- `caption_message_id(ids)` — id сообщения, несущего текст: единственное
  сообщение или ПЕРВЫЙ элемент альбома. По нему адресуются правки подписи
  (`editMessageCaption`) в живом блоке и редакторе;
- после успешной отправки контента вызывающий код при
  `needs_control_panel(len(photos))` (2+ фото) отправляет ОДНУ панель
  управления и записывает её id в `announcement_instances.panel_message_id`.
  При ретрае частичной публикации панель досылается, только если её ещё
  нет: завершённые партии контента повторно не отправляются.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from telegram import InputMediaPhoto, LinkPreviewOptions

logger = logging.getLogger(__name__)

# U1b: превью ссылок выключено и в живом блоке, и в готовом анонсе.
# Экземпляры разные (но равные по значению): для Telegram они означают одно
# и то же, а журнал FakeBot/тесты по идентичности объекта отличают живой
# предпросмотр от итоговой доставки, не заглядывая в БД.
LIVE_LINK_PREVIEW = LinkPreviewOptions(is_disabled=True)
FINAL_LINK_PREVIEW = LinkPreviewOptions(is_disabled=True)

# Лимиты Telegram для одного анонса (после разбора entities, т.е. по
# видимому тексту). Без фото — обычное сообщение, с фото — подпись.
ANNOUNCEMENT_TEXT_LIMIT = 4096
ANNOUNCEMENT_CAPTION_LIMIT = 1024

# Решение владельца от 2026-10-27: не более 10 фото на анонс (разделяет
# `announcement_store.MAX_ANNOUNCEMENT_PHOTOS`).
ANNOUNCEMENT_PHOTO_LIMIT = 10

# Telegram принимает не более 10 снимков в одном sendMediaGroup; группа из
# одного элемента недопустима — такой хвост уходит через sendPhoto.
MEDIA_GROUP_LIMIT = 10

# Невидимый маркер пустой подписи (§5.6: пустой текст + фото → только фото,
# без точки). Telegram не принимает пустой текст сообщения, а форма
# публикации требует непустой `publication_text`, поэтому в замороженном
# payload хранится этот маркер: подписью он НЕ становится, а отпечаток
# нормализуется к технической `.`, как и у пустого текста.
EMPTY_CAPTION_TEXT = "\u200b"

# Символы, которые не делают текст видимым: маркеры пустой подписи и BOM.
_INVISIBLE_CHARS = (EMPTY_CAPTION_TEXT, "\u2060", "\ufeff")


def visible_text(text: str | None) -> str:
    """Текст без невидимых маркеров: пустая подпись остаётся пустой."""
    value = text or ""
    for char in _INVISIBLE_CHARS:
        value = value.replace(char, "")
    return value.strip()


def delivery_fingerprint(text: str, photos: Sequence[str]) -> str:
    """
    Отпечаток отправленного: текст + упорядоченный список фото.

    Одинаковое содержимое даёт одинаковый отпечаток (можно продолжить
    частичную отправку и не слать дубль), а любая правка делает прежние
    message_id несопоставимыми с новым содержимым.

    Пустой видимый текст нормализуется к пустой строке: у анонса с фото
    подпись пустая, а в замороженном payload стоит маркер пустой подписи
    (`EMPTY_CAPTION_TEXT`), и без нормализации живой блок и финальная версия
    считались бы разными (это дало бы второй «готовый» пост). Литеральная
    `.` — обычный видимый текст, поэтому у неё свой отпечаток: правка
    `.` → пусто обязана доставить новую версию.
    """
    basis = visible_text(text)
    digest = hashlib.sha256()
    digest.update(basis.encode("utf-8"))
    for file_id in photos:
        digest.update(b"\x00")
        digest.update(str(file_id).encode("utf-8"))
    return digest.hexdigest()


def media_batches(photo_count: int) -> list[int]:
    """
    Разбиение фото на вызовы Telegram: 2–10 в группе, хвост 1 — отдельно.

    Возвращает размеры партий по порядку. Хвост из одного фото НЕЛЬЗЯ
    отправить `sendMediaGroup`, поэтому партия размера 1 уходит отдельным
    `sendPhoto` (см. `send_announcement_post`).

    Число партий — это и есть число операций прогресса (`progress["total"]`),
    поэтому caption-first не меняет контракт: 11 фото по-прежнему дают две
    операции (группа 10 + хвост), просто текст теперь у ПЕРВОГО элемента
    первой группы, а не у последнего сегмента.
    """
    if photo_count <= 0:
        return []
    sizes: list[int] = []
    remaining = photo_count
    while remaining > 0:
        size = min(MEDIA_GROUP_LIMIT, remaining)
        sizes.append(size)
        remaining -= size
    return sizes


def content_carries_keyboard(photo_count: int) -> bool:
    """
    Может ли контент анонса сам нести inline-клавиатуру (spec §20–22).

    Без фото (`sendMessage`) и с одним (`sendPhoto`) — да: текст и кнопки в
    одном сообщении. С 2+ фото — нет: `sendMediaGroup` общей клавиатуры не
    принимает, а отдельное сообщение с полным текстом запрещено. Панель
    управления в этом случае отправляет вызывающий код.
    """
    return photo_count <= 1


def needs_control_panel(photo_count: int) -> bool:
    """
    Нужна ли отдельная панель управления после доставки контента (§22).

    Только для 2+ фото: у медиагруппы нет общей клавиатуры. Для 0/1 фото
    кнопки живут на самом сообщении анонса, отдельная панель запрещена.
    """
    return photo_count > 1


def caption_message_id(message_ids: Sequence[int]) -> int | None:
    """
    `message_id` сообщения, которое несёт текст анонса (spec §19–22).

    Без фото/с одним — единственное сообщение; в альбоме — ПЕРВЫЙ элемент,
    потому что `delivery` кладёт текст в caption первого фото. Правки
    подписи (живой блок, редактор) обязаны адресоваться этому id, а не
    последнему сообщению группы (до 2.5 было наоборот).
    """
    ids = [int(item) for item in message_ids or () if item]
    return ids[0] if ids else None


def _progress_ids(progress: dict[str, Any]) -> list[int]:
    ids = progress.get("ids")
    if not isinstance(ids, (list, tuple)):
        return []
    return [int(item) for item in ids if isinstance(item, int)]


def is_retry_after(exc: BaseException) -> bool:
    """Flood control: Telegram просит подождать `retry_after` секунд."""
    return exc.__class__.__name__ == "RetryAfter"


def is_forbidden(exc: BaseException) -> bool:
    """Бот заблокирован/удалён из чата: продолжать бессмысленно."""
    return exc.__class__.__name__ == "Forbidden"


def is_not_modified(exc: BaseException) -> bool:
    """Правка не изменила сообщение — это успех, а не сбой."""
    return "message is not modified" in str(exc).lower()


def is_not_found(exc: BaseException) -> bool:
    """Сообщение для правки/удаления исчезло: нужен новый блок."""
    text = str(exc).lower()
    return "not found" in text or "message to edit" in text


async def call_telegram(
    perform: Callable[[], Awaitable[Any]],
    *,
    retry_limit: int = 1,
    max_sleep: float = 30.0,
) -> Any:
    """
    Один вызов Telegram с единственным повтором на `RetryAfter` (§5.4).

    `RetryAfter` — не сбой доставки, а просьба подождать: анонс повторяется
    ровно один раз после указанной паузы (с разумным потолком ожидания).
    `Forbidden` и остальные ошибки пробрасываются сразу: падение не должно
    считаться успехом, а блокировку бота бессмысленно повторять.
    """
    attempt = 0
    while True:
        try:
            return await perform()
        except Exception as exc:  # noqa: BLE001 — решение по типу ниже
            if not is_retry_after(exc) or attempt >= retry_limit:
                raise
            attempt += 1
            retry_after = float(getattr(exc, "retry_after", 0) or 0)
            delay = max(0.0, min(retry_after, max_sleep))
            logger.warning(
                "Telegram просит подождать %.1f с (RetryAfter) — повтор %s.",
                delay,
                attempt,
            )
            if delay:
                await asyncio.sleep(delay)


async def send_announcement_post(
    bot,
    chat_id: int,
    text: str,
    photos: Sequence[str],
    *,
    reply_markup=None,
    progress: dict | None = None,
    on_progress: Callable[[dict], None] | None = None,
    live: bool = False,
) -> list[int]:
    """
    Отправляет анонс одним логическим постом и возвращает message_id контента.

    Возвращаются ТОЛЬКО id сообщений контента (0/1 фото — одно сообщение,
    альбом — по id на каждое вложение), в порядке отправки. Панель управления
    медиагруппы сюда НЕ входит: её отправляет вызывающий код после доставки
    контента и пишет id в `announcement_instances.panel_message_id`. Поэтому
    `mark_announcement_publication_sent` получает ровно эти id, а
    `publication_progress["ids"]` описывает только контент.

    `reply_markup` передаётся ТОЛЬКО когда Telegram способен нести клавиатуру
    вместе с текстом: без фото (`sendMessage`) и с одним фото (`sendPhoto`).
    Для 2+ фото клавиатура игнорируется (`content_carries_keyboard` → False):
    `sendMediaGroup` общей клавиатуры не имеет, а отдельное сообщение с
    полным текстом запрещено §22. Клавиатуру медиагруппы отправляет
    вызывающий код как панель управления (см. `needs_control_panel`).

    `progress` — контейнер продолжения: `fingerprint`, `step`, `ids`,
    `total`. Изменение текста/фото сбрасывает прогресс по отпечатку, иначе
    продолжение досылает только недостающие операции (`total` = число
    партий `media_batches`, т.е. число API-вызовов контента). `on_progress`
    вызывается после каждой подтверждённой Telegram операции, поэтому
    запись в БД не опережает реальную отправку.

    `live` помечает верхний блок конструктора: payload тот же, отличается
    только маркер «это ещё не готовый пост» (`LIVE_LINK_PREVIEW`), по которому
    служебный экран (свои отключённые превью) и итоговая доставка
    (`FINAL_LINK_PREVIEW`) отличаются от живого предпросмотра. Превью ссылок
    выключено в обоих случаях (U1b).
    """
    file_ids = [str(item) for item in photos if item]
    keyboard = reply_markup if content_carries_keyboard(len(file_ids)) else None
    batches = media_batches(len(file_ids))
    operations = len(batches) if batches else 1
    tracking = progress if progress is not None else ({} if on_progress else None)

    done = 0
    sent: list[int] = []
    if tracking is not None:
        fingerprint = delivery_fingerprint(text, file_ids)
        if tracking.get("fingerprint") == fingerprint:
            sent = _progress_ids(tracking)
            done = int(tracking.get("step") or 0)
            # Одна операция может вернуть НЕСКОЛЬКО message_id: настоящий
            # `sendMediaGroup` отвечает списком по сообщению на каждое фото.
            # Поэтому `ids` сверяются с числом операций только снизу (`len >=
            # done`): иначе альбом из N фото считался бы битым прогрессом и
            # отправлялся повторно.
            if done < 0 or done > operations or len(sent) < done:
                logger.warning(
                    "Прогресс отправки анонса не соответствует числу операций "
                    "— отправка начинается заново."
                )
                sent = []
                done = 0
        elif tracking.get("fingerprint"):
            logger.warning("Содержимое анонса изменилось — отправка заново.")
        tracking["fingerprint"] = fingerprint
        tracking["step"] = done
        tracking["ids"] = list(sent)
        tracking["total"] = operations

    def _notify() -> None:
        if tracking is None:
            return
        tracking["ids"] = list(sent)
        if on_progress is not None:
            on_progress(dict(tracking))

    def _remember(messages) -> None:
        if messages is None:
            return
        if isinstance(messages, (list, tuple)):
            for message in messages:
                message_id = getattr(message, "message_id", None)
                if message_id is not None:
                    sent.append(int(message_id))
        else:
            message_id = getattr(messages, "message_id", None)
            if message_id is not None:
                sent.append(int(message_id))

    operation = 0

    async def _run(perform: Callable[[], Awaitable[Any]]) -> None:
        nonlocal operation
        current = operation
        operation += 1
        if current < done:
            return
        # RetryAfter — пауза и один повтор (не сбой доставки); Forbidden и
        # прочее пробрасываются: подтверждённый id не записывается.
        result = await call_telegram(perform)
        _remember(result)
        if tracking is not None:
            # Шаг фиксируется ТОЛЬКО после подтверждённого ответа Telegram:
            # упавший вызов не увеличивает прогресс.
            tracking["step"] = current + 1
            tracking["ids"] = list(sent)
            if on_progress is not None:
                on_progress(dict(tracking))

    _notify()

    if not file_ids:
        # Без фото текст и клавиатура — в одном sendMessage (spec §20).
        await _run(
            lambda: bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                link_preview_options=(
                    LIVE_LINK_PREVIEW if live else FINAL_LINK_PREVIEW
                ),
                reply_markup=keyboard,
            )
        )
        return sent

    offset = 0
    batch_index = 0
    for size in batches:
        batch = file_ids[offset : offset + size]
        is_first_batch = batch_index == 0
        offset += size
        batch_index += 1
        if size == 1:
            # Хвост альбома (>10 фото) идёт без подписи: текст уже доставлен
            # первым элементом первой группы. Одиночное фото (первая и
            # единственная партия) несёт и подпись, и клавиатуру (spec §21).
            async def photo_runner(
                batch=batch,
                is_first_batch=is_first_batch,
            ):
                has_caption = is_first_batch and bool(text)
                return await bot.send_photo(
                    chat_id=chat_id,
                    photo=batch[0],
                    caption=text if has_caption else None,
                    parse_mode="HTML" if has_caption else None,
                    reply_markup=keyboard if is_first_batch else None,
                )

            await _run(photo_runner)
            continue

        def group_runner(batch=batch, is_first_batch=is_first_batch):
            # Caption-first (spec §19, §22): весь текст — подписью ПЕРВОГО
            # элемента, остальные фото его не дублируют. У sendMediaGroup нет
            # общей inline-клавиатуры, поэтому reply_markup здесь не
            # передаётся вообще (панель управления отправляет вызывающий код).
            media: list[InputMediaPhoto] = []
            for index, file_id in enumerate(batch):
                if is_first_batch and index == 0 and text:
                    media.append(
                        InputMediaPhoto(media=file_id, caption=text, parse_mode="HTML")
                    )
                else:
                    media.append(InputMediaPhoto(media=file_id))
            return bot.send_media_group(chat_id=chat_id, media=media)

        await _run(group_runner)

    return sent
