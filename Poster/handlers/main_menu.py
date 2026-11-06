# handlers/main_menu.py
"""
Главное меню и навигация (Stage 11, Poster 2.0 — S2).

Главное меню — это ЭКРАН (постоянное сообщение управления, см. utils.screen):
/start и кнопки навигации перерисовывают его, а не создают новые сообщения.
Poster 2.4.1 T3: меню — три строки и четыре кнопки, единый вход в анонс:

- «📝 Создать анонс» — выбор зарегистрированной площадки; тап по названию
  сразу запускает единый конструктор. Внутри выбора площадки доступна
  кнопка «📝 Создать по личному шаблону» — конструктор без площадки;
- «📂 Мои анонсы» — незавершённые анонсы и история (`announcement_drafts`);
- «⚙️ Управление» — площадки, шаблоны, кирпичики и администрирование
  (последнее только при подтверждённых правах, роль перепроверяется в
  обработчике `/admin`);
- «❓ Помощь» — справка.

Команды /start, /help, /new_wizard, /places, /templates, /bricks, /admin,
/drafts и /cancel продолжают работать: меню — навигация, а команды остаются
совместимым входом в те же разделы. `/drafts` ведёт в единый список
«📂 Мои анонсы»; легаси-мастера и его WIP больше нет (S5).
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import (
    BaseHandler,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from about_rich import build_about_rich_message
from admin_state import clear_admin_session, has_admin_session, load_admin_session
from brick_state import clear_brick_session, has_brick_draft, load_brick_session
from database import SessionLocal
from help_rich import build_help_rich_message
from permissions import can_access_admin_panel, is_superadmin
from place_state import clear_place_session, has_place_session, load_place_session
from rich_message import send_rich_message
from template_state import (
    clear_template_session,
    has_template_session,
    load_template_session,
)
from utils import screen
from utils import tg_context as ctx
from utils.ui_texts import tr

logger = logging.getLogger(__name__)

# Категория экрана-источника справки (Poster 2.5.1). Только user_data:
# после рестарта службы возврат идёт в главное меню, а персистентный контекст
# (сессии разделов, resume-снимок конструктора) при этом не теряется.
HELP_RETURN_KEY = "help_return"


def _drop_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Явный выход из конструктора снимает авто-resume (Poster 2.3 H1).

    После рестарта службы у PTB уже нет состояния ConversationHandler,
    поэтому `/cancel`, `/start` и «🏠 В меню» обрабатываются здесь. Без
    снятия снимок «экран ждёт ввода» пережил бы выход, и следующий обычный
    текст записался бы в значение шага, затерев сохранённое. Справка
    (`/help`) выходом НЕ является (Poster 2.5.1): она снимок не снимает,
    возврат ведёт на текущий шаг.
    Импорт локальный: обратная зависимость main_menu ↔ announcement_wizard
    не должна ломать загрузку модулей.
    """
    from handlers.announcement_wizard import drop_resume_snapshot

    drop_resume_snapshot(update, context)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    """
    Inline-клавиатура главного меню: три строки, четыре кнопки (Poster 2.4.1).

    Строка 1 — единый вход в анонс (ключ `buttons.main.create_place`, callback
    `pick_place`): экран выбора площадки, откуда доступен и личный шаблон.
    Строка 2 — `buttons.main.announcements`; строка 3 — навигация
    `buttons.main.manage` | `buttons.main.help`. Callback-данные прежние:
    pick_place — выбор зарегистрированной площадки и немедленный старт
    конструктора, my_announcements — история анонсов, manage — раздел
    управления, help — справка.
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr("buttons.main.create_place"), callback_data="pick_place"
                )
            ],
            [
                InlineKeyboardButton(
                    tr("buttons.main.announcements"), callback_data="my_announcements"
                )
            ],
            [
                InlineKeyboardButton(tr("buttons.main.manage"), callback_data="manage"),
                InlineKeyboardButton(tr("buttons.main.help"), callback_data="help"),
            ],
        ]
    )


def manage_keyboard(*, show_admin: bool = False) -> InlineKeyboardMarkup:
    """
    Подменю «⚙️ Управление» (Poster 2.5 §10: компактная раскладка).

    Строки: «📍 Площадки»; «📝 Шаблоны | 🧩 Кирпичики»; «🛡 Администрирование»
    (только при подтверждённых правах); «🏠 В меню». Пустых кнопок нет, а
    обработчик `/admin` перепроверяет роль заново, поэтому скрытая кнопка —
    удобство, а не защита. Callback-данные прежние.
    """
    rows = [
        [
            InlineKeyboardButton(
                tr("buttons.manage.places"), callback_data="manage_places"
            )
        ],
        [
            InlineKeyboardButton(
                tr("buttons.manage.templates"), callback_data="manage_templates"
            ),
            InlineKeyboardButton(
                tr("buttons.manage.bricks"), callback_data="manage_bricks"
            ),
        ],
    ]
    if show_admin:
        rows.append(
            [
                InlineKeyboardButton(
                    tr("buttons.manage.admin"), callback_data="admin_panel"
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(tr("buttons.nav.main_menu"), callback_data="main_menu")]
    )
    return InlineKeyboardMarkup(rows)


def _show_admin_button(telegram_user_id: int) -> bool:
    """Видимость администрирования; роль всё равно перепроверяется в handler."""
    if is_superadmin(telegram_user_id):
        return True
    session = SessionLocal()
    try:
        return can_access_admin_panel(session, telegram_user_id)
    except Exception as e:  # noqa: BLE001 — меню должно открыться и без кабинета
        # Причина отказа видимости должна быть видна в логах: раньше здесь
        # был только текст ошибки без traceback, и «кнопка исчезла» было
        # невозможно диагностировать (S2, §7.5, гипотеза 3).
        logger.exception("Не удалось проверить доступ в кабинет: %s", e)
        return False
    finally:
        session.close()


def main_menu_text() -> str:
    """Текст экрана главного меню."""
    return tr("menu.main")


def manage_text() -> str:
    """Текст экрана «Управление»."""
    return tr("menu.manage")


async def render_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Рисует главное меню на существующем экране (или создаёт новый экран).

    После рестарта процесса ref экрана восстанавливается из БД один раз —
    /start редактирует старое сообщение, а не плодит дубли. Меню всегда
    состоит из постоянных четырёх кнопок в трёх строках: легаси-WIP удалён
    (S5), поэтому условных кнопок «продолжить» нет — незавершённые анонсы
    живут в «📂 Мои анонсы».
    """
    # навигация пользовательским сообщением → старый экран удаляется,
    # новый создаётся рядом с вводом (Stage 16, дефект №7); callback —
    # guard-пропуск, кнопки и так на экране
    await screen.reanchor_screen(update, context)
    # Poster 2.5 §53: справка — временный Rich Message. Возврат в меню
    # удаляет её (Rich Message нельзя перерисовать в обычный экран), а
    # анонсы пользователя не трогаются. После рестарта службы id есть только
    # в `user_states` — отсюда user_id.
    await screen.drop_rich_temp(
        context.bot, ctx.chat(update).id, ctx.data(context), ctx.user(update).id
    )
    # R1, дефект №23: главное меню — вход в актуальный UI, у клиента могла
    # остаться reply-клавиатура старых версий. Снимается один раз на вход.
    await screen.drop_legacy_reply_keyboard(update, context)
    user_data = ctx.data(context)
    user_id = ctx.user(update).id
    # Poster 2.3 H1: меню — экран без ввода. Снимок «экран ждёт значения»
    # снимается и здесь: после рестарта службы меню рисуют глобальные
    # обработчики, где состояния диалога конструктора уже нет.
    _drop_resume(update, context)
    # Главное меню — другая ветка сценария: незавершённый выбор места,
    # конструктор личного кирпичика и административный ввод не должны
    # перехватывать следующий текст. WIP-пост при этом не трогается.
    clear_place_session(user_id)
    clear_admin_session(user_id)
    clear_brick_session(user_id)
    clear_template_session(user_id)
    if screen.screen_ref(user_data) is None:
        screen.hydrate_screen_ref(user_data, user_id)
    chat_id = ctx.chat(update).id
    await screen.render_screen(
        context,
        user_data,
        chat_id,
        main_menu_text(),
        main_menu_keyboard(),
        user_id=user_id,
    )
    # ref экрана закрепляется в БД; payload не трогаем
    screen.save_screen_ref(user_id, user_data, chat_id)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Команда /start: главное меню или конструктор по deep-link.

    Стартовый личный шаблон выдаётся раньше — при первом контакте с ботом
    (`handlers/user_tracking.py`), поэтому `/start` внутри активного диалога
    ничего не теряет: выдача не зависит от того, какая ветка обработала
    команду. Повторный `/start` и удаление шаблона его не воскрешают.

    Deep-link payload разбирается ДО меню: `/start tf_...` — передача прав,
    `/start <token>` — ссылка на создание анонса площадки (Poster 2.2 U4b).
    `/start` без payload работает как прежде.
    """
    from handlers.admin import handle_transfer_start
    from handlers.place_invite import open_invite_payload

    if await handle_transfer_start(update, context):
        # /start с payload передачи — отдельный экран; команду убираем так же,
        # как в обычном меню. WIP-пост не трогается.
        await screen.drop_user_input(update, context)
        return ConversationHandler.END
    invite_state = await open_invite_payload(update, context)
    if invite_state is not None:
        # Ссылка обработана: конструктор площадки или отказ. WIP-пост не
        # трогается; сообщение с командой обработчик убирает сам.
        return invite_state
    await render_main_menu(update, context)
    # Сообщение с командой — временный ввод: меню уже на экране, убираем
    # его best-effort (при сбое очистки диалог не ломается)
    await screen.drop_user_input(update, context)
    return ConversationHandler.END


async def _close_screen(
    context: ContextTypes.DEFAULT_TYPE,
    user_data: dict,
    user_id: int,
    chat_id: int,
) -> None:
    """
    Закрывает текущий служебный экран перед открытием Rich Message (§53).

    Справка — временный экран, но Rich Message нельзя превратить обратно в
    текст обычного экрана. Поэтому прежний экран (меню/раздел) удаляется
    best-effort, а ref очищается в user_data и в БД — WIP-payload при этом
    сохраняется. Двух конкурирующих меню в чате не остаётся: вернувшись из
    справки, пользователь получит НОВЫЙ экран, а не правку старого.
    """
    ref = screen.screen_ref(user_data)
    if ref is None:
        ref = screen.hydrate_screen_ref(user_data, user_id)
    if ref is None:
        return
    user_data.pop("screen", None)
    await screen.cleanup_messages(context.bot, ref["chat_id"], [ref["message_id"]])
    screen.save_screen_ref(user_id, user_data, chat_id)


def _draft_from_resume_payload(user_id: int) -> int | None:
    """
    draft_id из resume-снимка конструктора (`user_states.payload`, H1).

    Снимок существует только там, где экран конструктора ждёт значения
    (вопрос или шаг фото). Это единственный персистентный признак живого
    черновика, доступный справке: `announcement_ui` живёт в памяти и
    очищается вызывающим кодом конструктора ещё до `render_help`.
    """
    from handlers.announcement_wizard import RESUME_PAYLOAD_KEY

    payload = screen.load_payload(user_id) or {}
    snapshot = payload.get(RESUME_PAYLOAD_KEY)
    if not isinstance(snapshot, dict):
        return None
    draft_id = snapshot.get("draft_id")
    if isinstance(draft_id, bool) or not isinstance(draft_id, int):
        return None
    return draft_id


def _place_help_source(user_id: int) -> str | None:
    """
    Открыт ли раздел «Места» — и какой именно экран.

    «Управление → Площадки» (справочник) — отдельный маршрут: обычный
    `open_places` такие фазы не возобновляет (сбрасывает в хаб), поэтому
    возврат идёт в `open_places_manage` со страницей и запросом из сессии.
    """
    from handlers import places

    state = load_place_session(user_id)
    if not isinstance(state, dict):
        return None
    phase = state.get("phase")
    if phase in places.MANAGE_ONLY_PHASES:
        return "places_manage"
    if phase in places.PHASES:
        return "places"
    return None


def _template_help_source(user_id: int) -> bool:
    """Открыт ли раздел шаблонов (хаб, карточка, правка, пикер)."""
    from handlers import templates

    state = load_template_session(user_id)
    if not isinstance(state, dict):
        return False
    return state.get("phase") in templates.PHASES


def _brick_help_source(user_id: int) -> bool:
    """Открыт ли раздел кирпичиков (списки, карточка, мастер)."""
    from handlers import bricks

    state = load_brick_session(user_id)
    if not isinstance(state, dict):
        return False
    return state.get("phase") in bricks.PHASES


def _admin_help_source(user_id: int) -> bool:
    """Открыт ли административный кабинет."""
    from handlers import admin

    state = load_admin_session(user_id)
    if not isinstance(state, dict):
        return False
    return state.get("phase") in admin.PHASES


def help_return_target(user_id: int, user_data: dict) -> str:
    """
    Экран-источник справки: куда вернёт «◀️ Назад» (Poster 2.5.1).

    Категория нужна, потому что Rich Message нельзя превратить обратно в
    обычный экран: сам экран закрывается, а контекст (персистентные сессии
    разделов, resume-снимок конструктора) остаётся и перерисовывается на
    возврате. Порядок проверок — от живых сессий разделов к конструктору:
    сессии разделов взаимоисключающие (вход в раздел чистит остальные), а
    resume-снимок конструктора может остаться от прежнего визита — при
    активном разделе он не действует (`_other_session_active`).

    Возвращает «places»/«places_manage»/«templates»/«bricks»/«admin»/
    «wizard»/«main». «main» — справка из главного меню, а также состояния,
    где возврат технически невозможен: единственный безопасный адрес — меню.
    """
    place = _place_help_source(user_id)
    if place is not None:
        return place
    if _template_help_source(user_id):
        return "templates"
    if _brick_help_source(user_id):
        return "bricks"
    if _admin_help_source(user_id):
        return "admin"
    # Конструктор: в памяти ui уже пуст (`_clear_ui` вызывается ровно перед
    # `render_help`), поэтому надёжный признак черновика — resume-снимок в
    # `user_states.payload`. Живой ui проверяется для прямых вызовов справки.
    if _draft_from_resume_payload(user_id) is not None:
        return "wizard"
    ui = user_data.get("announcement_ui")
    if isinstance(ui, dict) and ui.get("draft_id"):
        return "wizard"
    return "main"


async def _render_help_screen(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    user_data: dict,
    chat_id: int,
    *,
    keep_source: bool,
) -> bool:
    """
    Отправляет Rich Message справки и запоминает его id (Poster 2.5.4 §7).

    Общий путь для первого открытия и для возврата с «О проекте». Порядок
    обязателен: удалить прежний временный Rich Message (справку ИЛИ
    «О проекте»), закрыть служебный экран, отправить справку, запомнить id.

    `keep_source=True` — возврат с «О проекте»: категория источника уже
    записана при первом открытии справки, и пересчитывать её нельзя
    (иначе «Назад» из справки ушёл бы не туда, откуда её открыли).

    Возвращает False, если Telegram отверг структурированное сообщение:
    тогда показан обычный экран с понятной причиной.
    """
    if not keep_source:
        # Категория источника — только в памяти: после рестарта возврат идёт
        # в главное меню, а персистентный контекст при этом цел.
        user_data[HELP_RETURN_KEY] = help_return_target(user_id, user_data)
    # Повторное открытие не копит Rich Message: прежнее удаляется.
    # user_id — чтобы найти id в БД, если он потерян из user_data после
    # рестарта службы (§53).
    await screen.drop_rich_temp(context.bot, chat_id, user_data, user_id)
    await _close_screen(context, user_data, user_id, chat_id)

    # Poster 2.5.4 §7: обе кнопки в ОДНОЙ строке — возврат на экран-источник
    # и переход к «О проекте» (второй и последний Rich Message).
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr("buttons.help_back"), callback_data="help_back"
                ),
                InlineKeyboardButton(
                    tr("buttons.help_about"), callback_data="help_about"
                ),
            ]
        ]
    )
    try:
        message = await send_rich_message(
            context.bot, chat_id, build_help_rich_message(), reply_markup=keyboard
        )
    except Exception as e:  # noqa: BLE001 — справка не должна ронять бота
        # Токен в текст не попадает: адаптер его не читает и не печатает.
        logger.warning("Rich Message справки не отправлено: %s", e)
        # Справки нет — возврата «◀️ Назад» тоже: плашка сбоя живёт на меню.
        user_data.pop(HELP_RETURN_KEY, None)
        await screen.render_screen(
            context,
            user_data,
            chat_id,
            tr("help.rich.unavailable"),
            main_menu_keyboard(),
            user_id=user_id,
        )
        screen.save_screen_ref(user_id, user_data, chat_id)
        return False

    # Адаптер возвращает Message или (при неполном ответе) message_id —
    # обе формы равнозначны для навигации. id пишется и в user_data, и в
    # `user_states` (§53): после рестарта службы Rich Message иначе нечем
    # удалить, и он остался бы живой кнопкой рядом с новым экраном.
    if isinstance(message, Message):
        screen.remember_rich_temp(user_id, user_data, message.message_id)
    else:
        screen.remember_rich_temp(user_id, user_data, int(message))
    return True


async def render_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Справка: настоящий Rich Message `sendRichMessage` (Poster 2.5 §46–53).

    Порядок: запомнить экран-источник, закрыть его, отправить Rich Message с
    кнопками «◀️ Назад» и «ℹ️ О проекте» в одной строке, запомнить message_id
    как служебный. Обычного `sendMessage` здесь нет: подменять Rich Message
    текстом запрещено (§54). При отказе Telegram справка не показывается «как
    будто всё хорошо»: пользователь получает короткое сообщение в обычном
    экране, причина — в лог.

    Poster 2.5.1: источник НЕ стирается. Сессии разделов, resume-снимок и ui
    конструктора остаются — их не удаляют ни `_drop_resume`, ни
    `clear_*_session`: `help_back` перерисует предыдущий служебный экран из
    сохранённого контекста. Закрывается только сам экран (Rich Message
    нельзя рисовать рядом с активной клавиатурой — иначе в чате две
    конкурирующие клавиатуры).
    """
    # навигация сообщением ниже экрана → экран пересоздаётся рядом с вводом
    await screen.reanchor_screen(update, context)
    await _render_help_screen(
        context,
        ctx.user(update).id,
        ctx.data(context),
        ctx.chat(update).id,
        keep_source=False,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Справка по кнопкам меню и командам."""
    await render_help(update, context)


async def menu_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inline-кнопка «❓ Помощь» главного меню."""
    await ctx.query(update).answer()
    await render_help(update, context)


def about_keyboard() -> InlineKeyboardMarkup:
    """
    Клавиатура «О проекте»: единственная кнопка — возврат к справке.

    Та же кнопка стоит на плашке сбоя: Rich Message не отправился, но
    пользователь не должен остаться без пути назад.
    """
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(tr("buttons.about_back"), callback_data="about_help")]]
    )


