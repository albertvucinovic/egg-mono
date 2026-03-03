.PHONY: venv install test lint coverage build clean

PYTHON ?= python3

venv:
	$(PYTHON) -m venv .venv
	. .venv/bin/activate; pip install -U pip

install:
	. .venv/bin/activate; pip install -e .[dev]

lint:
	. .venv/bin/activate; pyflakes eggllm tests

pytest:
	. .venv/bin/activate; pytest -q

coverage:
	. .venv/bin/activate; coverage run -m pytest -q; coverage report -m

tox:
	. .venv/bin/activate; tox -q

build:
	. .venv/bin/activate; python -m build

clean:
	rm -rf .venv dist build *.egg-info .pytest_cache .coverage htmlcov
