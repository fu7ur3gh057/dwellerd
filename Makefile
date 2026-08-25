# Dwellerd — a small systemd daemon that watches a Linux host.
#
# Development:   make setup && make run
# Production:    make setup && sudo make install

SERVICE  := dwellerd
VENV     := .venv
PY       := $(VENV)/bin/python
PIP      := $(VENV)/bin/pip
ROOT     := $(shell pwd)

.PHONY: help venv setup run check test-alert report config \
        install uninstall purge start stop restart status logs \
        test lint clean

help:
	@echo ""
	@echo "  Dwellerd"
	@echo ""
	@echo "  Setup"
	@echo "    make setup        interactive wizard — writes config.yaml"
	@echo "    make config       show the resolved configuration"
	@echo ""
	@echo "  Run"
	@echo "    make run          run in the foreground (Ctrl-C to stop)"
	@echo "    make check        run every check once and print the results"
	@echo "    make test-alert   send a test alert to Telegram"
	@echo "    make report       build the status report and send it"
	@echo ""
	@echo "  Service (needs sudo)"
	@echo "    sudo make install     install + start the systemd unit"
	@echo "    sudo make uninstall   stop + remove the unit"
	@echo "    sudo make purge       uninstall and delete the user, config and data"
	@echo "    make start|stop|restart|status|logs"
	@echo ""
	@echo "  Development"
	@echo "    make test         run the test suite"
	@echo "    make clean        remove the venv and caches"
	@echo ""

$(VENV)/bin/python:
	python3 -m venv $(VENV)
	$(PIP) install -q --upgrade pip
	$(PIP) install -q -r requirements.txt

venv: $(VENV)/bin/python

setup: venv
	@$(PY) -m dwellerd setup

run: venv
	@$(PY) -m dwellerd run

check: venv
	@$(PY) -m dwellerd check

test-alert: venv
	@$(PY) -m dwellerd test --level $(or $(LEVEL),warn)

report: venv
	@$(PY) -m dwellerd report $(if $(PRINT),--print,)

config: venv
	@$(PY) -m dwellerd config

install:
	@bash deploy/install.sh $(ARGS)

uninstall:
	@bash deploy/uninstall.sh

purge:
	@bash deploy/uninstall.sh --purge

start:
	@sudo systemctl start $(SERVICE)

stop:
	@sudo systemctl stop $(SERVICE)

restart:
	@sudo systemctl restart $(SERVICE)

status:
	@systemctl status $(SERVICE) --no-pager || true

logs:
	@journalctl -u $(SERVICE) -f -n 100

test: venv
	@$(PIP) install -q pytest pytest-asyncio
	@$(VENV)/bin/pytest tests/ -q

clean:
	@rm -rf $(VENV) .pytest_cache
	@find . -name __pycache__ -type d -prune -exec rm -rf {} +
	@echo "cleaned"
