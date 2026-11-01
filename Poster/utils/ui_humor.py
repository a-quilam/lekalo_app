# utils/ui_humor.py
"""
Poster 2.0 S5 §5.7: редкие юмористические вариации интерфейса.

Модуль чистый: без Telegram-типов, без сети, без вызовов LLM и без
побочных эффектов на данные. Здесь только заранее написанные фразы и две
проверки — «разрешён ли шутливый экран» и «выпала ли вариация».

Poster 2.4 (P4): фразы вынесены в редактируемый каталог
`config/humor_phrases.yaml` (ключ `humor.phrases` — список строк). Это
отдельный файл, не `ui_texts.yaml`: юмор не смешивается с обязательными
текстами экранов. Механика не изменилась — ни частота 1%, ни белый список,
ни флаг, ни запрет на попадание шутки в анонс/ошибку/алерт.

Устройство каталога:

- `DEFAULT_JOKES` — вшитые фразы. Это страховка: при отсутствующем, битом
  или пустом YAML модуль работает на них, а не падает;
- файл читается один раз при импорте (`reload_phrases()` перечитывает
  явно). Любая проблема с файлом — WARNING в лог, не исключение;
- `HUMOR_JOKES` — действующий набор фраз (из файла или вшитый);
- проверка файла вручную: `.venv/bin/python -m utils.ui_humor`.

Правила (спека §5.7):

- частота ≈1% подходящих показов, проверка по хэшу ключа, а не `random()`:
  повторная отрисовка того же экрана даёт ту же формулировку;
- меняется **только** текст вопроса или подпись кнопки — вызывающий слой
  подставляет строку после формирования `callback_data` и клавиатуры;
- белый список экранов: ошибки, права, лимиты и предупреждения о потере
  данных не шутят никогда;
- флаг `UI_HUMOR_ENABLED` (config.py) главнее любого сценария: при
  выключенном юморе и `pick_variant`, и шутка пустого анонса возвращают
  `None`;
- шутка пустого анонса — из этого же набора, показывается всегда
  (без 1%-фильтра), но подчиняется флагу.

В готовый анонс (live-блок и `publication_text`) шутка не попадает: рендер
поста сюда не обращается, а `get_humor_line` вызывается только из нижнего
служебного экрана.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# ── Набор шуток ────────────────────────────────────────────────────────────
# Вшитые фразы (страховка). Файл каталога их повторяет; при любой проблеме
# с файлом бот работает именно на них.
DEFAULT_JOKES: tuple[str, ...] = (
    "Анонс пока пуст — как сцена до саундчека. Заполните поле или отправьте фото.",
    "Пусто, но интригующе. Добавьте хотя бы одно слово — и это уже анонс.",
    "Пока это самый лаконичный анонс в истории. Разбавим?",
    "Поле ждёт вас, как зритель у кассы. Одно слово — и представление начнётся.",
    "Не спешите: даже у великих анонсов сначала была пустая строка.",
    "Пока здесь тишина. Хороший повод сказать что-то важное.",
    "Без паники: пустое поле — это ещё не провал, а пауза перед выходом.",
    "Коротко и по делу — тоже вариант. Но одно слово всё-таки добавьте.",
)

# Путь по умолчанию: Poster/config/humor_phrases.yaml (рядом с config.py).
DEFAULT_PHRASES_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "humor_phrases.yaml"
)

# Переменная окружения для тестов и нестандартных развёртываний.
PATH_ENV_VARIABLE = "HUMOR_PHRASES_PATH"

# Ключ списка фраз в файле. Вложенная запись (`humor: phrases:`) и плоская
# (`"humor.phrases":`) равнозначны — разбор общий с каталогом текстов.
PHRASES_KEY = "humor.phrases"

# Фраза показывается в обычном тексте Telegram: она не шаблон, поэтому
# переменных в ней быть не может, а слишком длинная строка превратила бы
# подсказку в абзац.
MAX_PHRASE_LENGTH = 300

# Текст экрана уходит в Telegram с parse_mode=HTML, поэтому символы разметки
# в фразе недопустимы: даже при экранировании на стороне рендера каталог
# должен оставаться простым текстом (защита в глубину — см. D1).
FORBIDDEN_PHRASE_CHARS = ("<", ">", "&")

# ── Белый список экранов ───────────────────────────────────────────────────
# Шутить можно только в тексте вопроса и в подписи кнопки.
SCREEN_QUESTION = "question"
SCREEN_BUTTON = "button"

# Запрещённые виды экранов перечислены явно: это не «всё остальное», а
# конкретные продуктовые границы из спеки.
SCREEN_ERROR = "error"
SCREEN_RIGHTS = "rights"
SCREEN_LIMITS = "limits"
SCREEN_DATA_LOSS = "data_loss"

ALLOWED_SCREEN_KINDS = frozenset({SCREEN_QUESTION, SCREEN_BUTTON})
FORBIDDEN_SCREEN_KINDS = frozenset(
    {SCREEN_ERROR, SCREEN_RIGHTS, SCREEN_LIMITS, SCREEN_DATA_LOSS}
)

# Ключ шутки пустого анонса. Постоянный: экран пустого анонса перерисовывается
# при каждом шаге и не должен «прыгать» между фразами.
EMPTY_ANNOUNCEMENT_KEY = "empty-announcement"

# ≈1%: выпадает только нулевой остаток от 100. Отдельная соль, чтобы бросок
# не был связан с выбором конкретной фразы.
HUMOR_RATE = 100

# Действующий набор фраз. Заполняется при импорте (`_initial_load`) и явно —
# `reload_phrases()`. Имя сохранено ради существующих вызывающих тестов.
HUMOR_JOKES: tuple[str, ...] = DEFAULT_JOKES

# Совместимость с S3: прежнее имя набора для шутки пустого анонса.
EMPTY_ANNOUNCEMENT_JOKES = HUMOR_JOKES


@dataclass
class HumorReport:
    """Итог чтения и проверки YAML-каталога фраз (для логов и CLI)."""

    path: Path
    ok: bool
    used_defaults: bool
    phrases: int
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def summary(self) -> str:
        state = "ok" if self.ok else "invalid"
        source = "defaults" if self.used_defaults else "yaml"
        return (
            f"humor_phrases: {state}, source={source}, phrases={self.phrases}, "
            f"errors={len(self.errors)}, warnings={len(self.warnings)}, "
            f"path={self.path}"
        )


def phrases_path() -> Path:
    """Путь к каталогу фраз: `HUMOR_PHRASES_PATH` или `config/humor_phrases.yaml`."""
    raw = os.getenv(PATH_ENV_VARIABLE)
    if raw and raw.strip():
        return Path(raw.strip()).expanduser()
    return DEFAULT_PHRASES_PATH


def _flatten(data: Any, prefix: str = "") -> dict[str, Any]:
    """
    Плоский вид YAML: вложенные разделы → ключи через точку.

    Редактору проще искать `humor.phrases` строкой, но вложенная запись
    тоже принимается. Не-словари на верхнем уровне отбрасываются — это
    ошибка формата, её ловит вызывающая проверка.
    """
    flat: dict[str, Any] = {}
    if not isinstance(data, dict):
        return flat
    for key, value in data.items():
        name = str(key) if prefix == "" else f"{prefix}.{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, name))
        else:
            flat[name] = value
    return flat


def validate_phrases(
    data: Any, *, path: Path | None = None
) -> tuple[tuple[str, ...], HumorReport]:
    """
    Проверяет разобранный YAML и возвращает (фразы, отчёт).

    Ошибка формата, отсутствие ключа, пустой список или непригодная фраза →
    `ok=False` и пустой набор: вызывающий слой берёт вшитые дефолты целиком,
    а не смесь файла и дефолтов.
    """
    report_path = path or phrases_path()
    if not isinstance(data, dict):
        return (), HumorReport(
            path=report_path,
            ok=False,
            used_defaults=True,
            phrases=len(DEFAULT_JOKES),
            errors=("корень файла должен быть словарём",),
        )
    flat = _flatten(data)
    if PHRASES_KEY not in flat:
        return (), HumorReport(
            path=report_path,
            ok=False,
            used_defaults=True,
            phrases=len(DEFAULT_JOKES),
            errors=(f"не найден ключ {PHRASES_KEY}",),
        )
    raw = flat[PHRASES_KEY]
    if not isinstance(raw, list):
        return (), HumorReport(
            path=report_path,
            ok=False,
            used_defaults=True,
            phrases=len(DEFAULT_JOKES),
            errors=(f"{PHRASES_KEY}: значение должно быть списком строк",),
        )
    if not raw:
        return (), HumorReport(
            path=report_path,
            ok=False,
            used_defaults=True,
            phrases=len(DEFAULT_JOKES),
            errors=(f"{PHRASES_KEY}: список фраз пуст",),
        )
    errors: list[str] = []
    warnings: list[str] = []
    phrases: list[str] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, str):
            errors.append(f"{PHRASES_KEY}[{index}]: фраза должна быть строкой")
            continue
        phrase = item.strip()
        if not phrase:
            errors.append(f"{PHRASES_KEY}[{index}]: пустая фраза")
            continue
        if len(phrase) > MAX_PHRASE_LENGTH:
            errors.append(
                f"{PHRASES_KEY}[{index}]: фраза длиннее {MAX_PHRASE_LENGTH} "
                f"символов ({len(phrase)})"
            )
            continue
        if "{" in phrase or "}" in phrase:
            # Шутка — не шаблон: подстановки в ней нет, скобки попали бы
            # в текст как есть.
            errors.append(
                f"{PHRASES_KEY}[{index}]: фраза не может содержать {{переменные}}"
            )
            continue
        forbidden = [char for char in FORBIDDEN_PHRASE_CHARS if char in phrase]
        if forbidden:
            # Фраза — обычный текст, а экран отправляется с parse_mode=HTML.
            # Даже при экранировании на стороне рендера такой каталог не
            # применяется: символы разметки в файле — ошибка владельца.
            errors.append(
                f"{PHRASES_KEY}[{index}]: фраза содержит символы разметки "
                f"({' '.join(forbidden)}) — используйте обычный текст; "
                "файл будет отклонён"
            )
            continue
        if phrase in phrases:
            warnings.append(f"{PHRASES_KEY}[{index}]: повтор фразы — пропущен")
            continue
        phrases.append(phrase)
    if errors:
        return (), HumorReport(
            path=report_path,
            ok=False,
            used_defaults=True,
            phrases=len(DEFAULT_JOKES),
            errors=tuple(errors),
            warnings=tuple(warnings),
        )
    return tuple(phrases), HumorReport(
        path=report_path,
        ok=True,
        used_defaults=False,
        phrases=len(phrases),
        warnings=tuple(warnings),
    )


def _fallback(path: Path, error: str) -> HumorReport:
    """Отчёт «файл не применён»: вшитые дефолты и одна причина."""
    return HumorReport(
        path=path,
        ok=False,
        used_defaults=True,
        phrases=len(DEFAULT_JOKES),
        errors=(error,),
    )


def _load(path: Path) -> tuple[tuple[str, ...], HumorReport]:
    """Читает файл и проверяет его; любая проблема → дефолты и отчёт."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return DEFAULT_JOKES, _fallback(path, "файл каталога фраз не найден")
    except UnicodeDecodeError as exc:
        # Файл сохранён не в UTF-8 (типичный «ANSI» редактора) или обрезан
        # посередине символа. Импорт модуля не должен падать: встроенные
        # дефолты + предупреждение в лог.
        return DEFAULT_JOKES, _fallback(
            path,
            "не удалось прочитать файл каталога фраз: кодировка должна быть "
            f"UTF-8 (байт {exc.start}: {exc.reason})",
        )
    except OSError as exc:
        return DEFAULT_JOKES, _fallback(path, f"файл каталога фраз не читается: {exc}")
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        return DEFAULT_JOKES, _fallback(path, f"синтаксис YAML: {exc}")
    phrases, report = validate_phrases(data, path=path)
    if not report.ok:
        return DEFAULT_JOKES, report
    return phrases, report


