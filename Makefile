PYTHON ?= python3
VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
export PYTHONPATH := src

.PHONY: setup eval eval-live test ui demo clean

setup:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install -U pip
	$(PIP) install -r requirements.txt
	$(PY) -m northline.seed --db data/northline.db

eval:
	$(PY) -m northline.eval --offline

eval-live:
	$(PY) -m northline.eval --live

test:
	$(PY) -m pytest

ui:
	$(PY) -m uvicorn northline.ui.app:app --host 127.0.0.1 --port 8000

demo:
	$(PY) main.py --all --offline

clean:
	rm -rf $(VENV) data/*.db data/*.sqlite logs
