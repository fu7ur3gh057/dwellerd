SHELL := /bin/bash
PYTHON := python3
VENV := .venv
PIP := $(VENV)/bin/pip
PY := $(VENV)/bin/python
CONFIG := config.yaml
SERVICE := dwellerd
UNIT_PATH := /etc/systemd/system/$(SERVICE).service
DWELLERD_USER := dwellerd
DWELLERD_HOME := /var/lib/dwellerd
DWELLERD_ETC := /etc/dwellerd
CLIENT_DIR := client

.PHONY: help setup install run run-web run-bot reset-password ensure-perms fix-perms bootstrap-user preflight \
	client-install client-dev client-build client-clean ensure-node \
	install-service uninstall-service install-bot-service uninstall-bot-service \
	install-cli uninstall-cli start stop restart status logs \
	bot-start bot-stop bot-restart bot-status bot-logs clean

help:
	@echo "Dwellerd — targets:"
	@echo "  make setup              интерактивная настройка (config.yaml + опц. systemd)"
	@echo "  make install            создать venv и поставить зависимости"
	@echo "  make run                воркер в foreground (нужен $(CONFIG))"
	@echo "  make run-web            воркер + FastAPI на 127.0.0.1:8765"
	@echo "  make run-bot            запустить Telegram-бота (aiogram, polling)"
	@echo "  make ensure-perms       проверить, что ./data и ./logs пишутся текущим юзером"
	@echo "  make fix-perms          вернуть владельца ./data и ./logs текущему юзеру"
	@echo ""
	@echo "  make bootstrap-user     завести системного юзера '$(DWELLERD_USER)' + группы (sudo)"
	@echo "  make preflight          проверить доступ '$(DWELLERD_USER)' к docker/journal/логам"
	@echo ""
	@echo "  make install-service        поставить systemd-юнит демона от '$(DWELLERD_USER)' (sudo)"
	@echo "  make uninstall-service      остановить и удалить systemd-юнит демона (sudo)"
	@echo "  make install-bot-service    поставить systemd-юнит бота '$(SERVICE)-bot' (sudo)"
	@echo "  make uninstall-bot-service  остановить и удалить systemd-юнит бота (sudo)"
	@echo "  make install-cli            поставить глобальную команду '$(SERVICE)' в ~/.local/bin"
	@echo "  make uninstall-cli          удалить глобальную команду"
	@echo "  make start | stop | restart | status | logs              — демон"
	@echo "  make bot-start | bot-stop | bot-restart | bot-status | bot-logs   — бот"
	@echo "  make clean                  убрать venv и __pycache__"

$(VENV):
	@bash -c '. deploy/scripts/_bootstrap.sh && ensure_venv "$$PWD"'

install: $(VENV)

setup:
	@bash deploy/scripts/setup.sh

# Pre-flight for dev mode (`make run` from a shell): make sure ./data and ./logs
# are owned by the current user. If a previous systemd run left files owned by
# the `dwellerd` user, this prompts for `sudo chown -R` before the daemon trips
# on them.
ensure-perms:
	@bash -c '. deploy/scripts/_bootstrap.sh && ensure_writable_paths "$$PWD"'

# Same check, no prompt — chowns silently. Useful right after stopping the
# systemd service when you want to switch back to dev mode.
fix-perms:
	@DWELLERD_YES=1 bash -c '. deploy/scripts/_bootstrap.sh && ensure_writable_paths "$$PWD"'

bootstrap-user:
	@bash -c '. deploy/scripts/_bootstrap.sh && ensure_dwellerd_user'

preflight:
	@bash -c '. deploy/scripts/_bootstrap.sh && preflight_dwellerd "$$PWD"'

run: install ensure-perms
	@if [ ! -f $(CONFIG) ]; then echo "$(CONFIG) не найден — запусти 'make setup'"; exit 1; fi
	PYTHONPATH=server $(PY) -m main $(CONFIG)

run-web: install ensure-perms
	@if [ ! -f $(CONFIG) ]; then echo "$(CONFIG) не найден — запусти 'make setup'"; exit 1; fi
	PYTHONPATH=server $(PY) -m main $(CONFIG) --web

# ── bot (aiogram, отдельный процесс) ─────────────────────────────────────
# Токен берётся из DWELLERD_BOT_TOKEN или из bot.token / первого telegram-
# notifier'а в config.yaml. Админы — DWELLERD_BOT_ADMINS=123,456 либо
# bot.admins в YAML.
run-bot: install
	@if [ ! -f $(CONFIG) ]; then echo "$(CONFIG) не найден — запусти 'make setup'"; exit 1; fi
	PYTHONPATH=server $(PY) -m bot

# Сбросить пароль существующего юзера или создать нового. Спрашивает пароль
# дважды (без эха), хэширует тем же bcrypt что и wizard, апдейтит users.
reset-password:
	@if [ -z "$(USER_NAME)" ]; then echo "usage: make reset-password USER_NAME=admin"; exit 1; fi
	PYTHONPATH=server $(PY) deploy/scripts/reset-password.py "$(USER_NAME)" $(if $(CREATE),--create-if-missing,)

