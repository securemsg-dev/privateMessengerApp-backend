.PHONY: install dev test lint format migrate upgrade downgrade

# ─── Setup ────────────────────────────────────────────────
install:
	pip install -e ".[dev]"

# ─── Development server ───────────────────────────────────
dev:
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# ─── Testing ──────────────────────────────────────────────
test:
	pytest tests/ -v --tb=short

test-cov:
	pytest tests/ -v --cov=app --cov-report=term-missing --cov-report=html

# ─── Code quality ─────────────────────────────────────────
lint:
	ruff check app/ tests/

format:
	ruff format app/ tests/

typecheck:
	mypy app/

# ─── Database migrations ──────────────────────────────────
# Usage: make migrate message="your migration message"
migrate:
	alembic revision --autogenerate -m "$(message)"

upgrade:
	alembic upgrade head

downgrade:
	alembic downgrade -1

# ─── Utilities ────────────────────────────────────────────
secret:
	@python -c "import secrets; print(secrets.token_hex(32))"
