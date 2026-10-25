#!/usr/bin/env bash
# `make verify-published`: install the release that `publish-packages` just
# uploaded from the index into a fresh venv and import it.
#
# Runs on release.yml's bare runner, after the GitHub release exists and the
# upload succeeded, with no secrets or OIDC token; it needs `dist/` (the wheel
# `build` produced, whose version is the expected one). It only runs when a
# *_PUBLISH variable is set; with PYPI_PUBLISH unset (another registry is
# publishing) it does nothing.
#
# The venv must use an interpreter that satisfies the package's
# `requires-python` (the runner's own python3 is older: 3.12 on
# ubuntu-24.04), so it is built with the pinned uv. With no uv on the host
# (the bare runner) the script re-runs itself inside the Makefile's pinned uv
# image ($UV_IMAGE, which ships the matching Python); the runner needs only
# docker. If no interpreter satisfies `requires-python` it fails at once with
# that reason, instead of retrying an install pip/uv can never complete.
#
# Reads: PYPI_PUBLISH (`testpypi` | `pypi`), RELEASE_VERSION, UV_IMAGE (set by
# the Makefile; needed only without a host uv) and
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
        index_args=(--index-url https://test.pypi.org/simple/ --no-deps --reinstall)
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

# No uv here (the bare runner): re-run inside the pinned uv image, whose
# Python is the one the repository pins. Nothing in the container needs
# secrets; only the variables read below are forwarded.
if ! command -v uv >/dev/null 2>&1 && [[ -z ${VERIFY_IN_CONTAINER:-} ]]; then
    if [[ -z ${UV_IMAGE:-} ]] || ! command -v docker >/dev/null 2>&1; then
        echo "need uv, or docker plus UV_IMAGE (run via make verify-published)" >&2
        exit 1
    fi
    exec docker run --rm -u "$(id -u):$(id -g)" -v "$PWD:/src" -w /src \
        -e UV_CACHE_DIR=/tmp/uv-cache -e UV_PYTHON_DOWNLOADS=never \
        -e VERIFY_IN_CONTAINER=1 -e PYPI_PUBLISH -e RELEASE_VERSION \
        -e RELEASE_CHECK_ATTEMPTS -e RELEASE_CHECK_DELAY \
        --entrypoint bash "$UV_IMAGE" scripts/release-verify-published.sh
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
requires=$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb")).get("project", {}).get("requires-python", ""))')
venv_args=()
[[ -z $requires ]] || venv_args+=(--python "$requires")
if ! uv venv "${venv_args[@]}" "$tmp/published"; then
    echo "could not create a venv with a Python satisfying requires-python '${requires:-<unset>}' (see the uv error above); not retrying" >&2
    exit 1
fi
for ((i = 1; i <= attempts; i++)); do
    # Inside the loop so a transient failure of either install is retried.
    if { ((${#deps_args[@]} == 0)) || uv pip install --quiet \
        --no-cache --python "$tmp/published/bin/python" "${deps_args[@]}" "${wheels[0]}"; } &&
        uv pip install --quiet --no-cache --python "$tmp/published/bin/python" \
            "${index_args[@]}" "$name==$RELEASE_VERSION"; then
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
