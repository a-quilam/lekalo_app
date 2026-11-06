# about_rich.py
"""
Содержание экрана «О проекте» как Rich Message (Poster 2.5.4, §7).

«О проекте» — второй и последний Rich Message Poster (первый — справка):
раздел открывается кнопкой «ℹ️ О проекте» со справки и возвращается к ней
кнопкой «◀️ К помощи». Отправка живёт в `rich_message.py`, навигация и
клавиатуры — в `handlers/main_menu.py`; здесь только структура и тексты.

Почему содержание разбито так (тот же принцип, что у справки):

- короткие строки, которые владелец захочет поправить формулировкой —
  заголовок, введение, подсказка о свёрнутых разделах, сообщение о сбое и
  НАЗВАНИЯ разделов — берутся из каталога `config/ui_texts.yaml`
  (ключи `about.rich.*`);
- сами тексты разделов остаются константами модуля: они структурированы
  (абзацы, список, ссылки с подписью), а каталог — плоские строки; десятки
  ключей `about.rich.sectionN.paragraphM` сделали бы его нередактируемым.

Структура (§7): заголовок, введение, подсказка, разделитель и ШЕСТЬ
свёрнутых раскрываемых разделов — «Лекало», «Как всё начиналось», «Для
кого», «Открытый код», «Связаться», «Поддержать проект». `is_open` не
передаётся: по официальной схеме Bot API отсутствие поля и означает
«свёрнуто», раскрытие ничего не отправляет.

Ссылки кликабельные и с короткой подписью: адрес GitHub и телеграм-адреса
оформлены `RichTextUrl` (`type: "url"`), а не «голым» текстом — иначе
Telegram show бы длинный URL вместо понятной подписи. У «Поддержать проект»
намеренно НЕТ ни кнопки, ни ссылки на донаты: поддержка — участие, а не
платёж.

Все тексты статические: никакой генерации во время запроса (§50).
"""

from __future__ import annotations

from rich_message import (
    InputRichBlockDetails,
    InputRichBlockDivider,
    InputRichBlockList,
    InputRichBlockListItem,
    InputRichBlockParagraph,
    InputRichBlockSectionHeading,
    InputRichMessage,
    InputRichTextUrl,
    RichBlock,
    RichText,
)
from utils.ui_texts import tr

# —— адреса проекта ——————————————————————————————————————————————————
#
# Вынесены в константы: один адрес — одно место правки, тесты проверяют
# ровно те же значения, что уходят в сообщение.
GITHUB_URL = "https://github.com/a-quilam/samplePost_bot"
AUTHOR_URL = "https://t.me/a_quilam"
RANDOM_CULTURE_URL = "https://t.me/random_culture"

# Часть абзаца: обычный текст или ссылка с подписью. Отдельный тип (а не
# общий `RichText`) нужен, чтобы вложенный массив не появлялся случайно:
# абзац собирается из плоских частей.
TextPart = str | InputRichTextUrl