_last_report: HumorReport | None = None


def _apply(phrases: tuple[str, ...], report: HumorReport) -> HumorReport:
    """Публикует действующий набор фраз и отчёт; логирует результат."""
    global HUMOR_JOKES, EMPTY_ANNOUNCEMENT_JOKES, _last_report
    HUMOR_JOKES = phrases
    EMPTY_ANNOUNCEMENT_JOKES = phrases
    _last_report = report
    if report.ok:
        logger.info("Каталог фраз юмора загружен: %s", report.summary())
    else:
        # WARNING, а не CRITICAL: юмор — необязательная часть интерфейса,
        # бот работает на вшитых фразах и остаётся полностью рабочим.
        logger.warning(
            "Каталог фраз юмора не применён (%s) — используются вшитые фразы. %s",
            report.path,
            "; ".join(report.errors),
        )
    for warning in report.warnings:
        logger.warning("ui_humor: %s", warning)
    return report


def _initial_load() -> None:
    """Загрузка при импорте: битый файл не должен ломать старт бота."""
    phrases, report = _load(phrases_path())
    _apply(phrases, report)


def reload_phrases(path: Path | str | None = None) -> HumorReport:
    """
    Перечитывает каталог фраз (явная перезагрузка, без слежения за файлом).

    `path` — для тестов и нестандартного развёртывания. Возвращает отчёт
    проверки; при ошибке набор фраз целиком заменяется вшитыми дефолтами.
    """
    target = Path(path) if path is not None else phrases_path()
    phrases, report = _load(target)
    return _apply(phrases, report)


