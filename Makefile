.PHONY: help install test lint format check-deployment backup-check install-systemd

# Default target
help:
	@echo "Polymarket Weather Arb - Development & Deployment"
	@echo ""
	@echo "Development:"
	@echo "  make install          Install dependencies"
	@echo "  make test             Run test suite"
	@echo "  make lint             Run linter"
	@echo "  make format           Format code"
	@echo ""
	@echo "Deployment:"
	@echo "  make check-deployment Verify deployment files"
	@echo "  make backup-check     Verify backups"
	@echo "  make install-systemd  Install systemd units (requires root)"
	@echo ""
	@echo "Operations:"
	@echo "  make init-db          Initialize database"
	@echo "  make doctor           Run health check"
	@echo "  make reconcile        Run reconciliation"
	@echo "  make backup           Create backup"

# Development targets
install:
	uv sync --extra dev

test:
	uv run pytest -q

lint:
	uv run ruff check src/ tests/ scripts/

format:
	uv run ruff format src/ tests/ scripts/

# Deployment targets
check-deployment:
	python scripts/check_deployment_files.py

backup-check:
	python scripts/backup_restore_check.py

backup-check-full:
	python scripts/backup_restore_check.py --test-restore

install-systemd:
	@echo "This requires root privileges"
	sudo bash scripts/install_systemd_units.sh

# Operations targets
init-db:
	uv run polymarket-weather init-db

doctor:
	uv run polymarket-weather doctor --live

reconcile:
	uv run polymarket-weather reconcile

backup:
	uv run polymarket-weather backup-db

# Live readiness check
live-readiness:
	uv run polymarket-weather live-readiness

rehearse-live:
	python scripts/rehearse_live_readiness.py

# Dashboard
dashboard:
	uv run polymarket-weather dashboard --port 8765
