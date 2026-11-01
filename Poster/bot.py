# bot.py
import asyncio
import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from config import TELEGRAM_BOT_TOKEN
from database import init_db
from errors import error_handler
from handlers.admin import admin_handlers
from handlers.announcement_wizard import announcement_wizard_handlers
from handlers.bricks import bricks_handlers
from handlers.main_menu import main_menu_handlers
from handlers.places import places_handlers
from handlers.templates import templates_handlers
from handlers.user_tracking import track_user
from log_config import setup_logging
from utils import tg_context as ctx
from utils.parsers import APP_TZ
from utils.ui_texts import tr

logger = logging.getLogger(__name__)

# Poster 2.4 P3: записи анонсов старше 30 суток удаляются автоматически.
# Задача идёт раз в час: чаще не нужно (срок — сутки), а редкий запуск не
# создаёт нагрузки. Ошибки задачи логируются и НЕ останавливают бота.
ANNOUNCEMENT_CLEANUP_INTERVAL_SECONDS = 3600


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Неизвестная команда — подсказываем /help."""
    await ctx.message(update).reply_text(
        tr("screen.unknown_command.neizvestnaya_komanda_naberite_help")
    )


def _cleanup_reference_now() -> datetime:
    """
    «Сейчас» для очистки: текущий момент по APP_TZ, приведённый к UTC.

    Колонки `created_at` хранят наивный UTC (`models.utcnow`), поэтому
    сравнение делается в той же шкале: часовой пояс приложения нужен только
    для согласованного «сегодня», а не для арифметики в SQLite.
    """
    return datetime.now(APP_TZ).astimezone(timezone.utc).replace(tzinfo=None)


async def run_announcement_cleanup() -> int:
    """
    Один прогон автоочистки анонсов (Poster 2.4 P3).

    Удаляет записи (черновики и готовые) с `created_at` старше 30 суток.
    Telegram-сообщения не трогаются: это очистка БД. Возвращает число
    удалённых записей; исключения пробрасываются вызывающему слою, который
    обязан их залогировать и продолжить работу.
    """
    from announcement_store import cleanup_old_announcements

    # Импорт на вызов ради актуальности зависимостей модуля очистки.
    # Оговорка честно: тестовый харнесс (tests/integration_harness.py)
    # патчит `SessionLocal` у consumer-модулей (handlers/*, utils/screen и
    # т.п.), но НЕ у `database` и не у `bot`, поэтому эта фоновая задача
    # на временную БД харнесса не переключается. В тестах она и не
    # запускается: `post_init` вызывает только `run_polling`/`run_webhook`
    # (`Application.initialize()` его не трогает), а харнесс вызывает
    # именно `initialize()`. Проверка самой очистки идёт доменной
    # функцией `cleanup_old_announcements` на временной БД.
    from database import SessionLocal

    with SessionLocal() as session:
        removed = cleanup_old_announcements(session, now=_cleanup_reference_now())
    if removed:
        logger.info("Автоочистка анонсов: удалено %s записей.", removed)
    return removed


async def _announcement_cleanup_loop() -> None:
    """
    Фоновый цикл очистки, когда APScheduler/JobQueue недоступны.

    Первый прогон — сразу при старте (записи могли устареть, пока бот был
    выключен), затем раз в час. Любая ошибка логируется и не завершает
    цикл: сбой очистки не должен ронять бота.
    """
    while True:
        try:
            await run_announcement_cleanup()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — задача очистки не роняет бота
            logger.exception("Автоочистка анонсов завершилась ошибкой.")
        await asyncio.sleep(ANNOUNCEMENT_CLEANUP_INTERVAL_SECONDS)


CLEANUP_TASK_KEY = "announcement_cleanup_task"


async def post_init_announcement_cleanup(application: Application) -> None:
    """
    Регистрирует автоочистку после инициализации приложения (P3).

    Основной путь — JobQueue с повтором раз в час. `python-telegram-bot`
    ставит JobQueue только вместе с APScheduler (extra `job-queue`), а в
    текущем окружении его нет: тогда очистка живёт собственной фоновой
    задачей. Задача не привязана к `Application.create_task`: та ждёт
    завершения всех задач в `Application.stop()`, а бесконечный цикл
    подвесил бы остановку бота. Дескриптор хранится в `bot_data` и
    отменяется в `post_shutdown`.
    """
    queue = getattr(application, "job_queue", None)
    if queue is not None:
        queue.run_repeating(
            _announcement_cleanup_job,
            interval=ANNOUNCEMENT_CLEANUP_INTERVAL_SECONDS,
            first=0,
            name="announcement_cleanup",
        )
        logger.info("Автоочистка анонсов: JobQueue, раз в час.")
        return
    application.bot_data[CLEANUP_TASK_KEY] = asyncio.create_task(
        _announcement_cleanup_loop(), name="announcement_cleanup"
    )
    logger.info("Автоочистка анонсов: фоновая задача, раз в час.")


async def post_shutdown_announcement_cleanup(application: Application) -> None:
    """Останавливает фоновую автоочистку при выключении приложения (P3)."""
    task = application.bot_data.pop(CLEANUP_TASK_KEY, None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001 — остановка не должна падать из-за задачи
        logger.exception("Автоочистка анонсов не остановилась корректно.")


async def _announcement_cleanup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обёртка очистки для JobQueue: ошибка логируется, планировщик живёт."""
    try:
        await run_announcement_cleanup()
    except Exception:  # noqa: BLE001 — задача очистки не роняет бота
        logger.exception("Автоочистка анонсов завершилась ошибкой.")


