# Проверки проекта Poster. Выполнять из каталога Poster:
#   make check   — все проверки подряд (линтинг, типы, тесты)
#   make accept  — релизная приёмка (= check: стиль, типы, unit- и
#                  интеграционные тесты, критические сценарии экрана,
#                  smoke-запуск, миграции; без живого Telegram)
#   make format  — автоформатирование (black + автоисправления ruff)
#
# PYTHON указывает на интерпретатор окружения с установленными
# requirements.txt и requirements-dev.txt.

PYTHON ?= python

.PHONY: check accept lint types test format run db-maintain-report db-maintain-clean

check: lint types test

accept: check

# Штатное обслуживание базы после релиза (Poster/db_maintenance.py):
#   make db-maintain-report          — целостность + проверенный WAL-бэкап
#   make db-maintain-clean           — полный цикл: бэкап → очистка → smoke
#   make db-maintain-clean DB=path   — полный цикл на указанной базе
# Очистка невозможна без успешного проверенного бэкапа этой же базы
# (контроль внутри db_maintenance.py). Рабочая база очищается только
# утверждённой владельцем релизной операцией.
db-maintain-report:
	$(PYTHON) db_maintenance.py --db "$(DB)" --skip-clean

db-maintain-clean:
	$(PYTHON) db_maintenance.py --db "$(DB)"

lint:
	$(PYTHON) -m ruff check .
	$(PYTHON) -m black --check .

types:
	$(PYTHON) -m mypy .

test:
	$(PYTHON) -W ignore::DeprecationWarning -m unittest discover -s tests

format:
	$(PYTHON) -m black .
	$(PYTHON) -m ruff check --fix .

run:
	$(PYTHON) bot.py