async def render_about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    «ℹ️ О проекте»: второй Rich Message Poster (Poster 2.5.4 §7).

    Порядок: удалить Rich Message справки (в чате не должно остаться двух
    клавиатур), закрыть служебный экран, отправить «О проекте» с кнопкой
    «◀️ К помощи», запомнить id как временный Rich Message.

    `help_return` НЕ пересчитывается и не стирается: справка при открытии
    уже записала экран-источник, и после возврата «◀️ Назад» должен вести
    туда же — в раздел, на страницу списка или на шаг черновика.

    Отказ Telegram не оставляет пользователя без выхода: показывается
    обычный экран с понятной причиной и кнопкой возврата к справке.
    """
    await ctx.query(update).answer()
    user_id = ctx.user(update).id
    user_data = ctx.data(context)
    chat_id = ctx.chat(update).id
    # Справка — тоже временный Rich Message: удаляется перед «О проекте»
    # (id ищется и в user_data, и в БД, поэтому работает после рестарта).
    await screen.drop_rich_temp(context.bot, chat_id, user_data, user_id)
    await _close_screen(context, user_data, user_id, chat_id)

    try:
        message = await send_rich_message(
            context.bot,
            chat_id,
            build_about_rich_message(),
            reply_markup=about_keyboard(),
        )
    except Exception as e:  # noqa: BLE001 — экран не должен ронять бота
        # Токен в текст не попадает: адаптер его не читает и не печатает.
        logger.warning("Rich Message «О проекте» не отправлено: %s", e)
        await screen.render_screen(
            context,
            user_data,
            chat_id,
            tr("about.rich.unavailable"),
            about_keyboard(),
            user_id=user_id,
        )
        screen.save_screen_ref(user_id, user_data, chat_id)
        return

    if isinstance(message, Message):
        screen.remember_rich_temp(user_id, user_data, message.message_id)
    else:
        screen.remember_rich_temp(user_id, user_data, int(message))


async def about_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    «◀️ К помощи» на «О проекте» → Rich Message справки (Poster 2.5.4 §7).

    Сначала удаляется «О проекте» (временный Rich Message), затем справка
    отправляется заново. `help_return` сохраняется как есть: источник
    записан при первом открытии справки, поэтому «◀️ Назад» на ней вернёт
    пользователя туда, откуда он пришёл. При отказе Telegram работает общий
    путь `_render_help_screen` — обычный экран с причиной и меню.
    """
    await ctx.query(update).answer()
    user_id = ctx.user(update).id
    user_data = ctx.data(context)
    chat_id = ctx.chat(update).id
    await screen.drop_rich_temp(context.bot, chat_id, user_data, user_id)
    await _render_help_screen(context, user_id, user_data, chat_id, keep_source=True)


