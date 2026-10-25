#!/usr/bin/env bash
# `make release-check`: smoke-test the built wheel in a fresh venv. It gates
# the GitHub release and publishing, so it checks only the local artifact;
# the check of the published release is scripts/release-verify-published.sh
# (`make verify-published`), which runs after `publish-packages`.
set -euo pipefail

cd "$(dirname "$0")/.."

name=$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["name"])')
module=${name//-/_}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

shopt -s nullglob
wheels=(dist/*.whl)
if [[ ${#wheels[@]} -ne 1 ]]; then
    echo "expected exactly one wheel in dist/, found ${#wheels[@]}" >&2
    exit 1
fi
wheel=$(realpath "${wheels[0]}")
# name-version-tag...: the version hatchling stamped (already normalized).
wheel_version=$(basename "$wheel" | cut -d- -f2)

check_import() { # <venv> <expected version>
    local got
    got=$("$1/bin/python" -c "import importlib.metadata as m, $module; print(m.version('$name'))")
    if [[ $got != "$2" ]]; then
        echo "installed $name $got, expected $2" >&2
        exit 1
    fi
    echo "OK: import $module ($name $got)"
}

echo "== local wheel: $(basename "$wheel")"
# PEP 561: without the marker, consumers' type checkers treat the package as
# untyped (craftainer-tool-crud/-observability 0.1.0 shipped without it).
if ! python3 -c 'import sys, zipfile; sys.exit(f"{sys.argv[2]}/py.typed" not in zipfile.ZipFile(sys.argv[1]).namelist())' "$wheel" "$module"; then
    echo "$(basename "$wheel") has no $module/py.typed (PEP 561 marker)" >&2
    exit 1
fi
echo "OK: $module/py.typed is in the wheel"
uv venv --quiet "$tmp/local"
uv pip install --quiet --python "$tmp/local/bin/python" "$wheel"
check_import "$tmp/local" "$wheel_version"
