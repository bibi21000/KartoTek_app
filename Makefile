#!/usr/bin/make -f
-include makefile.local

ifndef PYTHON
PYTHON:=python3
endif
VERSION := $(shell grep -m 1 version pyproject.toml | tr -s ' ' | tr -d '"' | tr -d "'" | cut -d' ' -f3)

.PHONY: venv venv-min venv-dev build tests i18n-extract i18n-init i18n-update i18n-compile i18n docker


venv-min:
	${PYTHON} -m venv venv
	./venv/bin/pip install -e .
	./venv/bin/pip install -e .[tkinter]
	./venv/bin/pip install -e .[scan]

venv: venv-min
	./venv/bin/pip install -e .[similar]
	./venv/bin/pip install -e .[travel]

venv-dev: venv
	./venv/bin/pip install -e .[dev]
	./venv/bin/pip install -e .[simpostcards]
	./venv/bin/pip install -e .[flask]

serve:
	./venv/bin/python src/run.py

build:
	rm -rf dist build
	${MAKE} i18n-extract
	${MAKE} i18n-update
	${MAKE} i18n-compile
	./venv/bin/python3 -m build

coverage:
	-./venv/bin/coverage combine
	./venv/bin/coverage report --include pypostcards

ruff:
	./venv/bin/ruff check src/

tests:
	./venv/bin/pytest  --random-order tests/

deps_scan:
	sudo apt-get install sane sane-utils

deps_ocr:
	sudo apt install tesseract-ocr tesseract-ocr-fra

i18n-extract:
	./venv/bin/python3 scripts/i18n.py extract

i18n-init:
	./venv/bin/python3 scripts/i18n.py init

i18n-update:
	./venv/bin/python3 scripts/i18n.py update

i18n-compile:
	./venv/bin/python3 scripts/i18n.py compile

i18n: i18n-update i18n-compile

dockerfl:
	docker build -f docker_flpostcards/Dockerfile -t flpostcards .

dockerfl-push:
	docker tag flpostcards localhost:5000/flpostcards
	docker push localhost:5000/flpostcards

dockersim:
	docker build -f docker_simpostcards/Dockerfile -t simpostcards .

dockersim-push:
	docker tag simpostcards localhost:5000/simpostcards
	docker push localhost:5000/simpostcards

appimagetool-x86_64.AppImage:
	wget https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage
	chmod +x appimagetool-x86_64.AppImage

KartoTek.AppDir:
	python3 -m venv venv.AppImage
	./venv.AppImage/bin/pip install .
	./venv.AppImage/bin/pip install .[tkinter]
	./venv.AppImage/bin/pip install .[similar]
	./venv.AppImage/bin/pip install .[scan]
	./venv.AppImage/bin/pip install .[travel]
	mkdir KartoTek.AppDir
	./venv.AppImage/bin/pip freeze > KartoTek.AppDir/requirements.txt
	cp appimage/AppRun KartoTek.AppDir/
	chmod +x KartoTek.AppDir/AppRun      # +x pas -x !
	cp appimage/KartoTek.desktop KartoTek.AppDir/
	cp pyproject.toml KartoTek.AppDir/
	cp logos/kartotek_tkinter_256.png KartoTek.AppDir/kartotek.png  # minuscule !
	./venv/bin/python-appimage build app \
		--name KartoTek \
		--python-version 3.12 \
		KartoTek.AppDir/
