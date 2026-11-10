# tests/ui_texts_expect.py
"""
Сверка экранов со строками ДЕЙСТВУЮЩЕГО каталога `config/ui_texts.yaml`.

Тесты не являются контрактом формулировок: владелец правит YAML, и дословный
литерал в тесте перестаёт совпадать с экраном (Poster 2.5.5, задача 2).
Поэтому ожидание задаётся КЛЮЧОМ каталога, а не строкой:

- `text("menu.main")` — точная строка каталога (для сравнения экрана целиком);
- `shows(screen, "wizard.question.header")` — строка каталога есть на экране,
  подстановки `{title}`, `{step}`, `{total}` при этом любые.

Смысл проверок не меняется: «заголовок шага виден» остаётся проверкой
«заголовок шага виден» — просто источник строки теперь каталог, а не
зашитый в тест текст.
"""

from __future__ import annotations

import re
import unittest

from utils import ui_texts


def text(key: str, **values: object) -> str:
    """Готовая строка каталога — ровно то, что увидит пользователь."""
    return ui_texts.tr(key, **values)


def _template(key: str) -> str:
    catalog = ui_texts.texts()
    if key in catalog:
        return catalog[key]
    # Ключа нет в файле (или файл неполный) — действует вшитый дефолт.
    return ui_texts.DEFAULTS[key]


def fragment(key: str) -> str:
    """
    Литеральная часть строки каталога ДО первой подстановки.

    Для проверок вида «на экране есть этот блок» подходит именно она: значения
    `{canonical_name}`, `{count}` и т.п. тест знать не обязан, а каталог —
    единственный источник формулировки. Если подстановок нет, возвращается
    вся строка целиком.
    """
    return _template(key).split("{", 1)[0]


def full(key: str, **values: object) -> str:
    """Строка каталога, собранная как на экране (подстановки подставлены)."""
    return text(key, **values)


def pattern(key: str, **values: object) -> re.Pattern[str]:
    """
    Регулярка строки каталога: каждая подстановка `{name}` — любые символы.

    Литеральная часть экранируется, поэтому спецсимволы каталога (`·`, `«»`,
    точки) не превращаются в шаблон. Переданные `values` фиксируют конкретные
    подстановки (`step=2, total=2`), остальные остаются `.*?`.
    """
    chunks = ui_texts.PLACEHOLDER_RE.split(_template(key))
    parts: list[str] = []
    for index, chunk in enumerate(chunks):
        if index % 2 == 0:
            parts.append(re.escape(chunk))
            continue
        if chunk in values:
            parts.append(re.escape(str(values[chunk])))
        else:
            parts.append(".*?")
    return re.compile("".join(parts), re.S)


def question_re(step: int, total: int | None = None) -> re.Pattern[str]:
    """
    Регулярка заголовка вопроса: шаг (и, если задан, общий счётчик).

    Заголовок — одна строка каталога `wizard.question.header` с подстановками
    `{title}`, `{step}`, `{total}`. Название кирпичика тест знать не обязан,
    поэтому `{title}` — любые символы, а шаг/всего фиксируются числом:
    проверка «мы на вопросе 2 из 2» остаётся проверкой шага.
    """
    chunks = ui_texts.PLACEHOLDER_RE.split(_template("wizard.question.header"))
    parts: list[str] = []
    for index, chunk in enumerate(chunks):
        if index % 2 == 0:
            parts.append(re.escape(chunk))
            continue
        if chunk == "step":
            parts.append(str(step))
        elif chunk == "total" and total is not None:
            parts.append(str(total))
        else:
            parts.append(".+?")
    return re.compile("".join(parts), re.S)


def assert_question(
    case: unittest.TestCase,
    screen: str,
    step: int,
    total: int | None = None,
    msg: str | None = None,
) -> None:
    """Требует, чтобы экран был вопросом указанного шага."""
    case.assertRegex(
        screen or "",
        question_re(step, total),
        msg or f"нет заголовка вопроса {step}/{total or '?'}: {screen[:200]!r}",
    )


# Ключи короткой причины «текст не помещается»: шаг фото/черновик и правка
# уже доставленного анонса. Смысл проверки один — отказ по длине показан,
# поэтому тест не привязан к одной формулировке.
LENGTH_REFUSAL_KEYS = (
    "wizard.length_refusal.not_pomeschaetsya",
    "wizard.length_refusal.not_pomeschaetsya_bez_foto",
    "wizard.length_refusal.edit_not_applied",
)


