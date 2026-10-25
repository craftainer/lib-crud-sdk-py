#!/usr/bin/env bash
# `make verify-published`: install the release that `publish-packages` just
# uploaded from the index into a fresh venv and import it.
#
# Runs on release.yml's bare runner, after the GitHub release exists and the
# upload succeeded, with no secrets or OIDC token; it needs `python3` with
# `venv` and `dist/` (the wheel `build` produced, whose version is the
# expected one). It only runs when a *_PUBLISH variable is set; with
# PYPI_PUBLISH unset (another registry is publishing) it does nothing.
#
# Reads: PYPI_PUBLISH (`testpypi` | `pypi`), RELEASE_VERSION, and
# RELEASE_CHECK_ATTEMPTS / RELEASE_CHECK_DELAY (retry tuning for index
# propagation, defaults 10 and 15 seconds, doubling up to 120).
set -euo pipefail

cd "$(dirname "$0")/.."

case "${PYPI_PUBLISH:-}" in
    "")
        echo "PYPI_PUBLISH not set -- nothing to verify."
        exit 0
        ;;
    pypi)
        index_args=(--index-url https://pypi.org/simple/)
        deps_args=()
        ;;
    testpypi)
        # Dependencies live on PyPI, not TestPyPI. So the dependencies are
        # installed first, from PyPI, through the local wheel; then the
        # package itself replaces it from TestPyPI alone (no dependencies:
        # a same-named project on PyPI can never win).
        index_args=(--index-url https://test.pypi.org/simple/ --no-deps --force-reinstall)
        deps_args=(--index-url https://pypi.org/simple/)
        ;;
    *)
        echo "PYPI_PUBLISH must be 'pypi' or 'testpypi', got '$PYPI_PUBLISH'" >&2
        exit 1
        ;;
esac

if [[ -z ${RELEASE_VERSION:-} ]]; then
    echo "RELEASE_VERSION is required to verify the published release" >&2
    exit 1
fi

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
# name-version-tag...: the version hatchling stamped (already normalized).
wheel_version=$(basename "${wheels[0]}" | cut -d- -f2)

echo "== published $name==$RELEASE_VERSION on $PYPI_PUBLISH"
attempts=${RELEASE_CHECK_ATTEMPTS:-10}
delay=${RELEASE_CHECK_DELAY:-15}
python3 -m venv "$tmp/published"
for ((i = 1; i <= attempts; i++)); do
    # Inside the loop so a transient failure of either install is retried.
    if { ((${#deps_args[@]} == 0)) || "$tmp/published/bin/python" -m pip install --quiet \
        --no-cache-dir --disable-pip-version-check "${deps_args[@]}" "${wheels[0]}"; } &&
        "$tmp/published/bin/python" -m pip install --quiet --no-cache-dir \
            --disable-pip-version-check "${index_args[@]}" "$name==$RELEASE_VERSION"; then
        got=$("$tmp/published/bin/python" -c "import importlib.metadata as m, $module; print(m.version('$name'))")
        if [[ $got != "$wheel_version" ]]; then
            echo "installed $name $got, expected $wheel_version" >&2
            exit 1
        fi
        echo "OK: import $module ($name $got)"
        exit 0
    fi
    echo "attempt $i/$attempts failed; retrying in ${delay}s (index propagation)"
    sleep "$delay"
    delay=$((delay * 2 > 120 ? 120 : delay * 2))
done
echo "$name==$RELEASE_VERSION never appeared on $PYPI_PUBLISH" >&2
exit 1