# ── client (Next.js) — арендатор Этапа 5 ─────────────────────────────────

ensure-node:
	@bash -c '. deploy/scripts/_bootstrap.sh && ensure_node'

PKG_LOOKUP := PKG=$$(command -v pnpm 2>/dev/null || command -v npm 2>/dev/null); \
	if [ -z "$$PKG" ]; then echo "neither pnpm nor npm found"; exit 1; fi

client-install: ensure-node
	@if [ ! -d $(CLIENT_DIR) ]; then echo "client/ ещё не перенесён — Этап 5"; exit 0; fi
	@$(PKG_LOOKUP); cd $(CLIENT_DIR) && $$PKG install

client-dev: ensure-node
	@if [ ! -d $(CLIENT_DIR) ]; then echo "client/ ещё не перенесён — Этап 5"; exit 0; fi
	@$(PKG_LOOKUP); cd $(CLIENT_DIR) && $$PKG run dev

client-build: ensure-node
	@if [ ! -d $(CLIENT_DIR) ]; then echo "client/ ещё не перенесён — Этап 5"; exit 0; fi
	@$(PKG_LOOKUP); \
	NM=$(CLIENT_DIR)/node_modules; \
	PJ=$(CLIENT_DIR)/package.json; \
	PL=$(CLIENT_DIR)/package-lock.json; \
	NEEDS_INSTALL=0; \
	if [ ! -d $$NM ]; then NEEDS_INSTALL=1; \
	elif [ $$PJ -nt $$NM ]; then NEEDS_INSTALL=1; \
	elif [ -f $$PL ] && [ $$PL -nt $$NM ]; then NEEDS_INSTALL=1; \
	fi; \
	if [ $$NEEDS_INSTALL = 1 ]; then \
		echo "  dependencies out of sync — running $$PKG install..."; \
		cd $(CLIENT_DIR) && $$PKG install; cd ..; \
	fi; \
	cd $(CLIENT_DIR) && NODE_OPTIONS="--max-old-space-size=4096" $$PKG run build

client-clean:
	rm -rf $(CLIENT_DIR)/.next $(CLIENT_DIR)/out

# ── service ──────────────────────────────────────────────────────────────

BOT_SERVICE := $(SERVICE)-bot
BOT_UNIT_PATH := /etc/systemd/system/$(BOT_SERVICE).service

install-service:
	@bash deploy/scripts/install-service.sh

uninstall-service:
	@bash deploy/scripts/uninstall-service.sh

install-bot-service:
	@bash deploy/scripts/install-bot-service.sh

uninstall-bot-service:
	@bash deploy/scripts/uninstall-bot-service.sh

install-cli:
	@bash deploy/scripts/install-cli.sh

uninstall-cli:
	@rm -f $(HOME)/.local/bin/$(SERVICE) && echo "removed: $(HOME)/.local/bin/$(SERVICE)"

# Daemon — graceful start/stop/restart, only if unit exists
start:
	@if [ -f $(UNIT_PATH) ]; then sudo systemctl start $(SERVICE); else echo "$(SERVICE).service не установлен — запусти 'make install-service'"; fi
stop:
	@if [ -f $(UNIT_PATH) ]; then sudo systemctl stop $(SERVICE); else echo "$(SERVICE).service не установлен — нечего останавливать"; fi
restart:
	@if [ -f $(UNIT_PATH) ]; then sudo systemctl restart $(SERVICE); else echo "$(SERVICE).service не установлен — запусти 'make install-service'"; fi
status:
	@if [ -f $(UNIT_PATH) ]; then systemctl status $(SERVICE) --no-pager; else echo "$(SERVICE).service не установлен"; fi
logs:
	@if [ -f $(UNIT_PATH) ]; then sudo journalctl -u $(SERVICE) -f; else echo "$(SERVICE).service не установлен"; fi

# Bot — same shape
bot-start:
	@if [ -f $(BOT_UNIT_PATH) ]; then sudo systemctl start $(BOT_SERVICE); else echo "$(BOT_SERVICE).service не установлен — запусти 'make install-bot-service'"; fi
bot-stop:
	@if [ -f $(BOT_UNIT_PATH) ]; then sudo systemctl stop $(BOT_SERVICE); else echo "$(BOT_SERVICE).service не установлен — нечего останавливать"; fi
bot-restart:
	@if [ -f $(BOT_UNIT_PATH) ]; then sudo systemctl restart $(BOT_SERVICE); else echo "$(BOT_SERVICE).service не установлен — запусти 'make install-bot-service'"; fi
bot-status:
	@if [ -f $(BOT_UNIT_PATH) ]; then systemctl status $(BOT_SERVICE) --no-pager; else echo "$(BOT_SERVICE).service не установлен"; fi
bot-logs:
	@if [ -f $(BOT_UNIT_PATH) ]; then sudo journalctl -u $(BOT_SERVICE) -f; else echo "$(BOT_SERVICE).service не установлен"; fi

clean:
	rm -rf $(VENV)
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
