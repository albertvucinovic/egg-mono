.PHONY: install install-dev test lint clean

install:
	pip install -e ./eggllm
	pip install -e ./eggconfig
	pip install -e ./eggthreads
	pip install -e ./eggdisplay
	pip install -e ./eggflow
	pip install -e ./egg
	pip install -e ./eggw

install-dev:
	pip install -e "./eggllm[dev]"
	pip install -e ./eggconfig
	pip install -e "./eggthreads[dev]"
	pip install -e ./eggdisplay
	pip install -e "./eggflow[dev]"
	pip install -e "./egg[dev]"
	pip install -e ./eggw

test: install-dev
	pytest eggllm/tests -q
	pytest eggthreads/tests -q
	pytest eggdisplay/tests -q
	pytest eggflow/tests -q
	pytest egg/tests -q

lint:
	pyflakes eggllm/eggllm eggthreads/eggthreads

clean:
	find . -name "*.egg-info" -type d -exec rm -rf {} + 2>/dev/null || true
	find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
