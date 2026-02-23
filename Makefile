.PHONY: lint format

lint:
	ruff check .
	mypy orcheo_examples
	ruff format . --check

format:
	ruff format .
	ruff check . --select I001 --fix
	ruff check . --select F401 --fix
