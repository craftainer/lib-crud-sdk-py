#!/usr/bin/env bash
# Installs the project's dependencies (incl. the dev group) into the venv
# UV_PROJECT_ENVIRONMENT points at (default: .venv/ in the repo).
set -euo pipefail

uv sync