async def _restore_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Восстанавливает экран текущего этапа черновика (Poster 2.5.1).

    Источник — resume-снимок конструктора (`user_states.payload`): именно он
    переживает рестарт. `_resume_target` проверяет снимок по актуальному
    черновику и готовит `announcement_ui`, поэтому возврат рисует ТОТ ЖЕ шаг.
    Шаг фотографий рисуется напрямую (`render_photo_step`): `_resume_current`
    различает только этап черновика и для «ждём фото» показал бы обзор,
    закрыв приём снимков. Для вопроса достаточно `_resume_current` — он
    учитывает `active_brick_id` из снимка. Если снимка нет, а в
    `announcement_ui` остался `draft_id` (экран без ввода), этого
    достаточно: `_resume_current` сам выберет безопасный экран этапа.

    Возвращает False, если черновика нет — вызывающий уходит в меню.
    """
    from handlers import announcement_wizard as wizard

    draft_id = _draft_from_resume_payload(ctx.user(update).id)
    ui = ctx.data(context).get("announcement_ui")
    ui_draft = ui.get("draft_id") if isinstance(ui, dict) else None
    if draft_id is None and not ui_draft:
        return False

    if draft_id is not None:
        payload = screen.load_payload(ctx.user(update).id) or {}
        snapshot = payload.get(wizard.RESUME_PAYLOAD_KEY)
        chat = ctx.chat(update)
        prepared = wizard._resume_target(  # noqa: SLF001 — общий разбор снимка
            ctx.user(update).id,
            snapshot if isinstance(snapshot, dict) else {},
            chat.id if chat is not None else None,
        )
        if prepared is not None:
            details, restored_ui = prepared
            ctx.data(context)["announcement_ui"] = restored_ui
            if restored_ui.get("photo_step"):
                # Снимок «ждём фото»: текущий этап — шаг фотографий, а не
                # обзор. `_resume_current` такой режим не различает, поэтому
                # рисуем шаг напрямую — иначе возврат из справки закрыл бы
                # приём фото и снимок resume снялся бы.
                from handlers import wizard_photos

                await wizard_photos.render_photo_step(
                    update,
                    context,
                    details,
                    announcement_is_empty=wizard._announcement_is_empty(  # noqa: SLF001
                        update, details
                    ),
                )
                return True
        else:
            # Снимок устарел (этап сменился): безопасный экран по черновику.
            restored_ui = wizard._ui(context)  # noqa: SLF001
            restored_ui["draft_id"] = draft_id
            restored_ui["revision"] = None
            ctx.data(context)["announcement_ui"] = restored_ui

    await wizard._resume_current(update, context)  # noqa: SLF001
    return True


async def help_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    «◀️ Назад» на Rich Message справки → экран-источник (Poster 2.5.1).

    Порядок обязателен: сначала удалить временный Rich Message (его id есть
    и в user_data, и в `user_states`, поэтому очистка работает и после
    рестарта службы — §53), затем восстановить источник. Иначе в чате
    остались бы две конкурирующие клавиатуры.

    Категорию источника запоминает `render_help` в `user_data`. Она живёт
    только в памяти: после рестарта её нет и возврат идёт в главное меню.
    Персистентность обеспечивают сами сессии разделов и resume-снимок
    конструктора — их `render_help` больше не стирает.

    Ветки восстановления — существующие входы разделов (`open_*`), поэтому
    страница, поиск, фильтры и объект берутся из персистентной сессии. Для
    конструктора — `_resume_current` (безопасный экран текущего этапа
    черновика). Нет данных для возврата — честный фолбэк в главное меню.

    Ответ на callback делает вызываемый вход раздела сам (`open_places` и
    остальные отвечают внутри), поэтому здесь он нужен только для меню и
    конструктора: повторный `answer` Telegram считает ошибкой запроса.
    """
    user_id = ctx.user(update).id
    user_data = ctx.data(context)
    chat_id = ctx.chat(update).id
    # Rich Message — временное сообщение: удаляется в любом случае.
    await screen.drop_rich_temp(context.bot, chat_id, user_data, user_id)
    source = user_data.pop(HELP_RETURN_KEY, None)

    if source == "places":
        from handlers.places import open_places

        await open_places(update, context)
        return
    if source == "places_manage":
        from handlers.places import open_places_manage

        await open_places_manage(update, context)
        return
    if source == "templates":
        from handlers.templates import open_templates

        await open_templates(update, context)
        return
    if source == "bricks":
        from handlers.bricks import open_bricks

        await open_bricks(update, context)
        return
    if source == "admin":
        from handlers.admin import open_admin

        await open_admin(update, context)
        return
    if source == "wizard":
        await restore_wizard_or_menu(update, context)
        return
    await ctx.query(update).answer()
    await render_main_menu(update, context)


