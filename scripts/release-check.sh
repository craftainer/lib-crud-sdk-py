#!/usr/bin/env bash
# `make release-check`: smoke-test the built wheel in a fresh venv, and the
# published release when publishing is on.
#
# Reads: PYPI_PUBLISH (unset = skip the published check; `testpypi` | `pypi`),
# RELEASE_VERSION (the version to look for on the index), and
# RELEASE_CHECK_ATTEMPTS / RELEASE_CHECK_DELAY (retry tuning for index
# propagation, defaults 10 and 15 seconds, doubling up to 120).
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

case "${PYPI_PUBLISH:-}" in
    "")
        echo "PYPI_PUBLISH not set -- skipping the published-release check."
        exit 0
        ;;
    pypi)
        index_args=(--index-url https://pypi.org/simple/)
        ;;
    testpypi)
        # Dependencies live on PyPI, not TestPyPI.
        index_args=(--index-url https://test.pypi.org/simple/
            --extra-index-url https://pypi.org/simple/
            --index-strategy unsafe-best-match)
        ;;
    *)
        echo "PYPI_PUBLISH must be 'pypi' or 'testpypi', got '$PYPI_PUBLISH'" >&2
        exit 1
        ;;
esac

if [[ -z ${RELEASE_VERSION:-} ]]; then
    echo "RELEASE_VERSION is required to check the published release" >&2
    exit 1
fi

echo "== published $name==$RELEASE_VERSION on $PYPI_PUBLISH"
attempts=${RELEASE_CHECK_ATTEMPTS:-10}
delay=${RELEASE_CHECK_DELAY:-15}
uv venv --quiet "$tmp/published"
for ((i = 1; i <= attempts; i++)); do
    if uv pip install --quiet --no-cache --python "$tmp/published/bin/python" \
        "${index_args[@]}" "$name==$RELEASE_VERSION"; then
        check_import "$tmp/published" "$wheel_version"
        exit 0
    fi
    echo "attempt $i/$attempts failed; retrying in ${delay}s (index propagation)"
    sleep "$delay"
    delay=$((delay * 2 > 120 ? 120 : delay * 2))
done
echo "$name==$RELEASE_VERSION never appeared on $PYPI_PUBLISH" >&2
exit 1
