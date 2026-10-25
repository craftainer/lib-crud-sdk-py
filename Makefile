# template-python-package's implementation of template-base's release
# contract (docs/TEMPLATE.md, "Release: a Makefile contract"). release.yml
# sets RELEASE_VERSION / RELEASE_TAG / RELEASE_ARCH -- see there. A pure-Python
# wheel is built once, on one arch. PYPI_PUBLISH (a repo variable) selects
# where `publish-packages` uploads: unset = skip with a warning, `testpypi` or
# `pypi`. Uploads use PyPI trusted publishing (GitHub OIDC, the
# `publish-packages` job's `id-token: write`), so there is no stored token.
# `publish` is the per-arch image publish and is a no-op here (template-base
# >= 1.9.0).

.PHONY: release-arches build sbom release-assets publish publish-packages verify-published release-check finalize

ARCHES := amd64
# Distribution name from pyproject.toml, so an instance that renames the
# package needs no change here.
NAME := $(shell python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["name"])')
ASSET := dist/$(NAME)-$(RELEASE_VERSION)

# renovate: datasource=docker depName=anchore/syft
SYFT_VERSION := v1.52.0

# release.yml runs build/sbom/publish-packages on the bare runner, which has docker but
# no uv; release-check runs in the devcontainer, which has uv. So use the
# host's uv when there is one, else the pinned uv image (same uv and Python
# minor as .devcontainer/Dockerfile; tests/unit/test_release_contract.py
# fails if the uv version drifts from it). The OIDC variables are forwarded so
# `uv publish --trusted-publishing always` works inside the container.
# renovate: datasource=github-releases depName=astral-sh/uv
UV_VERSION := 0.13.5
# renovate: datasource=python-version depName=python versioning=docker
PYTHON_VERSION := 3.14
UV_IMAGE := ghcr.io/astral-sh/uv:$(UV_VERSION)-python$(PYTHON_VERSION)-trixie-slim
UV := $(shell command -v uv >/dev/null 2>&1 && echo uv || echo \
	docker run --rm -u $$(id -u):$$(id -g) -v "$(CURDIR):/src" -w /src \
	-e UV_CACHE_DIR=/tmp/uv-cache -e UV_PYTHON_DOWNLOADS=never \
	-e GITHUB_ACTIONS -e ACTIONS_ID_TOKEN_REQUEST_URL -e ACTIONS_ID_TOKEN_REQUEST_TOKEN \
	$(UV_IMAGE) uv)

TESTPYPI_UPLOAD := https://test.pypi.org/legacy/
TESTPYPI_CHECK := https://test.pypi.org/simple/
PYPI_UPLOAD := https://upload.pypi.org/legacy/
PYPI_CHECK := https://pypi.org/simple/

release-arches:
	@python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' $(ARCHES)

# Stamps RELEASE_VERSION into pyproject.toml only for the duration of the
# build (restored on exit), so the checkout stays clean.
build:
	rm -rf dist
	mkdir -p dist
	@set -e; bak=$$(mktemp); cp pyproject.toml "$$bak"; \
	trap 'cp "$$bak" pyproject.toml; rm -f "$$bak"' EXIT; \
	$(if $(RELEASE_VERSION),$(UV) version --frozen "$(RELEASE_VERSION)";) \
	$(UV) build --no-sources

sbom:
	@set -e; whl=$$(ls dist/*.whl); \
	docker run --rm -v "$(CURDIR)/dist:/dist" anchore/syft:$(SYFT_VERSION) \
		"file:/$$whl" -o "spdx-json=/$${whl%.whl}.spdx.json"

release-assets:
	@set -e; for pat in 'dist/*.tar.gz' 'dist/*.whl' 'dist/*.spdx.json'; do \
		ls $$pat >/dev/null 2>&1 || { echo "missing release asset: $$pat" >&2; exit 1; }; \
	done; ls dist

# Per arch, image registries only: this template ships no image.
publish:
	@echo "Python package -- no image to publish (see publish-packages)."

# Once, in release.yml's `publish-packages` job, which exists only when a
# *_PUBLISH variable is set and has `id-token: write`. That job is a bare
# runner: a checkout plus dist/ (the wheel and sdist `build` produced, which
# `sbom` described), so nothing is rebuilt. Only the wheel and sdist go up;
# dist/ also holds the SBOM. Add `--dry-run` to `uv publish` to check the
# files without uploading.
publish-packages:
ifeq ($(PYPI_PUBLISH),)
ifneq ($(NPM_PUBLISH)$(CARGO_PUBLISH),)
	@echo "PYPI_PUBLISH is not set -- skipping the PyPI upload (another registry is publishing)."
else
	@echo "::warning title=PyPI publish skipped::PYPI_PUBLISH is not set, so this package was NOT published to PyPI. To enable it, set the PYPI_PUBLISH repository variable to 'testpypi' or 'pypi' (Settings > Secrets and variables > Actions > Variables) and re-run the release."
endif
else ifeq ($(PYPI_PUBLISH),pypi)
	$(UV) publish --trusted-publishing always --publish-url $(PYPI_UPLOAD) --check-url $(PYPI_CHECK) dist/*.whl dist/*.tar.gz
else ifeq ($(PYPI_PUBLISH),testpypi)
	$(UV) publish --trusted-publishing always --publish-url $(TESTPYPI_UPLOAD) --check-url $(TESTPYPI_CHECK) dist/*.whl dist/*.tar.gz
else
	@echo "PYPI_PUBLISH must be 'pypi' or 'testpypi', got '$(PYPI_PUBLISH)'." >&2; exit 1
endif

# Once, in release.yml's `verify-published` job, after `publish-packages`
# succeeded and only when a *_PUBLISH variable is set: installs the published
# release from the index. A bare runner with no secrets and an older python3,
# so the script builds its venv with the pinned uv (the host's, else the
# UV_IMAGE container) -- see scripts/release-verify-published.sh.
verify-published:
	UV_IMAGE=$(UV_IMAGE) scripts/release-verify-published.sh

# Runs once after the arch leg, inside the devcontainer, and gates the GitHub
# release and publishing; dist/ holds the downloaded wheel. Checks the local
# wheel only. See scripts/release-check.sh.
release-check:
	scripts/release-check.sh

# Nothing to combine: a single wheel is built once.
finalize:
	@echo "Single pure-Python wheel -- nothing to finalize."
