IMAGE ?= localhost/vklass-mcp:latest

.PHONY: sync test lint format check build run install-quadlet

sync:
	uv sync --all-groups

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run mypy src

format:
	uv run ruff format .
	uv run ruff check --fix .

check: test lint

build:
	podman build --format docker -t $(IMAGE) -f Containerfile .

run:
	uv run vklass-mcp

install-quadlet:
	./scripts/install-quadlet.sh
