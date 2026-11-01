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
