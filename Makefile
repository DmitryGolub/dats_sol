.PHONY: run view up down build logs restart clean simulate tournament analyze versus snapshot snapshots

run:
	uv run python main.py

view:
	uv run python -m view

up:
	docker compose up -d --build

down:
	docker compose down

build:
	docker compose build

logs:
	docker compose logs -f

logs-engine:
	docker compose logs -f engine

logs-view:
	docker compose logs -f view

restart:
	docker compose restart

clean:
	docker compose down -v

simulate:
	uv run python -m strategy.runner $(ARGS)

tournament:
	uv run python -m strategy.tournament $(ARGS)

analyze:
	uv run python -m strategy.analyzer $(ARGS)

versus:
	uv run python -m strategy.tournament --versus $(ARGS)

snapshot:
	uv run python -m strategy.snapshot create

snapshots:
	uv run python -m strategy.snapshot list