def last_report() -> HumorReport:
    """Отчёт последней загрузки (для диагностики и CLI)."""
    if _last_report is None:
        return HumorReport(
            path=phrases_path(),
            ok=False,
            used_defaults=True,
            phrases=len(DEFAULT_JOKES),
            errors=("каталог ещё не загружен",),
        )
    return _last_report


def humor_allowed(screen_kind: str) -> bool:
    """
    Разрешён ли шутливый текст на экране этого вида.

    Проверка только по белому списку; флаг отключения учитывает
    `pick_variant`. Неизвестный/запрещённый вид экрана — не шутим.
    """
    return screen_kind in ALLOWED_SCREEN_KINDS


def humor_enabled() -> bool:
    """
    Включён ли юмор глобально.

    Читает `config.UI_HUMOR_ENABLED` при каждом вызове, поэтому флаг можно
    менять в тестах без переимпорта модуля. Значение по умолчанию — включён.
    """
    from config import UI_HUMOR_ENABLED

    return bool(UI_HUMOR_ENABLED)


def _digest(key: str) -> int:
    """Стабильный 64-битный отпечаток ключа: одинаков на любом процессе."""
    return int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:8], "big")


def pick_variant(
    key: str,
    *,
    screen_kind: str = SCREEN_QUESTION,
    forced: bool = False,
) -> str | None:
    """
    Детерминированная шутливая вариация для ключа экрана — или `None`.

    Ключ обязан состоять только из стабильных идентификаторов
    (`user_id` + `draft_id` + `stage`/`brick_id`), без счётчиков сообщений и
    времени: тогда повторная отрисовка того же экрана даёт ту же строку.

    `forced=True` — обязательный показ (пустой анонс): 1%-фильтр не
    применяется, но флаг отключения и белый список экранов действуют.
    """
    if not key:
        return None
    if not humor_allowed(screen_kind):
        return None
    if not humor_enabled():
        return None
    if not HUMOR_JOKES:
        # Пустой набор невозможен (дефолты непусты), но деление на ноль
        # здесь недопустимо ни при каком состоянии каталога.
        return None
    if not forced and _digest(f"rate|{key}") % HUMOR_RATE != 0:
        return None
    return HUMOR_JOKES[_digest(f"joke|{key}") % len(HUMOR_JOKES)]


