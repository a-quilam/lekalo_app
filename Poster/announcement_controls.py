# announcement_controls.py
"""
Poster 2.5, §18–22/§27: управление готовым анонсом.

У готового анонса НЕТ промежуточного экрана «✅ Анонс готов». Управление
живёт там же, где сам анонс:

- без фото и с одним фото — inline-клавиатура прямо на сообщении анонса
  (`sendMessage`/`sendPhoto` умеют нести `reply_markup`);
- с 2+ фото — одна отдельная панель управления: `sendMediaGroup` общей
  клавиатуры не принимает, а второе сообщение с полным текстом запрещено
  (§22). Панель — служебный экран (§5.1), переиспользуется редакторами
  (§27) и не дублирует текст анонса.

Кнопки всегда привязаны к КОНКРЕТНОМУ экземпляру (`announcement_instances`),
а не к черновику: после повторного открытия прежний экземпляр архивный, его
кнопки обязаны отвечать «устарело», а не открывать чужой редактор (§30–32).
Поэтому протокол callback — `anp_{action}:{instance_id}`, и он одинаков для
клавиатуры на сообщении, и для отдельной панели.
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

from utils.ui_texts import tr

# Маркер записи панели в журнале FakeBot/тестах: как у экрана и живого блока,
# это отдельный экземпляр `LinkPreviewOptions`. Панель — служебное сообщение
# (§5.1), а не пост: `h.posts()` не должен считать её анонсом.
PANEL_LINK_PREVIEW = LinkPreviewOptions(is_disabled=True)

# Префикс всех кнопок управления готовым анонсом. Отделён от `aw_`
# (конструктор): по нему обработчик отличает управление экземпляром от
# шагов мастера, даже если сообщение осталось в чате после рестарта.
PANEL_CALLBACK_PREFIX = "anp_"

# Действия панели. Полный редактор (§18): поля, доп. поля, фотографии,
# выход в меню. Имена короткие — callback_data ограничен 64 байтами.
ACTION_DATA = "data"
ACTION_PHOTOS = "photos"
ACTION_EXTRAS = "extras"
ACTION_MENU = "menu"
PANEL_ACTIONS = (ACTION_DATA, ACTION_PHOTOS, ACTION_EXTRAS, ACTION_MENU)

# Фильтр обработчика: только известные действия и числовой id экземпляра.
# Строже `parse_panel_callback` он не заменяет: подделанный callback с тем же
# префиксом всё равно получит короткий ответ, а не доменную ошибку.
PANEL_CALLBACK_PATTERN = rf"^{PANEL_CALLBACK_PREFIX}({'|'.join(PANEL_ACTIONS)}):\d+$"

# Poster 2.5.3 §5: список ТЕКСТОВЫХ полей анонса. Он живёт не на отдельном
# экране, а на самом управлении: у 0–1 фото — клавиатурой сообщения анонса,
# у медиагруппы — текстом её единственной панели. Поэтому у протокола свой
# префикс: кнопки списка не являются действиями панели и разбираются
# отдельно (проверка экземпляра у них та же).
FIELD_CALLBACK_PREFIX = "anf_"
FIELD_ACTION_PAGE = "page"
FIELD_ACTION_SELECT = "sel"
FIELD_ACTION_BACK = "back"
FIELD_ACTIONS = (FIELD_ACTION_PAGE, FIELD_ACTION_SELECT, FIELD_ACTION_BACK)

# `anf_page:{instance_id}:{offset}`, `anf_sel:{instance_id}:{brick_id}`,
# `anf_back:{instance_id}` — id экземпляра обязателен, страница и кирпичик
# необязательны в зависимости от действия.
FIELD_CALLBACK_PATTERN = (
    rf"^{FIELD_CALLBACK_PREFIX}({'|'.join(FIELD_ACTIONS)}):\d+(?::\d+)?$"
)

# Ответ на кнопку устаревшего экземпляра (§32, сценарий Ж). Кнопка могла
# пережить архив, удаление записи или рестарт службы: пользователь получает
# только пояснение, без правки экранов и записей в БД.
STALE_INSTANCE_TEXT = tr("errors.stale.instance")


def panel_callback(action: str, instance_id: int) -> str:
    """
    `callback_data` кнопки управления (протокол §32, подготовка C3).

    В данных только действие и id экземпляра: черновик, чат и поколение
    восстанавливаются по экземпляру, поэтому устаревшая кнопка физически
    не может открыть чужой анонс.
    """
    if action not in PANEL_ACTIONS:
        raise ValueError(f"Неизвестное действие панели анонса: {action!r}")
    return f"{PANEL_CALLBACK_PREFIX}{action}:{int(instance_id)}"


def parse_panel_callback(data: str | None) -> tuple[str, int] | None:
    """
    Разбирает `anp_{action}:{instance_id}`; None — данные не нашего формата.

    Строгий разбор нужен обработчику: подделанный или обрезанный callback
    должен получить короткий ответ, а не доменную ошибку.
    """
    raw = (data or "").strip()
    if not raw.startswith(PANEL_CALLBACK_PREFIX):
        return None
    parts = raw.split(":")
    if len(parts) != 2:
        return None
    action = parts[0][len(PANEL_CALLBACK_PREFIX) :]
    if action not in PANEL_ACTIONS or not parts[1].isdigit():
        return None
    instance_id = int(parts[1])
    if instance_id <= 0:
        return None
    return action, instance_id


def field_callback(action: str, instance_id: int, value: int | None = None) -> str:
    """
    `callback_data` кнопки списка текстовых полей (2.5.3 §5).

    `value` — смещение страницы (`page`) или id кирпичика (`sel`); у
    «◀️ Назад» его нет. Как и у `anp_`, в данных только id экземпляра:
    черновик и чат восстанавливаются по записи, поэтому старая кнопка
    списка не может открыть чужой анонс.
    """
    if action not in FIELD_ACTIONS:
        raise ValueError(f"Неизвестное действие списка полей: {action!r}")
    base = f"{FIELD_CALLBACK_PREFIX}{action}:{int(instance_id)}"
    if action == FIELD_ACTION_BACK:
        return base
    if value is None:
        raise ValueError(f"Действию списка полей {action!r} нужен параметр.")
    return f"{base}:{int(value)}"


def parse_field_callback(data: str | None) -> tuple[str, int, int | None] | None:
    """
    Разбирает `anf_{action}:{instance_id}[:{value}]`.

    `None` — данные не нашего формата; подделанный callback получает тот же
    короткий ответ «устарело», что и кнопка архивного экземпляра.
    """
    raw = (data or "").strip()
    if not raw.startswith(FIELD_CALLBACK_PREFIX):
        return None
    parts = raw.split(":")
    if len(parts) not in {2, 3}:
        return None
    action = parts[0][len(FIELD_CALLBACK_PREFIX) :]
    if action not in FIELD_ACTIONS or not parts[1].isdigit():
        return None
    instance_id = int(parts[1])
    if instance_id <= 0:
        return None
    value: int | None = None
    if len(parts) == 3:
        if not parts[2].isdigit():
            return None
        value = int(parts[2])
    if action == FIELD_ACTION_BACK:
        return (action, instance_id, None) if value is None else None
    if value is None:
        return None
    return action, instance_id, value


def panel_keyboard(
    instance_id: int,
    *,
    photos_count: int = 0,
    extras_available: bool = True,
) -> InlineKeyboardMarkup:
    """
    Клавиатура управления экземпляром: ровно действия редактора (§18/§27).

    «Изменить» и «В меню» отдельными кнопками не дублируются: категории
    правки и есть управление, а выход в меню завершает работу с анонсом.
    Порядок повторяет экран правки, к которому привык автор: данные и фото
    в одной строке, дополнительные поля — ниже.

    `extras_available` — результат общего предиката
    (`announcement_store.extras_available`): у шаблона без расширенной
    версии/кандидатов кнопка «🧩 Дополнительные поля» не показывается,
    иначе она открывала бы пустой экран (Poster 2.5.2 §4).
    """
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                tr("buttons.finished.data"),
                callback_data=panel_callback(ACTION_DATA, instance_id),
            ),
            InlineKeyboardButton(
                tr("buttons.finished.photos", count=photos_count),
                callback_data=panel_callback(ACTION_PHOTOS, instance_id),
            ),
        ]
    ]
    if extras_available:
        rows.append(
            [
                InlineKeyboardButton(
                    tr("buttons.finished.extras_edit"),
                    callback_data=panel_callback(ACTION_EXTRAS, instance_id),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                tr("buttons.finished.main_menu"),
                callback_data=panel_callback(ACTION_MENU, instance_id),
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def panel_text() -> str:
    """
    Текст отдельной панели управления (только для 2+ фото).

    Это служебная строка, а НЕ «✅ Анонс готов» и не копия анонса: панель
    лишь объясняет, к чему относятся кнопки ниже (§22).
    """
    return tr("wizard.panel.text")


async def send_panel(
    bot,
    chat_id: int,
    instance_id: int,
    *,
    photos_count: int = 0,
    extras_available: bool = True,
) -> int | None:
    """
    Отправляет ОДНУ панель управления для медиагруппы; возвращает её id.

    Панель — отдельная операция ПОСЛЕ доставки контента (§22): текст альбома
    уже ушёл caption'ом первого элемента, поэтому здесь только служебная
    строка и кнопки. `None` — Telegram не вернул сообщение; вызывающий слой
    не считает это сбоем доставки анонса.
    """
    message = await bot.send_message(
        chat_id=chat_id,
        text=panel_text(),
        parse_mode="HTML",
        reply_markup=panel_keyboard(
            instance_id,
            photos_count=photos_count,
            extras_available=extras_available,
        ),
        link_preview_options=PANEL_LINK_PREVIEW,
    )
    message_id = getattr(message, "message_id", None)
    return int(message_id) if message_id is not None else None
