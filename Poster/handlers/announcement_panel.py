# handlers/announcement_panel.py
"""
Poster 2.5, C3: управление готовым анонсом вне конструктора (§30–33).

Кнопки `anp_{action}:{instance_id}` живут на самом анонсе (0–1 фото) или на
отдельной панели медиагруппы (`announcement_controls.panel_keyboard`). Они
привязаны к КОНКРЕТНОМУ экземпляру и обязаны работать после рестарта службы,
когда состояния диалога уже нет. Поэтому обработчик зарегистрирован отдельно
от `ConversationHandler` и раньше него в группе 0: PTB вызывает не более
одного обработчика на группу, и `anp_`-нажатие не доходит до fallback `.*`
конструктора.

Порядок проверок — защита старых callback (§32): сначала экземпляр по id,
затем чат и владелец черновика. Нет экземпляра, он архивный, нажатие пришло
из другого чата или запись удалена — короткий ответ и НИКАКИХ правок экранов
и БД: старая кнопка не восстанавливает прежнюю версию и не открывает чужой
редактор.

Действий ровно четыре (как и кнопок): «Данные» → компактный экран правки,
«Фото» → коллекция вложений, «Доп. поля» → список доп. полей, «В меню» →
главное меню. Экраны переиспользуются из конструктора: второй набор экранов
для готового анонса не нужен. После действия состояние диалога выставляется
явно (`remember_conversation_state`), иначе следующий `aw_`-callback попал
бы в глобальный обработчик «кнопка устарела».
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import BaseHandler, CallbackQueryHandler, ContextTypes

import announcement_controls
from announcement_instances import AnnouncementInstance, get_instance
from announcement_store import (
    AnnouncementActorError,
    AnnouncementError,
    AnnouncementNotFoundError,
    get_announcement_draft,
)
from database import SessionLocal
from handlers import announcement_wizard as wizard
from utils import tg_context as ctx
from utils.ui_texts import tr

logger = logging.getLogger(__name__)

# Какое состояние диалога соответствует экрану после действия панели.
# «В меню» не открывает экран конструктора — состояние закрывается.
PANEL_STATES = {
    announcement_controls.ACTION_DATA: wizard.AW_PREVIEW,
    announcement_controls.ACTION_PHOTOS: wizard.AW_PREVIEW,
    announcement_controls.ACTION_EXTRAS: wizard.AW_EXTRAS,
}


def _instance(instance_id: int) -> AnnouncementInstance | None:
    """Экземпляр по id; None — строки нет (в т.ч. после удаления анонса)."""
    with SessionLocal() as session:
        return get_instance(session, instance_id)


def _details(user_id: int, instance: AnnouncementInstance):
    """
    Черновик экземпляра только его автору; None — запись недоступна.

    Проверка нужна даже при найденном экземпляре: без включённого `PRAGMA
    foreign_keys` (или при снятом каскаде) строка экземпляра может пережить
    удаление анонса (§33). Такой callback обязан получить то же пояснение,
    что и архивный, а не открыть редактор удалённой записи.
    """
    try:
        with SessionLocal() as session:
            return get_announcement_draft(
                session,
                actor_telegram_user_id=user_id,
                draft_id=instance.draft_id,
            )
    except (
        AnnouncementNotFoundError,
        AnnouncementActorError,
        AnnouncementError,
    ) as exc:
        logger.info(
            "Экземпляр #%s недоступен: черновик #%s не читается (%s).",
            instance.id,
            instance.draft_id,
            exc,
        )
        return None


async def _stale_answer(update: Update) -> None:
    """Короткое пояснение на устаревшую кнопку: без правок и сообщений."""
    await wizard._answer(update, announcement_controls.STALE_INSTANCE_TEXT, alert=True)


async def handle_panel_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Действие панели готового анонса по id экземпляра (§26–27, §32).

    Спиннер на клиенте закрывается `answer()` в любом исходе: устаревшая
    кнопка получает пояснение, действующая — выбранный экран. Данные
    черновика здесь не меняются: это навигация, правку делает сам редактор.
    """
    parsed = announcement_controls.parse_panel_callback(ctx.query(update).data)
    if parsed is None:
        # Формат уже отфильтрован `PANEL_CALLBACK_PATTERN`; подделанный
        # callback всё равно не должен оставить клиент без ответа.
        await _stale_answer(update)
        return
    action, instance_id = parsed

    instance = _instance(instance_id)
    if instance is None or instance.is_archived:
        await _stale_answer(update)
        return
    if instance.chat_id != ctx.chat(update).id:
        # Кнопка пересланного анонса в другом чате не открывает редактор:
        # экран и клавиатура экземпляра принадлежат чату доставки.
        await _stale_answer(update)
        return
    details = _details(ctx.user(update).id, instance)
    if details is None:
        await _stale_answer(update)
        return

    await wizard._answer(update)
    if action == announcement_controls.ACTION_MENU:
        # Панель и сам анонс остаются на месте: «В меню» — навигация.
        await wizard._exit_to_main_menu(update, context)
        wizard.forget_conversation_state(update, context)
        return

    if action != announcement_controls.ACTION_PHOTOS and wizard._edit_blocked(details):
        # Идёт отправка: правка запрещена так же, как по кнопке «✏️ Изменить».
        # Коллекция фото объясняет запрет сама, поэтому сюда не попадает.
        await wizard._answer(update, tr("alerts.wizard.otpravka_nachata"), alert=True)
        await wizard._render_publish_status(update, context, details)
        return

    wizard.remember_conversation_state(update, context, PANEL_STATES[action])
    if action == announcement_controls.ACTION_DATA:
        await wizard._render_delivered_edit(update, context, details)
    elif action == announcement_controls.ACTION_PHOTOS:
        await wizard._render_photos(update, context, details)
    else:
        await wizard._enter_extras(update, context, details)


def announcement_panel_handlers() -> list[BaseHandler]:
    """
    Обработчик кнопок готового анонса — один на всё приложение.

    Регистрируется в группе 0 раньше конструктора: `anp_` не пересекается с
    префиксами сценариев, а порядок гарантирует, что нажатие не съест
    fallback активного диалога.
    """
    return [
        CallbackQueryHandler(
            handle_panel_callback,
            pattern=announcement_controls.PANEL_CALLBACK_PATTERN,
        )
    ]


__all__ = ["handle_panel_callback", "announcement_panel_handlers"]