def build_application() -> Application:
    """
    Собирает приложение со ВСЕМИ обработчиками (без запуска — тестируемо).

    Порядок регистрации важен: ConversationHandler анонсов идёт ПЕРВЫМ.
    Так reply-кнопки главного меню, нажатые ВНУТРИ диалога, обрабатываются
    состояниями диалога (с его завершением), а не глобальным хендлером
    main_menu — состояние не «висит». Вне диалога ConversationHandler
    возвращает None, и update уходит дальше по цепочке (main_menu).
    Легаси-мастер и его обработчики (/create_post, static WIP) удалены в S5.
    """
    # TELEGRAM_BOT_TOKEN проверяется в config при импорте; здесь — ещё и
    # нарровинг для mypy (build_application типизирован, в отличие от старого main)
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан — см. .env.example")
    # job_queue(None): в окружении нет APScheduler (extra `job-queue` не
    # установлен), поэтому PTB не может собрать планировщик. Периодические
    # задачи P3 (автоочистка анонсов раз в час) регистрируются в `post_init`:
    # при доступном JobQueue — её повтором, иначе — собственной фоновой
    # задачей приложения. Оба пути не зависят от наличия APScheduler.
    application = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .job_queue(None)
        .post_init(post_init_announcement_cleanup)
        .post_shutdown(post_shutdown_announcement_cleanup)
        .build()
    )

    # Регистрация известных пользователей (Stage 20A): отдельная группа -1,
    # выполняется до сценариев, не отвечает в чат и не меняет порядок групп 0.
    application.add_handler(TypeHandler(Update, track_user), group=-1)

    # Адаптивный мастер анонсов Stage 27B — первым: у него собственные
    # entry-точка aw_start и команда /new_wizard. Явный вход завершает
    # посторонние диалоги, но не удаляет их сохранённые черновики.
    for handler in announcement_wizard_handlers():
        application.add_handler(handler)

    # Справочник мест: после мастера (не перехватывает его тексты) и до
    # глобальных обработчиков главного меню (иначе outside_dialog_hint съел
    # бы ввод в фазах поиска/создания места).
    for handler in places_handlers():
        application.add_handler(handler)

    # Административный кабинет: после «Мест», до главного меню. Текстовый
    # поиск получателя активен только в своей явной фазе.
    for handler in admin_handlers():
        application.add_handler(handler)

    # Личные кирпичики: после мастера/«Мест»/кабинета, но до глобального
    # обработчика текста главного меню. У него собственный префикс bk_.
    for handler in bricks_handlers():
        application.add_handler(handler)

    # Редактор шаблонов: после кирпичиков, но до главного меню. Префикс tp_.
    for handler in templates_handlers():
        application.add_handler(handler)

    # Обработчики главного меню
    for handler in main_menu_handlers():
        application.add_handler(handler)

    # Обработчик неизвестных команд
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    # Глобальный обработчик ошибок: лог + уведомление пользователя
    application.add_error_handler(error_handler)
    return application


def main() -> None:
    # Настройка логирования — только здесь (побочных эффектов при импорте
    # модуля bot.py нет, поэтому модуль можно импортировать в тестах)
    setup_logging()
    logger.info("Запуск бота.")

    init_db()
    application = build_application()

    # Фоновых задач, кроме автоочистки анонсов, нет: TTL-очистка черновиков
    # отключена (Stage 14) — черновики хранятся бессрочно, пока пользователь
    # не удалит их сам. Автоочистка анонсов P3 (записи старше 30 суток, раз
    # в час) регистрируется в `post_init`/`post_shutdown` приложения
    # (`build_application`) и здесь ничего не требует.

    # Явный allowed_updates обязателен: Telegram хранит последнюю подписку
    # между вызовами getUpdates («If not specified, the previous setting will
    # be used»). Без явного параметра PTB не включает его в запрос, и бот
    # наследует любую прошлую серверную настройку. Инцидент P0: подписка
    # «только callback_query» пережила перезапуск — сообщения (/start, ввод
    # полей) перестали доставляться при формально здоровом polling.
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
