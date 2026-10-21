# 0001. Provide a Python package template with trusted publishing and CodSpeed

## Status

Proposed

## Context

The CRUD framework and the observability code that `template-fastapi`
carries are being extracted into libraries that several applications
consume. Each library needs the same quality bar the apps already have
(strict typing and linting, a 100% coverage gate, layering checks,
dependency audit, the devcontainer and CI from `template-base`) and a way to
ship releases to a package index. Copying that setup by hand into every
library would let the copies drift, and fixes would not reach them.

`template-base` (ADR-0018 in `template-fastapi`) is the upstream for
language-agnostic scaffolding and template sync, but ships no runtime and a
no-op release Makefile. `template-fastapi` has the Python tooling but also
an app, a runtime image and a compose stack that a library does not want.

Publishing needs credentials. A long-lived PyPI API token stored as a
repository secret is a standing risk; PyPI supports trusted publishing
through GitHub's OIDC identity instead. Performance regressions in a
library's hot paths are also easy to miss in review, and wall-clock
benchmarks on shared CI runners are too noisy to gate on.

## Decision

We will create `craftainer/template-python-package`, an instance of
`template-base` that is itself a template, and create every Python library
from it. It contains:

- a `src/` layout package with a `py.typed` marker, built with hatchling and
  managed with `uv` (`uv.lock` committed);
- `ruff`, `mypy --strict`, `pytest` with a 100% coverage gate,
  `import-linter` and `pip-audit`, all run by `prek` (`uv run` hooks), the
  same bar as `template-fastapi`;
- a `Makefile` implementing `template-base`'s release contract: one
  pure-Python wheel (`release-arches` is `["amd64"]`), an sdist, a Syft SPDX
  SBOM, and `uv publish --trusted-publishing always`, enabled per repository
  by the `PYPI_PUBLISH` variable (`testpypi` or `pypi`) and skipped when it
  is unset; `release-check` installs the built wheel, and the published
  version when publishing is on, into a fresh environment and imports it;
- PyPI trusted publishing: each package registers a pending publisher for
  its repository and `release.yml`, and its first release goes to TestPyPI
  as a dry run. This relies on `template-base`'s `release.yml` granting
  `id-token: write` to the publish job;
- a CodSpeed workflow running `tests/bench` in simulation mode (instruction
  counts) on pull requests, so each package benchmarks its own hot paths in
  its own repository;
- the GPL-3.0 license, matching `template-fastapi`, so the SBOM and package
  metadata carry the same license as the applications using the libraries;
- a Renovate config and a sync manifest that classifies the scaffold
  (`pyproject.toml`, `uv.lock`, `src/`, `tests/`) as instance-owned and the
  release tooling as template-owned.

## Consequences

- A new library starts with the full quality bar and release path, and
  receives fixes through two sync hops (`template-base`, then this template).
- No PyPI token exists to leak or rotate. The cost is a one-time pending
  publisher registration per package and the dependency on `release.yml`
  carrying `id-token: write`; a repository that doesn't publish leaves that
  permission unused.
- The tooling versions in `pyproject.toml` are instance-owned, so a
  template-side bump does not propagate by sync; Renovate keeps each package
  current instead, and packages can drift apart.
- A package whose `tests/bench` is empty or whose CodSpeed app isn't
  installed still passes CI but gets no regression report.
- Pure-Python only: a library with compiled extensions needs a per-arch
  build matrix and another template.
- GPL-3.0 is inherited by every library created from here unless its owner
  replaces `LICENSE`.