async def restore_wizard_or_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Возврат в конструктор анонса с безопасным фолбэком (Poster 2.5.2 §3.1).

    Общий маршрут для «◀️ Назад» из справки и из хаба кирпичиков, открытого
    с шага конструктора: сначала восстанавливается экран текущего шага из
    resume-снимка, иначе — главное меню (черновика нет, возврат технически
    невозможен). Ответ на callback делается здесь ровно один раз: повторный
    `answer` Telegram считает ошибкой запроса.
    """
    await ctx.query(update).answer()
    if await _restore_wizard(update, context):
        return
    # Черновика нет — возврат технически невозможен: безопасное меню.
    logger.info(
        "Возврат в конструктор невозможен: user_id=%s — меню.",
        ctx.user(update).id,
    )
    await render_main_menu(update, context)


async def drafts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Команда /drafts — совместимый вход в единый список «📂 Мои анонсы».

    Отдельного легаси-хранилища Draft больше нет (S5): команда открывает
    тот же экран, что и кнопка меню, без второго списка.
    """
    from handlers.announcement_wizard import open_my_announcements

    await open_my_announcements(update, context)


async def menu_drafts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Совместимость: старая кнопка «📂 Черновики» в открытых экранах."""
    await ctx.query(update).answer()
    from handlers.announcement_wizard import open_my_announcements

    await open_my_announcements(update, context)


async def open_manage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Экран «⚙️ Управление»: разделы и администрирование по правам."""
    query = getattr(update, "callback_query", None)
    if query is not None:
        await query.answer()
    await screen.reanchor_screen(update, context)
    user_id = ctx.user(update).id
    clear_place_session(user_id)
    clear_admin_session(user_id)
    clear_brick_session(user_id)
    clear_template_session(user_id)
    user_data = ctx.data(context)
    if screen.screen_ref(user_data) is None:
        screen.hydrate_screen_ref(user_data, user_id)
    chat_id = ctx.chat(update).id
    await screen.render_screen(
        context,
        user_data,
        chat_id,
        manage_text(),
        manage_keyboard(show_admin=_show_admin_button(user_id)),
        user_id=user_id,
    )
    screen.save_screen_ref(user_id, user_data, chat_id)


