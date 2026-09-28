PY_PKGS := packages/data packages/vision packages/coaching apps/api apps/worker
COV_MODS := cricai_data cricai_vision cricai_coaching cricai_api cricai_worker

.PHONY: setup lint typecheck test test-unit test-integration safety golden coverage seed web-lint web-test web-build contract contract-update web-e2e dev check all

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

# End-to-end journeys (Phase 8): boots scripts/dev_stack.py (real API on
# in-memory SQLite + seeded demo + next dev) and runs Playwright on a tablet and
# a desktop viewport. One-time: cd apps/web && pnpm exec playwright install chromium
web-e2e:
	cd apps/web && pnpm e2e

# Local dashboard development against the seeded demo API.
dev:
	uv run scripts/dev_stack.py

# API contract (Phase 8): the checked-in OpenAPI dump must be current; the web
# contract test (part of web-test) then pins lib/api.ts to it.
contract:
	uv run scripts/dump_openapi.py --check

contract-update:
	uv run scripts/dump_openapi.py

check: lint typecheck contract test-unit web-lint web-test

all: check test-integration safety golden web-build web-e2e