def empty_announcement_joke(key: str = EMPTY_ANNOUNCEMENT_KEY) -> str | None:
    """
    Шутка для служебного экрана пустого анонса (обязательный показ).

    Берётся из общего набора, показывает всегда — без 1%-фильтра, но
    подчиняется флагу `UI_HUMOR_ENABLED`.
    """
    return pick_variant(key, screen_kind=SCREEN_QUESTION, forced=True)


def get_humor_line(
    *,
    announcement_is_empty: bool,
    key: str = EMPTY_ANNOUNCEMENT_KEY,
) -> str | None:
    """
    Строка шутки для нижнего служебного экрана — или `None`.

    Единственная точка вызова из UI: решает только «пусто/не пусто» и
    включён ли юмор. В live-блок и в отправленный анонс эта функция не
    вызывается, поэтому шутка физически не может попасть в пост.
    """
    if not announcement_is_empty:
        return None
    return empty_announcement_joke(key)


_initial_load()


def main() -> int:
    """
    CLI-проверка каталога фраз: `.venv/bin/python -m utils.ui_humor`.

    Код возврата 0 — файл валиден, 1 — есть ошибки (подробности в stdout).
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    report = reload_phrases()
    print(report.summary())
    for error in report.errors:
        print(f"ERROR: {error}")
    for warning in report.warnings:
        print(f"WARNING: {warning}")
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover — ручная проверка файла
    raise SystemExit(main())
