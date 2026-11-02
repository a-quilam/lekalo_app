# State — Poster

## 2026-11-02 — T2+T3 выполнены, приёмка/ревизия пройдены, make check зелёный — релизный diff готов к коммитам

- T3 (Developer, 1 вызов, blocked по steps на финальной docstring-правке; менеджер
  перепроверил): меню 3 строки/4 кнопки (`handlers/main_menu.py`); пикер площадок —
  кнопка «📝 Создать по личному шаблону» и «◀️ Назад»→меню (`handlers/places.py`,
  `open_place_picker_with_page`); пикер личных шаблонов «◀️ Назад»→пикер площадок с
  сохранением pick_page (`handlers/templates.py`, callback `tp_pick_back`); строки —
  `buttons.main.create_place`→«📝 Создать анонс», help переписан под 4 кнопки,
  удалены 5 мёртвых ключей (dead-key тест снова зелёный); README приведён к новому меню.
- Тесты T3: tests/test_poster2_241_create_entry.py (11 сценариев a–h); обновлены
  menu/menu_tester/u4a_verify/u4b_*/stage5/27b/19b/r1 контракты. Целевые наборы — OK.
- Независимая приёмка T2+T3 (Tester, 1 вызов): 13/13 PASS, 56 собственных проверок OK,
  блокирующих дефектов нет. Наблюдения (не блокеры): plm_back сброс страницы (косметика),
  tp_pick_back из pick_search_results → страница 0 (в рамках «по возможности»),
  расхождение подписей в тексте задания vs факт («🧱 Предложения кирпичиков (N)»,
  «◀️ К списку мест» | «🏠 В меню» — фактические подписи); предсуществующий dead-end
  adm_place из brick_list — вне diff, follow-up.
- Reviewer релизного diff (1 вызов): BLOCKER нет; IMPORTANT — вакуумный регресс-тест
  `test_lost_rights_do_not_show_internal_error` (plm_invite → stale); исправлено
  (Developer, 1 вызов): триггер `adm_invite:`, pre-assert кнопки в карточке, усилен
  assert на общий fallback. tests.test_poster2_u4b_defects_tester → OK (4).
  MINOR принято: mid-surrogate decode (паритет с существующим entities_to_html),
  orchestra_protocol.md untracked — коммитится намеренно.
- Граница этапа: `make check PYTHON=.venv/bin/python` → Ran 2152 tests OK
  (lint ruff+black, mypy — в составе check), `python -m utils.ui_texts` → ok keys=1113.
- Коммиты: T1 → T2 → T3 → docs/чек-пойнт (выполняет менеджер); НЕ добавлять untracked
  бэкапы post_bot.db.backup-*/replaced-* и config/ui_texts.yaml.before-text-edit.bak.
- Далее релизный цикл: stop службы → make db-maintain-clean → верификация базы →
  start → polling → отчёт владельцу.

## 2026-11-02 — T1 выполнена (Developer → Tester → Reviewer), ожидает коммита вместе с T2/T3

- Суть: entities → каноническая грамматика Stage 24. Новый `entities_to_template_markup`
  в utils/tg_html.py (UTF-16-срезы, вложенность, порядок с равными offset — внешним
  слоем более длинная entity, text_link внутри форматирования); точка захвата —
  handlers/templates.py `_process_section_text` (единая для личных/стандартных,
  content/extended_content). Грамматика: `||спойлер||`, `` `код` ``, ``` ```pre``` ```,
  `tg://` в ссылках (парсер И рендер-компилятор — рассинхрон снят), дефисы в подписи
  сохранены (квирк удалён), явная форма `{подпись}(URL)` для ссылки в середине текста.
- [код] при entities-вводе остаётся кирпичиком (`_escape_template_text` keep_bricks);
  литеральные `[[`, `]`-края — согласованы с BRICK_PATTERN.
- Деградации (документированы): text_mention/custom_emoji/date_time → текст; URL со
  скобками/пробелами/backslash, mailto/ftp → подпись без ссылки (сохранение не падает).
- Дефекты Tester-а D1–D5 исправлены; Reviewer: BLOCKER tg:// снят, VERDICT ok.
- Тесты: tests/test_poster2_241_template_markup.py (56), tests/test_poster2_241_markup_acceptance.py
  (22, независимая приёмка) — OK; целевые наборы parser/compiler/u1b/h1/h2/stage25/r2 — OK.
