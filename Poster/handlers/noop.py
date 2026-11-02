# handlers/noop.py
"""
Безопасный no-op callback для «неактивных» кнопок (Poster 2.5, spec §14).

Bot API 10.3 знает поле `disabled` (DisabledButton), но PTB 22.8 его не
поддерживает: в `InlineKeyboardButton.__slots__` нет `disabled`. Поэтому
индикатор пагинации `2/4` создаётся обычной кнопкой с callback
`noop:<page>:<total>`, а этот обработчик:

- отвечает коротким `answer()` — спиннер на клиенте не висит;
- ничего не меняет в состоянии и БД;
- не отправляет и не редактирует сообщения.

Обработчик регистрируется один раз глобально (`build_application`) и
работает при любом состоянии диалога: `noop:` не пересекается с префиксами
сценариев. Регистрация — ПЕРВЫМ в группе 0: PTB вызывает не более одного
обработчика на группу, поэтому до fallback `.*` активного диалога нажатие
индикатора не доходит (возврат `True` сам по себе цепочку не останавливает —
в PTB это делает только `ApplicationHandlerStop`).
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import BaseHandler, CallbackQueryHandler, ContextTypes

from utils import tg_context as ctx

logger = logging.getLogger(__name__)

NOOP_PATTERN = r"^noop:\d+:\d+$"


async def handle_noop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Подтверждает нажатие индикатора, не выполняя действий.

    Спиннер на клиенте закрывается `answer()`, состояние и БД не меняются,
    сообщение не редактируется. Приоритет над диалогами обеспечивает
    порядок регистрации (см. `handlers/noop.py` в шапке модуля).
    """
    await ctx.query(update).answer()


def noop_handlers() -> list[BaseHandler]:
    """Обработчик индикатора страницы — один на всё приложение."""
    return [CallbackQueryHandler(handle_noop, pattern=NOOP_PATTERN)]


__all__ = ["NOOP_PATTERN", "handle_noop", "noop_handlers"]
