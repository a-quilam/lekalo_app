# handlers/wizard_photos.py
"""
Poster 2.0 S4: последний шаг конструктора — фотографии анонса.

Шаг живёт после последнего кирпичика и не имеет кнопки «Готово»: анонс
готовится автоматически, как только принята ЦЕЛАЯ Telegram-партия — одно
фото или завершённый альбом (2–10 снимков). «Без фотографий» завершает
сразу, без дополнительных вопросов.

Модуль держит три вещи:

- вид шага: текст и кнопки (переехали из `announcement_wizard._render_photo_step`);
- приём партии: идемпотентное сохранение `file_id` с CAS и жёстким лимитом
  10 фото на анонс (решение владельца 2026-10-27). 11-е фото отклоняется,
  уже принятые не теряются и не перезаписываются;
- границу альбома: Telegram присылает каждое фото отдельным Update с общим
  `media_group_id`, поэтому партия собирается в буфере `user_data` и
  закрывается короткой задержкой (`asyncio`) либо следующим действием
  пользователя. До закрытия партии готовность НЕ наступает.

Каждый снимок сразу сохраняется в SQLite (идемпотентно и с CAS), поэтому
рестарт посреди альбома ничего не теряет и не дублирует: незавершённая
партия просто не запускает финализацию.

Порядок в чате («медиаблок сверху, служебный экран снизу») обеспечивает
`handlers/wizard_live.py`; шаг только просит синхронизацию до отрисовки.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from telegram import InlineKeyboardMarkup

from announcement_store import (
    MAX_ANNOUNCEMENT_PHOTOS,
    AnnouncementError,
    add_announcement_photo,
    get_announcement_draft,
    remove_announcement_photo,
)
from database import SessionLocal
from place_photo_store import list_place_photos
from utils import keyboard_layouts, pagination
from utils.tg_html import escape_html
from utils.ui_texts import tr

logger = logging.getLogger(__name__)

# Пауза закрытия альбома: Telegram доставляет все снимки одной группы почти
# одновременно, а новый Update той же группы приходит в пределах десятых
# секунды. Задержка нужна, чтобы не начать сборку анонса на первом снимке
# трёхсоставного альбома.
ALBUM_DEBOUNCE_SECONDS = 0.2

# Буфер незавершённой партии в `user_data`: media_group_id → [file_id, ...].
PENDING_ALBUM_KEY = "wizard_photos_pending"
PENDING_TASK_KEY = "wizard_photos_task"
# Итог приёма по партии: сколько принято/отклонено. По нему партия решает,
# завершать анонс автоматически или показать объяснение.
PENDING_RESULT_KEY = "wizard_photos_results"


def h(value: object) -> str:
    """HTML-экранирование значения для текста экрана."""
    return escape_html(str(value if value is not None else ""))


@dataclass(frozen=True)
class PhotoBatchResult:
    """Итог приёма партии: актуальный черновик и что именно произошло."""

    details: Any
    accepted: tuple[str, ...] = ()
    duplicate: tuple[str, ...] = ()
    rejected: tuple[str, ...] = ()
    photos_before: int = 0

    @property
    def limit_reached(self) -> bool:
        return bool(self.rejected)

    @property
    def changed(self) -> bool:
        """Партия реально что-то добавила (дубли и отказы — не изменение)."""
        return bool(self.accepted)


def photo_limit(details: Any) -> int:
    """Сколько ещё снимков примет анонс (лимит 10 на весь анонс)."""
    return max(0, MAX_ANNOUNCEMENT_PHOTOS - len(getattr(details, "photos", ())))


def add_photo_batch(
    *,
    telegram_user_id: int,
    draft_id: int,
    revision: int | None,
    file_ids: list[str] | tuple[str, ...],
    session_factory=None,
) -> PhotoBatchResult:
    """
    Сохраняет последовательность `file_id` с CAS и лимитом анонса.

    Каждое подтверждённое добавление поднимает revision, поэтому ожидаемая
    ревизия обновляется между вызовами. Дубль `file_id` идемпотентен и не
    считается новым снимком. При исчерпании лимита остаток возвращается в
    `rejected`: данные уже принятых фото сохранены и не перезаписаны.
    Ошибка домена пробрасывается — вызывающий слой покажет её честно.
    """
    factory = session_factory or SessionLocal
    with factory() as session:
        details = get_announcement_draft(
            session,
            actor_telegram_user_id=telegram_user_id,
            draft_id=draft_id,
        )
    photos_before = len(details.photos)
    known = list(details.photos)
    accepted: list[str] = []
    duplicate: list[str] = []
    rejected: list[str] = []
    current_revision = revision
    for index, file_id in enumerate(file_ids):
        value = str(file_id)
        if value in known:
            # Уже сохранён (в БД или этой же партией): повтор Update'а.
            duplicate.append(value)
            continue
        if len(known) >= MAX_ANNOUNCEMENT_PHOTOS:
            # 11-е фото и далее: не принимаем, но и не теряем молча.
            rejected.extend(str(item) for item in file_ids[index:])
            break
        with factory() as session:
            updated = add_announcement_photo(
                session,
                actor_telegram_user_id=telegram_user_id,
                draft_id=draft_id,
                file_id=value,
                expected_revision=current_revision,
            )
        details = updated
        known = list(updated.photos)
        current_revision = updated.revision
        accepted.append(value)
    return PhotoBatchResult(
        details=details,
        accepted=tuple(accepted),
        duplicate=tuple(duplicate),
        rejected=tuple(rejected),
        photos_before=photos_before,
    )


def limit_notice(result: PhotoBatchResult) -> str:
    """Объяснение отказа без потери уже принятых фото (S4 §5.3)."""
    if not result.rejected:
        return ""
    return tr(
        "wizard.photos.limit",
        limit=MAX_ANNOUNCEMENT_PHOTOS,
        rejected=len(result.rejected),
    )


def batch_notice(result: PhotoBatchResult) -> str:
    """Короткое подтверждение приёма партии для нижнего экрана."""
    if result.rejected:
        return limit_notice(result)
    if result.accepted:
        count = len(result.accepted)
        key = (
            "wizard.photos.accepted_one"
            if count == 1
            else "wizard.photos.accepted_many"
        )
        return tr(key, count=count)
    if result.duplicate:
        return tr("wizard.photos.duplicate")
    return ""


def library_photo_ids(details: Any, *, session_factory=None) -> tuple[str, ...]:
    """
    `file_id` фототеки площадки этого черновика (Poster 2.6.2 §3.4).

    У анонса по личному шаблону площадки нет — библиотека пуста, раздел
    «Фото площадки» не показывается. Изоляция обеспечивается самим
    `place_id` черновика: чужая библиотека сюда не попадает.
    """
    place_id = getattr(details, "place_id", None)
    if not isinstance(place_id, int) or isinstance(place_id, bool):
        return ()
    factory = session_factory or SessionLocal
    with factory() as session:
        return tuple(view.file_id for view in list_place_photos(session, place_id))


def photo_step_text(
    details: Any,
    *,
    notice: str | None = None,
    announcement_is_empty: bool = False,
    library_count: int = 0,
    picked_count: int = 0,
) -> str:
    """
    Текст нижнего экрана шага фото: короткая инструкция и счётчик.

    Технических объяснений (медиагруппы, статус публикации, «партия») здесь
    нет: владелец просил одно понятное действие. Шутка — только в этом
    служебном экране и только пока текста анонса нет (S3 п.11).

    `library_count` — снимков в фототеке площадки; при непустой библиотеке
    добавляется строка про раздел «Фото площадки» (§3.4). `picked_count` —
    сколько из них уже выбрано в этом анонсе.
    """
    total = len(getattr(details, "photos", ()) or ())
    lines = [
        tr("wizard.photos.title"),
        "",
        tr("wizard.photos.hint"),
    ]
    if total:
        lines.append(
            tr("wizard.photos.counter", count=total, limit=MAX_ANNOUNCEMENT_PHOTOS)
        )
    if library_count:
        lines.append(
            tr(
                "wizard.photos.library_hint",
                count=library_count,
                picked=picked_count,
            )
        )
    if notice:
        lines.insert(1, notice)
    if announcement_is_empty:
        from utils import ui_humor

        humor = ui_humor.get_humor_line(announcement_is_empty=True)
        if humor:
            # Экран уходит с parse_mode=HTML: фраза из редактируемого
            # каталога — обычный текст, а не разметка. Без экранирования
            # `<`, `>` или `&` ломают разбор и Telegram не отправляет экран
            # (пользователь видит «Что-то пошло не так» без кнопок шага).
            lines.extend(["", h(humor)])
    return "\n".join(lines)


def photo_step_keyboard(
    details: Any, *, library_count: int = 0
) -> InlineKeyboardMarkup:
    """
    Кнопки шага фото: «Назад» и «Пропустить».

    «Пропустить» (callback `aw_photo_none`) сохраняет прежнюю семантику:
    анонс завершается сразу, без вложений. Добавление фото отдельной
    кнопкой не нужно — снимки принимаются обычным сообщением, о чём и
    говорит текст экрана. «Назад» возвращает к предыдущему шагу (последний
    кирпичик), не меняя уже сохранённые данные.

    Poster 2.6.2 §3.4: при НЕПУСТОЙ фототеке площадки выше добавляется
    отдельная строка «Фото площадки» — выбор из библиотеки без
    автодобавления. Пустая библиотека раздела не показывает.

    U3a №13: пара собирается общим шаблоном `keyboard_layouts.pair`, а не
    вручную — так же, как пара «Назад | Пропустить» на вопросе даты.
    """
    draft_id = details.draft_id
    revision = details.revision
    rows: list[list[Any]] = []
    if library_count:
        rows.append(
            [
                keyboard_layouts.button(
                    tr("buttons.wizard.foto_ploschadki", count=library_count),
                    f"aw_photolib:{draft_id}:{revision}:0",
                )
            ]
        )
    rows.extend(
        keyboard_layouts.pair(
            keyboard_layouts.button(
                tr("buttons.nav.back"), f"aw_photo_back:{draft_id}:{revision}"
            ),
            keyboard_layouts.button(
                tr("buttons.wizard.skip"), f"aw_photo_none:{draft_id}:{revision}"
            ),
        )
    )
    return InlineKeyboardMarkup(rows)


async def render_photo_step(
    update,
    context,
    details,
    *,
    notice: str | None = None,
    announcement_is_empty: bool = False,
    library: tuple[str, ...] | None = None,
) -> int:
    """
    Отрисовывает шаг фото: сначала живой верхний блок, затем экран.

    `library` — `file_id` фототеки площадки (§3.4). `None` — библиотека
    читается здесь; явный кортеж передаётся, когда обработчик уже её знает.
    Непустая библиотека добавляет раздел «Фото площадки»; собственные снимки
    анонса и библиотечные объединяются в общем лимите 10.
    """
    from handlers.announcement_wizard import (
        AW_PREVIEW,
        _render,
        _set_details,
        _sync_live,
        _ui,
    )

    ui = _ui(context)
    ui["photo_step"] = True
    _set_details(ui, details)
    await _sync_live(update, context, details)
    if library is None:
        library = library_photo_ids(details)
    photos = tuple(getattr(details, "photos", ()) or ())
    picked = sum(1 for file_id in library if file_id in photos)
    await _render(
        update,
        context,
        photo_step_text(
            details,
            notice=notice,
            announcement_is_empty=announcement_is_empty,
            library_count=len(library),
            picked_count=picked,
        ),
        photo_step_keyboard(details, library_count=len(library)),
    )
    return AW_PREVIEW


# --- фототека площадки в шаге фото (Poster 2.6.2 §3.4) ---------------------

# Сколько снимков библиотеки показывает одна страница выбора: пять — общая
# единица списков Poster (`utils.pagination.PAGE_SIZE`).
PHOTO_LIBRARY_PAGE_SIZE = 5


def library_page_state(details: Any, library: tuple[str, ...]) -> tuple[int, int]:
    """
    (выбрано в анонсе, всего в библиотеке) для текста раздела §3.4.

    Отдельная функция нужна экрану выбора и тестам: «выбрано» считается по
    СОВПАДЕНИЮ `file_id`, а не по индексу — порядок библиотеки стабилен, но
    снимок мог быть добавлен в анонс и обычным сообщением.
    """
    photos = tuple(getattr(details, "photos", ()) or ())
    picked = sum(1 for file_id in library if file_id in photos)
    return picked, len(library)


def library_step_text(
    details: Any,
    library: tuple[str, ...],
    page: int,
    *,
    notice: str | None = None,
) -> str:
    """
    Экран «Фото площадки»: библиотека площадки с отметками выбора.

    Показывает только библиотеку ЭТОЙ площадки (её `file_id` в черновике
    отсутствуют — они не смешиваются). Свои прикреплённые фото не
    добавляются автоматически: каждое нажатие — тумблер.
    """
    picked, total = library_page_state(details, library)
    lines = [
        tr("wizard.photo_library.title"),
        "",
        tr(
            "wizard.photo_library.hint",
            picked=picked,
            total=total,
            limit=MAX_ANNOUNCEMENT_PHOTOS,
        ),
    ]
    if notice:
        lines.insert(1, notice)
    return "\n".join(lines)


def library_step_keyboard(
    details: Any,
    library: tuple[str, ...],
    page: int,
) -> InlineKeyboardMarkup:
    """
    Клавиатура раздела: тумблер каждого снимка, пагинация и «Назад».

    Callback передаёт НОМЕР в библиотеке (`aw_phototoggle:<draft>:<rev>:<n>`),
    поэтому снимок остаётся адресуемым даже после удаления соседнего:
    индекс пересчитывается по актуальной библиотеке на каждом нажатии.

    «◀️ Назад» ведёт на ШАГ ФОТО (`aw_photoback`), а не в список вложений:
    раздел рисуется вместо шага, поэтому родитель у него именно шаг. Иначе
    после захода в раздел кнопка «Фото площадки» пропадала бы, и повторный
    выбор из библиотеки стал бы недоступен.
    """
    total = len(library)
    pages = max(1, (total + PHOTO_LIBRARY_PAGE_SIZE - 1) // PHOTO_LIBRARY_PAGE_SIZE)
    page = max(0, min(int(page or 0), pages - 1))
    photos = tuple(getattr(details, "photos", ()) or ())
    start = page * PHOTO_LIBRARY_PAGE_SIZE
    rows: list[list[Any]] = []
    for index in range(start, min(start + PHOTO_LIBRARY_PAGE_SIZE, total)):
        file_id = library[index]
        mark = "☑️" if file_id in photos else "☐"
        rows.append(
            [
                keyboard_layouts.button(
                    tr(
                        "buttons.wizard.foto_vybor",
                        number=index + 1,
                        mark=mark,
                    ),
                    f"aw_phototoggle:{details.draft_id}:{details.revision}:{index}",
                )
            ]
        )
    nav = pagination.nav_buttons(
        page,
        pages,
        f"aw_photolib:{details.draft_id}:{details.revision}",
        as_offset=False,
    )
    if nav:
        rows.append([keyboard_layouts.button(label, data) for label, data in nav])
    rows.append(
        [
            keyboard_layouts.button(
                tr("buttons.nav.back"),
                f"aw_photoback:{details.draft_id}:{details.revision}",
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def toggle_library_photo(
    *,
    telegram_user_id: int,
    draft_id: int,
    revision: int,
    index: int,
    library: tuple[str, ...],
    session_factory=None,
) -> tuple[Any, str]:
    """
    Тумблер «выбрать/снять»: возвращает (актуальный черновик, плашку).

    Выбор НЕ автоматический: снимок добавляется к анонсу только этим
    действием. Снятие убирает именно этот `file_id`, остальные не
    сдвигаются. Лимит 10 суммарно: 11-й снимок отклоняется понятной
    плашкой, уже выбранные не теряются.
    """
    factory = session_factory or SessionLocal
    with factory() as session:
        details = get_announcement_draft(
            session,
            actor_telegram_user_id=telegram_user_id,
            draft_id=draft_id,
        )
    if index < 0 or index >= len(library):
        raise AnnouncementError("Этот снимок больше недоступен в фототеке.")
    file_id = library[index]
    photos = tuple(details.photos)
    if file_id in photos:
        position = photos.index(file_id)
        with factory() as session:
            updated = remove_announcement_photo(
                session,
                actor_telegram_user_id=telegram_user_id,
                draft_id=draft_id,
                index=position,
                expected_revision=details.revision,
            )
        return updated, tr("wizard.photo_library.snyato", number=index + 1)
    if len(photos) >= MAX_ANNOUNCEMENT_PHOTOS:
        return details, tr(
            "wizard.photo_library.limit",
            limit=MAX_ANNOUNCEMENT_PHOTOS,
        )
    with factory() as session:
        updated = add_announcement_photo(
            session,
            actor_telegram_user_id=telegram_user_id,
            draft_id=draft_id,
            file_id=file_id,
            expected_revision=details.revision,
        )
    return updated, tr("wizard.photo_library.vybrano", number=index + 1)


async def render_photo_library(
    update,
    context,
    details,
    *,
    page: int = 0,
    notice: str | None = None,
    library: tuple[str, ...] | None = None,
) -> int:
    """
    Рисует раздел «Фото площадки» вместо шага фото.

    Живой верхний блок не синхронизируется: выбор из библиотеки меняет
    только коллекцию вложений, а она появится в блоке при обычной
    синхронизации шага. Экран остаётся тем же «экраном снизу», поэтому
    правило «медиаблок сверху, служебный экран снизу» не нарушается.
    """
    from handlers.announcement_wizard import AW_PREVIEW, _render, _set_details, _ui

    ui = _ui(context)
    # Раздел живёт внутри шага фото: после рестарта обычный текст/фото
    # продолжают шаг, а не проваливаются в подсказку меню.
    ui["photo_step"] = True
    _set_details(ui, details)
    if library is None:
        library = library_photo_ids(details)
    await _render(
        update,
        context,
        library_step_text(details, library, page, notice=notice),
        library_step_keyboard(details, library, page),
    )
    return AW_PREVIEW


# --- буфер альбома ---------------------------------------------------------


def _pending(user_data: dict[str, Any]) -> dict[str, list[str]]:
    buffer = user_data.get(PENDING_ALBUM_KEY)
    if not isinstance(buffer, dict):
        buffer = {}
        user_data[PENDING_ALBUM_KEY] = buffer
    return buffer


def buffer_photo(
    user_data: dict[str, Any], media_group_id: str | None, file_id: str
) -> str:
    """
    Кладёт `file_id` в партию и возвращает ключ партии.

    Одиночное фото (без `media_group_id`) — партия из одного снимка с
    уникальным ключом: она закрывается сразу, без ожидания. Одинаковый
    `file_id` дважды в партию не попадает: повтор Update'а не дублирует
    ни сохранение, ни финализацию.
    """
    key = media_group_id or f"single-{file_id}"
    batch = _pending(user_data).setdefault(key, [])
    if file_id not in batch:
        batch.append(file_id)
    return key


def store_result(user_data: dict[str, Any], key: str, result: PhotoBatchResult) -> None:
    """Запоминает итог приёма партии до решения о финализации."""
    results = user_data.get(PENDING_RESULT_KEY)
    if not isinstance(results, dict):
        results = {}
        user_data[PENDING_RESULT_KEY] = results
    results[key] = result


def absorb_result(
    user_data: dict[str, Any], key: str, result: PhotoBatchResult
) -> PhotoBatchResult:
    """
    Складывает итоги отдельных Update'ов одной партии в общий.

    Telegram присылает альбом несколькими Update'ами, поэтому «принято/
    отклонено» копится по партии: финализация наступает только после её
    закрытия, а лимит объясняется один раз.
    """
    previous = user_data.get(PENDING_RESULT_KEY)
    previous = previous.get(key) if isinstance(previous, dict) else None
    merged = PhotoBatchResult(
        details=result.details,
        accepted=(
            (previous.accepted if isinstance(previous, PhotoBatchResult) else ())
            + result.accepted
        ),
        duplicate=(
            (previous.duplicate if isinstance(previous, PhotoBatchResult) else ())
            + result.duplicate
        ),
        rejected=(
            (previous.rejected if isinstance(previous, PhotoBatchResult) else ())
            + result.rejected
        ),
        photos_before=(
            previous.photos_before
            if isinstance(previous, PhotoBatchResult)
            else result.photos_before
        ),
    )
    store_result(user_data, key, merged)
    return merged


def take_result(user_data: dict[str, Any], key: str) -> PhotoBatchResult | None:
    """Забирает итог партии (None — партия ещё не принималась)."""
    results = user_data.get(PENDING_RESULT_KEY)
    if not isinstance(results, dict):
        return None
    value = results.pop(key, None)
    if not results:
        user_data.pop(PENDING_RESULT_KEY, None)
    return value if isinstance(value, PhotoBatchResult) else None


def take_batch(user_data: dict[str, Any], key: str) -> tuple[str, ...]:
    """Забирает партию из буфера; повторный вызов возвращает пустой кортеж."""
    buffer = _pending(user_data)
    items = buffer.pop(key, [])
    if not buffer:
        user_data.pop(PENDING_ALBUM_KEY, None)
    return tuple(items)


def cancel_pending_task(user_data: dict[str, Any]) -> None:
    """Отменяет запланированное закрытие партии (диалог ушёл вперёд)."""
    task = user_data.pop(PENDING_TASK_KEY, None)
    if task is not None and not getattr(task, "done", lambda: True)():
        task.cancel()


def schedule_album_close(application, user_data: dict[str, Any], closer) -> None:
    """
    Закрывает партию после короткой паузы, если новых снимков не пришло.

    JobQueue у бота отключён (`job_queue(None)`), поэтому используется
    `Application.create_task`: задержка не блокирует обработку следующих
    фото того же альбома, а запасной путь — явное закрытие следующим
    действием пользователя через `cancel_pending_task` в мастере анонса.
    """
    if application is None:
        return
    cancel_pending_task(user_data)

    async def _wait() -> None:
        try:
            await asyncio.sleep(ALBUM_DEBOUNCE_SECONDS)
        except asyncio.CancelledError:  # pragma: no cover — отмена штатная
            return
        try:
            await closer()
        except Exception as exc:  # noqa: BLE001 — шаг не должен ломать бота
            logger.exception("Не удалось закрыть партию фото: %s", exc)

    # PTB не создаёт задачи, пока приложение не запущено (тесты гоняют
    # обработчики напрямую) — тогда планируем обычной задачей текущей петли.
    if getattr(application, "running", False):
        task = application.create_task(_wait())
    else:
        task = asyncio.ensure_future(_wait())
    user_data[PENDING_TASK_KEY] = task


def rejected_photo_notice(error: BaseException) -> str:
    """Единый текст отказа по лимиту/домену — без потери принятых фото."""
    return tr("wizard.photos.rejected", reason=str(error)[:300])


__all__ = [
    "ALBUM_DEBOUNCE_SECONDS",
    "AnnouncementError",
    "PHOTO_LIBRARY_PAGE_SIZE",
    "PhotoBatchResult",
    "add_photo_batch",
    "batch_notice",
    "buffer_photo",
    "cancel_pending_task",
    "h",
    "library_photo_ids",
    "library_page_state",
    "library_step_keyboard",
    "library_step_text",
    "limit_notice",
    "photo_limit",
    "photo_step_keyboard",
    "photo_step_text",
    "rejected_photo_notice",
    "render_photo_library",
    "render_photo_step",
    "schedule_album_close",
    "store_result",
    "take_batch",
    "take_result",
    "toggle_library_photo",
]