def length_refusal(screen: str, limit: int | None = None) -> bool:
    """Есть ли на экране штатный отказ по длине (по желанию — с лимитом)."""
    values = {} if limit is None else {"limit": limit}
    return any(shows(screen or "", key, **values) for key in LENGTH_REFUSAL_KEYS)


def assert_length_refusal(
    case: unittest.TestCase,
    screen: str,
    limit: int | None = None,
    msg: str | None = None,
) -> None:
    """Требует штатный отказ по длине текста (ключи `wizard.length_refusal.*`)."""
    case.assertTrue(
        length_refusal(screen, limit),
        msg
        or (
            f"нет короткой ошибки о длине текста"
            f"{'' if limit is None else f' (лимит {limit})'}: {screen[:200]!r}"
        ),
    )


# Ключи, объясняющие пользователю, что кнопка устарела: у каждого раздела
# свой текст, но смысл проверки везде один — «кнопка устарела, раздел откройте
# заново». Поэтому проверка идёт по любому из них, а не по литералу.
STALE_KEYS = (
    "errors.stale.wizard",
    "errors.expired.wizard",
    "errors.stale.places",
    "errors.stale.pick",
    "errors.stale.manage",
    "errors.stale.bricks",
    "errors.stale.admin",
    "errors.stale.templates",
    "errors.stale.instance",
)


def stale_texts() -> list[str]:
    """Все варианты каталога «кнопка устарела» (в нижнем регистре)."""
    return [_template(key).lower() for key in STALE_KEYS]


def is_stale(text: str) -> bool:
    """Похоже ли сообщение на штатное объяснение устаревшей кнопки."""
    lowered = (text or "").lower()
    return any(variant and variant in lowered for variant in stale_texts())


def assert_stale(case: unittest.TestCase, text: str, msg: str | None = None) -> None:
    """Требует штатное объяснение устаревшей кнопки (любой раздел)."""
    case.assertTrue(
        is_stale(text),
        msg or f"нет объяснения устаревшей кнопки: {text[:200]!r}",
    )


def assert_not_stale(
    case: unittest.TestCase, text: str, msg: str | None = None
) -> None:
    """Требует ОТСУТСТВИЯ объяснения «кнопка устарела» в ответе."""
    case.assertFalse(
        is_stale(text),
        msg or f"ложное объяснение устаревшей кнопки: {text[:200]!r}",
    )


def is_empty_block(key: str) -> bool:
    """Пустой ли блок каталога (владелец очистил формулировку)."""
    return _template(key).strip() == ""


def assert_no_empty_block(
    case: unittest.TestCase,
    screen: str,
    key: str,
    msg: str | None = None,
) -> None:
    """
    Требует, чтобы пустой блок каталога не оставлял на экране дыры.

    `wizard.question.missing`, `wizard.extras.intro`
    в каталоге пусты: сборка экрана обязана пропускать такой блок целиком, а
    не вставлять пустую строку-разделитель. Поэтому проверяются два условия:
    ключ действительно пуст и на экране нет трёх переводов строки подряд
    (двойной пустой строки).
    """
    case.assertTrue(is_empty_block(key), msg or f"блок каталога {key!r} не пуст")
    case.assertNotIn(
        "\n\n\n",
        screen or "",
        msg or f"пустой блок {key!r} оставил пустую строку-разделитель",
    )


def shows(screen: str, key: str, **values: object) -> bool:
    """Есть ли на экране строка каталога `key` (подстановки — любые/заданные)."""
    return bool(pattern(key, **values).search(screen or ""))


def assert_shows(
    case: unittest.TestCase,
    screen: str,
    key: str,
    msg: str | None = None,
    **values: object,
) -> None:
    """Требует, чтобы экран показывал строку каталога `key`."""
    case.assertRegex(
        screen or "", pattern(key, **values), msg or f"нет строки каталога {key!r}"
    )


def assert_not_shows(
    case: unittest.TestCase,
    screen: str,
    key: str,
    msg: str | None = None,
    **values: object,
) -> None:
    """Требует, чтобы строки каталога `key` на экране не было."""
    case.assertNotRegex(
        screen or "",
        pattern(key, **values),
        msg or f"лишняя строка каталога {key!r}",
    )
