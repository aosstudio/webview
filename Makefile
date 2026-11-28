PYTHON ?= python3
JOBS ?= 8
APK ?= out/build/apks/SystemWebView64.apk

.PHONY: check build verify

check:
	shellcheck scripts/*.sh
	$(PYTHON) -m ruff format --check scripts
	$(PYTHON) -m ruff check scripts
	$(PYTHON) -m mypy --strict scripts
	$(PYTHON) scripts/verify.py

build:
	$(PYTHON) scripts/ts_proto.py "$${CHROMIUM_SRC:?Set CHROMIUM_SRC to the prepared Chromium tree}" out/archives
	JOBS="$(JOBS)" PYTHON="$(PYTHON)" bash scripts/build.sh

verify:
	$(PYTHON) scripts/verify.py --apk "$(APK)" --sdk "$(ANDROID_HOME)"