async def manage_places(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«Управление» → «Площадки»: справочник мест (не выбор для анонса)."""
    from handlers.places import open_places_manage

    await open_places_manage(update, context)


async def manage_templates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    «Управление» → «Шаблоны»: только управление шаблонами.

    Вход не возобновляет сохранённую фазу `pick` и никогда не запускает
    создание анонса (R2, дефект №25): раздел открывается списком управления.
    """
    from handlers.templates import open_templates

    await open_templates(update, context)


async def manage_bricks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    «Управление» → «Кирпичики».

    Источник входа передаётся в раздел: «◀️ Назад» в личном хабе вернёт
    сюда же, в «Управление» (Poster 2.5.2 §3.1, сценарий 11), а не в
    главное меню. Источник хранится в сессии и переживает рестарт.
    """
    from handlers.bricks import HUB_ORIGIN_MANAGE, open_bricks

    await open_bricks(update, context, origin=HUB_ORIGIN_MANAGE)


async def cancel_without_dialog(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """/cancel без активного диалога: отмена места/кабинета или подсказка."""
    # навигация сообщением ниже экрана → новый экран рядом с вводом
    await screen.reanchor_screen(update, context)
    # Poster 2.3 H1: отмена закрывает приём ввода конструктора. Отдельный
    # вызов нужен для post-restart-пути: там состояния диалога уже нет, и
    # только здесь снимается снимок «экран ждёт значения».
    _drop_resume(update, context)
    user_id = ctx.user(update).id
    place_active = has_place_session(user_id)
    admin_active = has_admin_session(user_id)
    brick_active = has_brick_draft(user_id)
    template_active = has_template_session(user_id)
    if place_active:
        clear_place_session(user_id)
    if admin_active:
        clear_admin_session(user_id)
    if brick_active:
        clear_brick_session(user_id)
    if template_active:
        clear_template_session(user_id)
    user_data = ctx.data(context)
    if screen.screen_ref(user_data) is None:
        screen.hydrate_screen_ref(user_data, user_id)
    if place_active:
        text = tr("menu.cancel.place")
    elif admin_active:
        text = tr("menu.cancel.admin")
    elif brick_active:
        text = tr("menu.cancel.brick")
    elif template_active:
        text = tr("menu.cancel.template")
    else:
        text = tr("menu.cancel.none")
    cancelled = place_active or admin_active or brick_active or template_active
    await screen.render_screen(
        context,
        user_data,
        ctx.chat(update).id,
        text,
        main_menu_keyboard() if cancelled else None,
        user_id=user_id,
    )
    if cancelled:
        await screen.drop_user_input(update, context)


async def outside_dialog_hint(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Подсказка на обычный текст/фото ВНЕ диалога: обновляется на экране
    управления, а не копится новыми сообщениями.

    Внутри диалога этот хендлер не вызывается: ConversationHandler анонсов и
    прежнего мастера зарегистрированы первой группой и свой ввод обслуживают
    сами.
    """
    message = ctx.message(update)
    # навигация сообщением ниже экрана → подсказка рядом с вводом
    await screen.reanchor_screen(update, context)
    user_data = ctx.data(context)
    if screen.screen_ref(user_data) is None:
        screen.hydrate_screen_ref(user_data, ctx.user(update).id)
    if message.photo:
        text = tr("menu.outside.photo")
    else:
        text = tr("menu.outside.text")
    await screen.render_screen(
        context, user_data, ctx.chat(update).id, text, user_id=ctx.user(update).id
    )


async def handle_main_menu_selection(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Callback «main_menu» ВНЕ диалога конструктора: возврат в главное меню.

    Раньше жил в `handlers/callbacks.py`; после удаления легаси (S5) это
    единственный живой маршрут того модуля, поэтому он переехал сюда.
    Экран перерисовывается (Stage 11: один живой экран управления), новых
    сообщений не создаётся.
    """
    await ctx.query(update).answer()
    await render_main_menu(update, context)


def main_menu_handlers() -> list[BaseHandler]:
    """
    Обработчики главного меню Poster 2.0.

    Кнопки создания анонса — entry-точки ConversationHandler анонсов
    (`handlers/announcement_wizard.py`), поэтому здесь они не регистрируются:
    состояние диалога иначе не отслеживалось бы. «Мои анонсы» и «Управление»
    — экраны навигации без собственного диалога.

    Порядок важен: /cancel вне диалога и подсказка на обычный текст/фото
    стоят ПОСЛЕ команд и callback'ов — они не перехватывают ни их, ни
    состояния диалогов (ConversationHandler'ы идут первой группой в bot.py).
    """
    handlers: list[BaseHandler] = [
        CommandHandler("start", start),
        CommandHandler("help", help_command),
        CallbackQueryHandler(handle_main_menu_selection, pattern=r"^main_menu$"),
        CommandHandler("drafts", drafts_command),
        CommandHandler("cancel", cancel_without_dialog),
        CallbackQueryHandler(menu_drafts, pattern="^drafts$"),
        CallbackQueryHandler(menu_help, pattern="^help$"),
        # Poster 2.5.1: возврат из Rich Message справки на экран-источник.
        CallbackQueryHandler(help_back, pattern="^help_back$"),
        # Poster 2.5.4 §7: «ℹ️ О проекте» со справки и возврат «◀️ К помощи».
        CallbackQueryHandler(render_about, pattern="^help_about$"),
        CallbackQueryHandler(about_back, pattern="^about_help$"),
        CallbackQueryHandler(open_manage, pattern="^manage$"),
        CallbackQueryHandler(manage_places, pattern="^manage_places$"),
        CallbackQueryHandler(manage_templates, pattern="^manage_templates$"),
        CallbackQueryHandler(manage_bricks, pattern="^manage_bricks$"),
        # Последним: обычный текст/фото вне диалога раньше оставались
        # без ответа («тишина»). Команды исключены (~COMMAND), поэтому
        # unknown_command и dialog-fallback'и работают как прежде.
        MessageHandler(
            (filters.TEXT | filters.PHOTO) & ~filters.COMMAND, outside_dialog_hint
        ),
    ]
    return handlers
