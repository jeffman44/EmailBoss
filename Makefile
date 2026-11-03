# Email Boss developer targets. Same targets exist for Windows in tasks.ps1.
#
#   make test            app + license server test suites
#   make run             start the app from source on :8080 with a dev license
#   make serve-license   start the license server on :8600 (dev key, trials on)
#   make build           wheel + PyInstaller onedir bundle (app/dist/)
#   make check           compileall + the banned-literal / em-dash grep + both pytest suites
#   make bench           the 50k-message benchmark (writes app/bench.json)
#   make clean

PY       ?= python3
PORT     ?= 8080
LIC_PORT ?= 8600
APP      := app
LIC      := license_server

# tasks.ps1 carries the same pattern, so it is excluded from the scan of shipped
# text. DESIGN.md is scanned like everything else.
GREP_FILES := --include='*.md' --include='*.py' --include='*.html' --include='*.yaml' --include='*.json' --include='*.js' --include='*.css' \
              --include='*.ps1' --include='*.sh' --exclude-dir=tests --exclude-dir=screens --exclude-dir=.git \
              --exclude-dir=__pycache__ --exclude-dir=dist --exclude-dir=build \
              --exclude=tasks.ps1 --exclude=Makefile

.PHONY: help test test-app test-license run serve-license build wheel check bench clean install-dev

help:
	@grep -E '^#   make' Makefile | sed 's/^#   //'

install-dev:
	cd $(APP) && $(PY) -m pip install -e ".[dev,attachments,http]"
	cd $(LIC) && $(PY) -m pip install -e ".[test]"

test: test-app test-license

test-app:
	cd $(APP) && $(PY) -m compileall -q plannerboard && $(PY) -m pytest -q

test-license:
	cd $(LIC) && $(PY) -m pytest -q

run:
	cd $(APP) && PB_LICENSE_DEV=1 $(PY) -m plannerboard --port $(PORT) --no-tray

serve-license:
	cd $(LIC) && TRIAL_ENABLED=1 $(PY) -m uvicorn licserver.main:app --reload --port $(LIC_PORT)

wheel:
	cd $(APP) && $(PY) -m pip install -q --upgrade build && $(PY) -m build --wheel --outdir dist

build:
	cd $(APP) && ./scripts/build_release.sh

check:
	cd $(APP) && $(PY) -m compileall -q plannerboard
	@! grep -rInE $(GREP_FILES) 'amsofusa|\bAMS\b|JDeuel|QRF|SAMPro|Tucson|C:\\Workspace' . || (echo "banned literal found" && exit 1)
	@! grep -rIn $(GREP_FILES) "$$(printf '\342\200\224')" . || (echo "em dash found" && exit 1)
	cd $(APP) && $(PY) -m pytest -q
	cd $(LIC) && $(PY) -m pytest -q
	@echo "check ok"

bench:
	cd $(APP) && EB_BENCH=1 $(PY) -m pytest -q tests/bench

clean:
	rm -rf $(APP)/build $(APP)/dist $(APP)/*.egg-info $(LIC)/build $(LIC)/dist $(LIC)/*.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	find . -name .pytest_cache -type d -prune -exec rm -rf {} +
