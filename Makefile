.ONESHELL:
SHELL := $(shell command -v bash)
.SHELLFLAGS := -euo pipefail -c
.SILENT:

.PHONY: all


all:
	rm -rf dist
	mkdir dist
	nfpm package   --config nfpm.yaml   --packager rpm   --target dist/

