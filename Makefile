PY_PKGS := packages/data packages/vision packages/coaching apps/api apps/worker
COV_MODS := cricai_data cricai_vision cricai_coaching cricai_api cricai_worker

.PHONY: setup lint typecheck test test-unit test-integration safety golden coverage seed web-lint web-test web-build check all

setup:
	uv sync --all-packages
	cd apps/web && pnpm install

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .
	uv run ruff check --fix .

typecheck:
	uv run mypy packages apps/api/src apps/worker/src scripts

# 100% line+branch coverage, enforced per package (owner directive).
test-unit:
	@set -e; \
	for i in 1 2 3 4 5; do \
		pkg=$$(echo $(PY_PKGS) | cut -d' ' -f$$i); \
		mod=$$(echo $(COV_MODS) | cut -d' ' -f$$i); \
		echo "=== $$pkg ==="; \
		uv run pytest $$pkg/tests -m "not integration and not golden" \
			--cov=$$mod --cov-branch --cov-report=term-missing --cov-fail-under=100; \
	done

test-integration:
	uv run pytest -m integration

safety:
	uv run pytest -m safety

golden:
	uv run pytest -m golden

test: test-unit

seed:
	uv run scripts/seed_demo.py

web-lint:
	cd apps/web && pnpm lint && pnpm typecheck

web-test:
	cd apps/web && pnpm test

web-build:
	cd apps/web && pnpm build

check: lint typecheck test-unit web-lint web-test

all: check test-integration safety golden web-build