- Вне задачи (MINOR, follow-up): лексер `[[a[b]]]` — поведение до диффа; смежность
  `[main]]x` в конвертере — привязка консервативно становится литералом.
- Коммит: после завершения T2/T3, тематический.

## 2026-11-02 — Этап «Poster 2.4.1: форматирование шаблонов, единая карточка, единый вход» (план)

Три задачи (все утверждения владельца — в задании 2.4.1, не дублирую):

- **T1 (HIGH)**: Telegram-форматирование шаблонов. Канон — грамматика Stage 24
  (template_parser.py). План: маркеры спойлера `||`, code `` ` ``, pre ```` ``` ````
  в грамматике; `tg://` URL в _normalize_url; удаление дефис-квирка подписи
  (противоречит требованию сохранения подписи text_link; недокументирован);
  конвертер entities→грамматика (utils/tg_html.py, UTF-16-срезы; при наличии
  entities литеральные маркеры экранируются, без entities — текст как есть —
  прежнее поведение); точка захвата — handlers/templates.py `_process_section_text`
  (единая для content/extended/личных/стандартных). Анонсовый путь (capture_message_text)
  не трогается. Конвейер: Developer → Tester → Reviewer.
- **T2 (MEDIUM)**: `plm_open` для управляющих → сразу полная карточка (admin
  phase="place", доступ superadmin ИЛИ can_manage_place); ссылка-инвайт переносится
  в полную карточку (админ-сессия, переиспользование `_invite_content`/ensure_invite);
  plm_edit/plm_invite/plm_invite_regen/plm_admins уходят; manage_card остаётся только
  не-управляющим (просмотр + заявка). «К списку мест» — по фактическому маршруту.
- **T3 (MEDIUM)**: главное меню 3 строки: «📝 Создать анонс» (callback pick_place
  остаётся рабочим для старых сообщений), «📂 Мои анонсы», «⚙️|❓». Экран выбора
  площадки + кнопка «📝 Создать по личному шаблону» + «◀️ Назад»→меню. Из пикера
  личных шаблонов «◀️ Назад» → выбор площадок (страница сохраняется по возможности).
  Все строки — через ui_texts.yaml + DEFAULTS.

Порядок: T1 → T2 → T3 (последовательно: places.py пересекается в T2/T3), затем
Tester-приёмка, Reviewer релизного diff, make check, коммиты/push, CI, релизный
цикл (stop → db-maintain-clean → проверки → start → polling).

Начало этапа: 2026-11-02. База: HEAD cf569d7, рабочее дерево чистое (untracked —
бэкапы БД и orchestra_protocol.md, владельцу не мешают).

## 2026-11-01 — Этап «довести очистку тестовой БД до утверждённого контракта» (Build, done)

- Постановка: db_maintenance.py удалил 37 строк, но users/places/templates/versions/bricks
  удержаны RESTRICT и триггерами. Требование владельца: после релиза до первого
  публичного выпуска — полная очистка тестовых данных (площадки, пользователи,
  личные/локальные кирпичики, шаблоны, предложения, админы, анонсы); сохранить
  актуальную схему, системные кирпичики, встроенный стандарт, настройки, суперадмина
  (конфигурационного, SUPERADMIN_USER_IDS в .env).
- Права: Build/developer — `make db-maintain-*` запрещены. poster-manager — allow на
  db-maintain-report/clean (в т.ч. к рабочей базе). poster-tester — allow только с
  `DB=/tmp/*`. Отчёт по рабочей БД (задание 1) — через субагента poster-manager
  (read-only фазы 1–2 make-цели db-maintain-report).
- Найденная причина неполноты: построчное DELETE не может убрать строки-якоря
  неизменяемых журналов (brick_proposals/template_snapshots → users/places FK RESTRICT,
  триггеры no_delete). Нужна архитектура «бэкап → bootstrap новой БД → верификация →
  атомарная замена» (задание 4) вместо вычёсывания.