# —— разделы: (ключ названия, абзацы, пункты списка) ——————————————————
#
# Абзац — кортеж частей: одна строка или фраза со вставленной ссылкой.
# `RichTextUrl` внутри фразы остаётся кликабельной подписью. Пустой кортеж
# пунктов означает «раздел без списка».
_SECTIONS: tuple[tuple[str, tuple[tuple[TextPart, ...], ...], tuple[str, ...]], ...] = (
    (
        "about.rich.section1",
        (
            (
                "«Лекало» — рабочее имя Poster. Так мы называем шаблон, по "
                "которому собирается анонс: структуру описывают один раз, а "
                "дальше в неё только подставляют значения.",
            ),
            (
                "Из этого вырос весь бот: площадки, шаблоны, кирпичики и "
                "фотографии — части одного лекала, а не отдельные сервисы.",
            ),
        ),
        (),
    ),
    (
        "about.rich.section2",
        (
            (
                "Poster начался с ручной работы: анонсы собирались копированием "
                "текста и правкой дат, и на каждый уходило больше времени, чем "
                "на само событие.",
            ),
            (
                "Сначала появился шаблон с кодами полей, потом — конструктор, "
                "который спрашивает значения и сам собирает текст. Бот вырос из "
                "этого конструктора и развивается вместе с задачами площадок.",
            ),
        ),
        (),
    ),
    (
        "about.rich.section3",
        (("Poster рассчитан на тех, кто регулярно рассказывает о событиях.",),),
        (
            "Площадкам и домам культуры — один шаблон на все анонсы.",
            "Организаторам — быстрый анонс без правки дат и адресов вручную.",
            "Администраторам — общие шаблоны и кирпичики для своей команды.",
        ),
    ),
    (
        "about.rich.section4",
        (
            (
                "Проект открыт: код, миграции и тесты лежат в открытом "
                "репозитории. ",
                InputRichTextUrl(text="Открыть на GitHub", url=GITHUB_URL),
            ),
            (
                "Читать, проверять и предлагать правки можно без разрешения; "
                "идеи и замечания тоже приветствуются.",
            ),
        ),
        (),
    ),
    (
        "about.rich.section5",
        (
            (
                "Написать автору проекта: ",
                InputRichTextUrl(text="t.me/a_quilam", url=AUTHOR_URL),
            ),
            (
                "Проект вырос из «РанДом Культуры» — ",
                InputRichTextUrl(text="t.me/random_culture", url=RANDOM_CULTURE_URL),
                ": там мысль о лекале проверялась на реальных анонсах.",
            ),
        ),
        (),
    ),
    (
        "about.rich.section6",
        (
            (
                "Поддержать проект можно без денег: рассказать о нём площадке, "
                "которой он пригодится, прислать замечание или идею и проверить "
                "новую версию на своих анонсах.",
            ),
        ),
        (),
    ),
)


def _paragraph_text(parts: tuple[TextPart, ...]) -> RichText:
    """
    Содержимое абзаца: одна часть — она сама, несколько — массив `RichText`.

    Массив из одного элемента Bot API тоже принимает, но лишний уровень
    вложенности только запутывает контракт и тесты.
    """
    return parts[0] if len(parts) == 1 else parts


def build_about_rich_message() -> InputRichMessage:
    """
    Собирает экран «О проекте» способом `blocks` (§48).

    Структура: заголовок, введение, подсказка о свёрнутых разделах,
    разделитель и ровно шесть свёрнутых `InputRichBlockDetails` — те же
    блоки и тот же вид, что у справки, чтобы экраны не выглядели разными
    продуктами. Функция вызывается на каждое открытие и каждый раз собирает
    новую структуру: изменяемых общих объектов нет.
    """
    blocks: list[RichBlock] = [
        # 1 — самый крупный заголовок официальной схемы Bot API.
        InputRichBlockSectionHeading(text=tr("about.rich.title"), size=1),
        InputRichBlockParagraph(text=tr("about.rich.intro")),
        InputRichBlockParagraph(text=tr("about.rich.hint")),
        InputRichBlockDivider(),
    ]
    for title_key, paragraphs, items in _SECTIONS:
        content: list[RichBlock] = [
            InputRichBlockParagraph(text=_paragraph_text(paragraph))
            for paragraph in paragraphs
        ]
        if items:
            content.append(
                InputRichBlockList(
                    items=tuple(
                        InputRichBlockListItem(
                            blocks=(InputRichBlockParagraph(text=item),)
                        )
                        for item in items
                    )
                )
            )
        blocks.append(
            InputRichBlockDetails(
                summary=tr(title_key),
                # is_open не передаётся: отсутствие поля = свёрнуто (§7).
                blocks=tuple(content),
            )
        )
    return InputRichMessage(blocks=tuple(blocks))


def about_section_titles() -> tuple[str, ...]:
    """Названия шести разделов (в порядке экрана). Для проверок и тестов."""
    return tuple(tr(title_key) for title_key, _paragraphs, _items in _SECTIONS)
