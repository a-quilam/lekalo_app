# State — Poster

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