- Каталог системных кирпичиков: SYSTEM_BRICKS в brick_types.py (10 определений после
  P1: id 1–5, 7–11; id 6 легаси устранён миграцией 25). Количество НЕ зашивать:
  верификация сверяется с brick_types.SYSTEM_BRICKS.
- Миграции: DEFAULT_MIGRATIONS, LATEST_VERSION = 25 (migrations.py).
- Стандартный шаблон места создаётся вместе с местом (place_store.create_place →
  _insert_standard_template, BUILTIN_TEMPLATE v4).
- План: db_maintenance.py + режим rebuild (фазы 1–2 те же, затем bootstrap новой базы
  штатным init_db на новом файле, верификация схемы/каталога/нулевых счётчиков,
  атомарный replace при остановленной службе); тест tests/test_db_maintenance_rebuild.py.
- Рабочая база в этой задаче не изменяется (задание 9); механизм проверяется на
  /tmp/opencode базах.
- Итог: db_maintenance.py переписан на rebuild (фазы 1–2 report прежние; clean =
  служба остановлена → bootstrap init_db в staging-файл → верификация 1..25 +
  каталог кирпичиков + нулевые счётчики → wal_checkpoint → атомарный replace,
  прежняя БД сохраняется как `<база>.replaced-<метка>`). Makefile: `REAL_DB` —
  дефолт цели при отсутствии DB=. Тест tests/test_db_maintenance_rebuild.py (8
  кейсов, временная БД): зелёный; полный make check — 2049 тестов OK. Lint/
  black/mypy по изменённым файлам — чисто. Рабочая БД НЕ очищена.
- Команда менеджера для релизной очистки: из каталога Poster при остановленном
  боте (`./botctl.sh stop`) — `make db-maintain-clean PYTHON=.venv/bin/python`;
  подтверждение результата: `user_counts_after` — все нули, кроме
  `bricks = каталог кода`, `integrity_after: ok`, путь `replaced_to`.

## 2026-11-01 — Релиз 2.4 завершён: rebuild-очистка рабочей БД выполнена (Manager)

- Коммит Build-механизма: `651bd8f` (db_maintenance.py rebuild-архитектура,
  Makefile REAL_DB, tests/test_db_maintenance_rebuild.py 7 тестов, state.md,
  opencode.jsonc — allow db-maintain-report для build) — pushed в origin/main.
  Целевой тест механизма зелёный (unittest tests.test_db_maintenance_rebuild).
- Релизная очистка рабочей базы (утверждена владельцем, протокол п. 11):
  служба остановлена штатно (inactive, PID-файла нет) → `make db-maintain-clean`
  → полный rebuild: бэкап
  `Poster/post_bot.db.backup-20261101T140954950730Z-p719644` (проверен),
  bootstrap новой базы, верификация (миграции 1..25, системные кирпичики ровно
  каталог brick_types — 10 кодов, все пользовательские счётчики нулевые),
  атомарная замена. Прежняя база сохранена:
  `post_bot.db.replaced-20261101T140955345908Z`. Контракт выполнен полностью:
  в базе не осталось ни одной тестовой записи (users/places/templates/
  versions/snapshots/proposals/анонсы/состояния — 0), единственная ненулевая
  таблица `bricks` = 10 системных. Независимая пост-проверка: integrity ok,
  foreign_key_check пуст, миграции 1..25, sys_codes = каталог.
- Суперадмин: конфигурационный (.env), rebuild файл конфигурации не читает и
  не изменяет; подтверждено косвенно — бот стартовал без ConfigError.
- Служба запущена: active/running, NRestarts=0, «Запуск бота» — 1 (нет
  restart-loop), Traceback/ERROR 0 за 2+ минуты наблюдения, polling жив
  (63 ESTAB), конфликтов нет, автоочистка анонсов зарегистрирована.
  Telegram-сообщения механизм не трогает (Telegram API не вызывает).
- Незакоммиченный остаток (намеренно): ui_texts.yaml.before-text-edit.bak,
  untracked orchestra_protocol.md, 5 бэкапов + replaced-файл прежней базы
  (в .gitignore, архивировать при следующей операции с базой).
- **Poster 2.4: разработка, приёмка, коммиты, очистка БД, деплой — завершены.**
  Живая визуальная приёмка в реальном Telegram — за владельцем.
