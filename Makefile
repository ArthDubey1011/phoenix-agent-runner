.PHONY: up down migrate test lint worker chaos eval timeline bench api
SEED ?= 1
RUNS ?= 30
up:
	docker compose up -d --wait
	python -m phoenix migrate
down:
	docker compose down
migrate:
	python -m phoenix migrate
test:
	python -m pytest tests/unit tests/integration
lint:
	ruff check . && ruff format --check .

worker:
	python -m phoenix worker

chaos:
	python -m phoenix chaos --seed $(SEED) --runs $(RUNS)

eval:
	python -m phoenix eval

timeline:
	python -m phoenix timeline $(RUN)

bench:
	python bench/sandbox_bench.py
	python bench/recovery_bench.py
	python bench/chaos_bench.py
	python bench/eval_bench.py

api:
	python -m phoenix api
