# Template conventions

Everything about this repository's structure, tooling, and workflow that's
identical across every instance of this template — as opposed to the root
`README.md`'s short, instance-owned preface. See `../CLAUDE.md`'s "Keeping
this file current" for where a new convention belongs.

`template-base` ships the generic "devcontainer + CI + AI-assisted
workflow" scaffold only — no application runtime, no backing services, no
language assumed. It carries no `src/`/`tests/` of its own. An instance
(a Python/Node.js/Rust/Go library, an infra-only repo, or a full
application) adds its own language runtime, backing services, and release
artifact on top of the shape described here.

## Contents

- `.devcontainer/` — the devcontainer setup; see its `README.md`. An
  instance that needs backing services adds a `stack/` directory here,
  following the "Devcontainer stack pattern" convention this template's
  own instances (e.g. `template-fastapi`) already establish.
- `.github/` — CI and release workflows; see its `CONTENTS.md`.
- `.vscode/` — editor settings, tasks; see its `README.md`.
- `.claude/` — Claude Code CLI project config; see its `README.md`.
- `.mcp.json` — project-scope MCP servers not covered by a
  `.claude/settings.json` plugin; see `.claude/README.md`.
- `docs/` — knowledge about what the instance does; this file is the one
  exception, documenting the template itself rather than product/domain
  knowledge. `adrs/`, `frs/`, `nfrs/`, `plans/` ship only their own
  `README.md` + `template.md` scaffolding — no numbered content, which is
  entirely instance-owned.
- `.secrets/` — local secret files, never committed; see its `README.md`.
- `.devcontainer/Dockerfile` — a single `develop` stage (the devcontainer
  image), with its build-context excludes in
  `.devcontainer/Dockerfile.dockerignore`. The repo root has no
  `Dockerfile`: that one name is reserved for an instance's own runtime
  image (see "Instance extension points"), so a repo never has more than
  one top-level Dockerfile.
- `scripts/develop.sh`, `scripts/post-create.sh` — the `develop` stage's
  setup script and the devcontainer's `postCreateCommand`, each running
  an instance hook directory (`develop.d/`, `post-create.d/`); see
  `scripts/README.md` and "Instance extension points" below.
- `Makefile` — the release contract `release.yml` drives
  (`build`/`sbom`/`release-assets`/`publish`/`publish-packages`), each a documented no-op or implementation
  here — see "Release: a Makefile contract" below.
- `.pre-commit-config.yaml` — git hooks, run by `prek` or `pre-commit`;
  language-agnostic hooks only.
- `CLAUDE.md` — the AI-assisted coding workflow Claude Code follows in
  this repository; general conventions live in this file and each
  directory's own `README.md` instead.

## Instance extension points

Base-owned files stay `replace` tier (a sync overwrites them) and expose
hooks an instance fills with files it owns, so an instance never edits a
base-owned file directly. Don't edit `.devcontainer/Dockerfile`, `scripts/develop.sh`
or `compose.yml` in an instance; use the hooks below.

| Hook | Base side (template-owned) | Instance side (instance-owned) |
|---|---|---|
| Extra devcontainer tooling | `.devcontainer/Dockerfile` `develop` stage runs every `scripts/develop.d/*.sh` after `develop.sh` | `scripts/develop.d/NN-name.sh` |
| Extra post-create steps | `scripts/post-create.sh`, run by `devcontainer.json`'s `postCreateCommand`, runs every `scripts/post-create.d/*.sh` | `scripts/post-create.d/NN-name.sh` |
| Extra compose services / dev-service settings | `devcontainer.json` `"dockerComposeFile": ["compose.yml", "compose.instance.yml"]` | `.devcontainer/compose.instance.yml` (base ships a stub, `ignore` tier) |
| Runtime image | nothing (the devcontainer image lives in `.devcontainer/`) | top-level `Dockerfile` + `.dockerignore`, listed in `.github/template-sync-manifest.local.yml`'s `ignore:` |

Files with no include mechanism of their own (JSON configs,
`.pre-commit-config.yaml` — prek has no include) are `merge` tier, so an
instance's additions survive a sync. The runtime image is the
top-level `Dockerfile` with a plain `.dockerignore`, both instance-owned:
base keeps the devcontainer image and its excludes under
`.devcontainer/`, where BuildKit picks up
`.devcontainer/Dockerfile.dockerignore` for that build automatically.

**One-time upgrade (instances synced from before this layout).** A sync
adds `.devcontainer/Dockerfile` but can't delete the old top-level
`Dockerfile`/`.dockerignore`: from this release on, base's manifest no
longer classifies those paths, and a merge-tier file is never deleted
automatically. In the sync PR, by hand:

- With a runtime image: rename `app.Dockerfile` → `Dockerfile` and
  `app.Dockerfile.dockerignore` → `.dockerignore`, overwriting the old
  base files. Update every reference (`-f app.Dockerfile` in the
  `Makefile`, `dockerfile:` in compose files, CI, docs) and list both
  paths under `ignore:` in `.github/template-sync-manifest.local.yml`.
- Without one: delete the old top-level `Dockerfile` and `.dockerignore`.

## Getting started

1. Open this folder in a devcontainer (VS Code: "Reopen in Container" —
   `.vscode/extensions.json` recommends the extension that offers this —
   or any tool that reads `.devcontainer/devcontainer.json`). This builds
   the `develop` stage and installs the git hooks via `postCreateCommand`
   (`scripts/post-create.sh`).
2. There's nothing to run yet — `template-base` ships no application. An
   instance documents its own "run the app" / "run the CLI" step here.

Without a devcontainer: install [`prek`](https://prek.j178.dev/) and run
`prek install` once to enable the git hooks; install whatever else your
instance's own toolchain needs.

## Checks

`.pre-commit-config.yaml` defines whitespace/EOF fixers, YAML/TOML/JSON
checks, `conventional-pre-commit` (Conventional Commits, enforced at the
`commit-msg` stage), the `template-sync-manifest` completeness check, and
the `devcontainer-lock` lockfile-consistency check. An instance layers
its own language-specific hooks on top (lint,
format, type check, test suite, dependency-vulnerability scan, ...),
matching the shape `template-fastapi`'s own `.pre-commit-config.yaml`
already establishes for `ruff`/`mypy`/`pytest`/`pip-audit`.

Run everything at once with:

```bash
prek run --all-files --hook-stage manual
```

Every hook except the commit-message check carries the `manual` stage,
so the command above runs everything else regardless of which git hook
would normally trigger it. Commit messages can only be checked by
actually committing (see the comment in `.pre-commit-config.yaml`).

CI (`.github/workflows/checks.yml`) runs the same `--hook-stage manual`
command, inside the devcontainer itself, on every push and pull request
— as an `amd64`/`arm64` matrix, both legs native (no QEMU).
`.github/workflows/release.yml` is triggered manually to cut an
alpha/beta/rc/full release — see "Release: a Makefile contract" below and
`.github/workflows/README.md`.

## Release: a Makefile contract

`release.yml` computes the next SemVer tag (`compute_next_version.py`,
unchanged across every instance), then drives these targets:

| Target | Runs | Where | Env available |
|---|---|---|---|
| `release-arches` | once, first | host runner | none; must print a JSON array on stdout, e.g. `["amd64"]` |
| `build`, `sbom`, `release-assets`, `publish` | once **per arch**, in that order | host runner for that arch | `RELEASE_VERSION`, `RELEASE_TAG`, `RELEASE_ARCH`, `OCI_REGISTRY`, `OCI_IMAGE_NAME`, `OCI_REGISTRY_USERNAME`, `OCI_REGISTRY_PASSWORD`, all `GITHUB_*`; no package-registry variables or secrets, and no OIDC token |
| `release-check` | once, after all arches; gates the GitHub release and `publish-packages` (not the per-arch image `publish`, which already ran in `build`) | **inside the devcontainer** (`devcontainers/ci`), stack up | `RELEASE_VERSION`, `RELEASE_TAG`, `GITHUB_REPOSITORY`; **not** `RELEASE_ARCH` (it runs once, not per arch) and no `ARCHES`: to check each arch's files in `dist/`, iterate over `make -s release-arches` |
| `finalize` | once, after `release-check`; then the GitHub release is created | host runner | same as per-arch minus `RELEASE_ARCH` |
| `publish-packages` | once, after the GitHub release exists, **only if** `PYPI_PUBLISH`, `NPM_PUBLISH` or `CARGO_PUBLISH` is set | host runner (own job, with `id-token: write`) | `RELEASE_VERSION`, `RELEASE_TAG`, `PYPI_PUBLISH`, `NPM_PUBLISH`, `CARGO_PUBLISH`, all `GITHUB_*`, the OIDC token, `NPM_TOKEN` and `CARGO_REGISTRY_TOKEN` (bootstrap secrets; empty when unset, so treat empty as absent); `dist/` holds every arch leg's files |
| `verify-published` | once, after `publish-packages` succeeded, **only if** a `*_PUBLISH` variable is set | host runner (own job, no secrets, no OIDC token) | `RELEASE_VERSION`, `RELEASE_TAG`, `PYPI_PUBLISH`, `NPM_PUBLISH`, `CARGO_PUBLISH`; `dist/` as for `publish-packages` |

What each target is for: `build` produces the release artifact(s) (an
image, `cargo build --release`, `npm pack`, `helm package`, ...); `sbom`
writes an SBOM for what `build` produced; `release-assets` populates
`dist/` (gitignored) with every file to attach to the GitHub release;
`publish` pushes the per-arch image to its registry, skipping cleanly when
`OCI_REGISTRY` isn't set; `publish-packages` uploads packages (PyPI, npm,
crates.io) once per release, never once per arch, and runs in a job that
exists only for instances that set one of the `*_PUBLISH` variables, so
an image-only instance never has `id-token: write` or a package secret
(base's default fails if any `*_PUBLISH` variable is set, so an instance
can't silently skip it; with none set it is a no-op; it runs on a fresh runner with a checkout and the
downloaded `dist/` only, so it uploads from `dist/` and sets up its own
tooling); `verify-published` checks the release from the registry (e.g.
installs the just-published package) and is a no-op in base's default, since
there is nothing to verify unless an instance implements it;
`release-check` runs checks that need the backing services (e.g. tests
producing a coverage report) and the local artifact; `finalize`
does cross-arch work such as combining per-arch registry tags into one
multi-arch manifest list. `RELEASE_VERSION` (e.g. `1.2.3` or
`1.2.3-alpha.1`) and `RELEASE_TAG` (the same, `v`-prefixed) are for
embedding or tagging with the version. A single-arch no-op is the
default: `release-arches` prints `["amd64"]`.

**Upgrading a package instance.** Before this contract, `publish` also
uploaded packages, once per arch. Now `publish` is image-only and gets no
`*_PUBLISH` variables, secrets or OIDC token. An instance that publishes
packages must move that upload from `publish` into a new
`publish-packages` target (reading artifacts from `dist/`, setting up its
own tooling). Until it does, base's default `publish-packages` (if the
instance kept it) fails with a message naming the variable that is set;
an instance whose Makefile has no such target fails with `make: No rule
to make target`. Either way nothing is uploaded, and because the GitHub
release is created first (see "Job order"), the release already exists
when the `publish-packages` job fails: it is out without its packages.
Fix the Makefile and re-run the failed jobs to publish them; do not cut
another release for it.

**Job order.** `build` (per arch) → `release-check` → `release`
(`finalize`, then the GitHub release) → `publish-packages` →
`verify-published`. `release-check` gates both the release and package publishing. It does
not gate the per-arch image `publish`, which runs inside `build`, so a pushed
per-arch image can exist for a release that `release-check` then fails.
The GitHub release exists before any registry upload, so a partial
registry failure leaves the release in place and a re-run of the failed
job finishes publishing. So an instance's `publish-packages` **must be
safe to run again**: it skips versions already on the registry (for
example `uv publish --check-url`, or an `npm view` guard) instead of failing
on them or double-publishing.

The `release` job is re-runnable too: if the release already exists it
skips `finalize` and the release creation (a leftover draft is deleted and
recreated), so re-running failed jobs after a later job failed does not
fail on the existing release. If `release` itself failed before the release
existed, `finalize` runs again on the re-run, so an instance's `finalize`
must be safe to repeat (it must not double-apply its side effects).
`verify-published` runs only after `publish-packages` succeeded, so after a
partial `publish-packages` failure nothing is verified until the re-run of
the failed jobs, which then runs it.

Two runs for the same tag cannot overlap in the `release` job: it sits in a
`concurrency` group named after the tag (`cancel-in-progress: false`), so the
second waits and then sees the published release. If that release was cut
from a different commit than the waiting run's (two dispatches that computed
the same tag), the waiting run fails instead of publishing its own `dist/`
under that version (detected only here, after the per-arch images of both runs were
pushed). GitHub keeps at most one waiting run per group, and a
newer waiting run replaces it.

Re-run only the *failed jobs* of a run. "Re-run all jobs" runs `version`
again, which sees the tag the first attempt created and computes the next
version, so it cuts another release instead of finishing the first.

In `release-check`, if `dist/` holds files owned by another user (the
devcontainer ran as root), the job takes them over with `sudo`, which must
be passwordless (`sudo -n`); the runner images provide that. A self-hosted
runner without it fails there with a message saying so.

Package templates (npm, Python, Rust) move their
registry check ("install what was just published") from `release-check` to
`verify-published`; their `publish-packages` is unchanged and already skips
versions already on the registry. Base's
`release-check` no longer receives the `*_PUBLISH` variables.

**Package instance checklist.** An instance that publishes a package also:
sets `repository`, homepage and bugs URLs in its package manifest
(`package.json`, `pyproject.toml`, `Cargo.toml`) -- npm provenance
verifies the repository URL against the publishing repository, and the
other registries show these links to users; and implements `publish-packages`
(re-runnable: it skips versions already on the registry) in its own `Makefile`, which replaces base's default (the
`DefaultPublishPackagesTest` in `.github/scripts` skips itself once the
default message is gone). That only works where the `Makefile` is
instance-owned, as in base. A downstream template that makes the `Makefile`
replace-tier (an image template, say) gives its instances no place to
implement it, so such a template either ships the `publish-packages` logic
itself, driven by variables the instance sets, or is not a fit for package
instances. `release-check` receives
`GITHUB_REPOSITORY`, so an instance can run repository guards there.
It also moves any "install what was just published" check from
`release-check` to `verify-published` (see "Job order" above).

*In this template (Python).* `publish` is a no-op and `publish-packages`
runs `uv publish --trusted-publishing always` on the wheel and sdist
`make build` already put in `dist/` (nothing is rebuilt; the SBOM stays
out of the upload). The job is a bare runner with no `uv`, so the
Makefile uses the pinned uv image there, with the OIDC variables
forwarded. `PYPI_PUBLISH` unset prints a `::warning::` and succeeds (the
job only exists when some `*_PUBLISH` variable is set, e.g. only
`NPM_PUBLISH`); any value but `pypi` or `testpypi` fails.

To upgrade a Python package instance created from this template, sync to
this release: the `Makefile` is template-owned (`replace`), so it brings
`publish-packages`, `verify-published` and `scripts/release-verify-published.sh`
with it, as well as base's `release.yml` (release first, then publish; see
"Job order" above). Then:
(1) the PyPI trusted publisher's workflow name stays `release.yml`, but
the job that uploads is now `publish-packages`; if the publisher
restricts the job or an environment, update it; (2) if the instance
overrode `publish` locally, move that upload into `publish-packages`;
(3) if the instance has its own `tests/` that assert the old `publish`,
update them; (4) the registry install check moved from `release-check` to
`verify-published` (a Makefile that overrides either target locally needs the
same move); (5) check a release with `testpypi` before relying on it.

`dist/` rules: each arch leg's `dist/` is uploaded as artifact
`release-assets-<arch>` (a tarball, so it is never empty), and
`release-check`'s job assembles all of them into one `dist/`, failing if an
arch's upload is missing or two legs produced the same file name, so
**per-arch file names must include the arch**. `release-check` may add
more files to `dist/`, and the assembled `dist/` (uploaded at the end of the
`release-check` job, before `finalize`) is what `release`,
`publish-packages` and `verify-published` start from. `finalize` may add
more files, but they are attached to the GitHub release only:
`publish-packages` and `verify-published` do not see them. A path that two
legs both produce (the same file name, a file in one and a directory of
that name in another, or a symlink) fails the assembly.
`release.yml` then runs `gh release create` with every file left in
`dist/`.

The arch legs run natively, not under QEMU: `arm64` on `ubuntu-24.04-arm`,
`amd64` on `ubuntu-24.04`, each overridable with the `CI_RUNNER_ARM64` /
`CI_RUNNER_AMD64` repository/organization variables.

This repo's own `Makefile` implements every target as a documented
no-op, since `template-base` ships no artifact — it's both a working
example of the contract's shape and what `release.yml` needs to run
cleanly if this repo itself is ever released. An instance's `Makefile` is
not template-owned (it's the whole point that it differs per artifact
type), so it's tracked like any other instance-owned file — untouched by
this template's own sync manifest.

## Template sync

Once instantiated, a repo created from this template can pull in later
template fixes/improvements via `.github/workflows/template-sync.yml`
(itself template-owned, `replace`-tier): on a schedule (cadence set by
the `TEMPLATE_SYNC_INTERVAL` repository variable, `weekly` or `monthly`)
or on demand, it diffs the instance against the template's latest tagged
release, per `.github/template-sync-manifest.yml`'s three tiers
(`replace`, `ignore`, `merge` — see that file's header), and opens a PR
with the result. It never pushes directly or auto-merges; a genuine
`merge`-tier conflict is left with `<<<<<<<` markers for a human to
resolve. An instance that predates this workflow bootstraps its
`.github/template-sync-state.json` via the workflow's manual
`initial_sync_tag`/`template_repo` inputs first.

The workflow runs `template_sync.py apply` from the *new* tag's checkout,
with that tag's manifest. Do the same when applying a sync by hand: the
instance's own copy of the script is the old release's, and an older one
does not expand glob entries such as `scripts/release-*.sh`, so it skips
those files (a second run with the new script then picks them up). `apply`
is meant to run once per tag pair: the state file moves to the new tag
afterwards. Running it again is harmless for files already merged cleanly,
but it merges a hand-resolved conflict again and re-creates its markers.

An instance that isn't itself a template shouldn't edit that manifest to
list its own files (`src/`, say): it's `merge`-tier, so edits would
conflict with later syncs. It lists them under an `ignore:` key in the
instance-owned
`.github/template-sync-manifest.local.yml` instead; the
`template-sync-manifest` check merges that file in, but sync never reads
it, and it accepts no other tier.

A repo can itself be both an instance of `template-base` *and* its own
template for further instances (e.g. `template-fastapi`): its own
`.github/template-sync-manifest.yml` classifies its *own* tracked files
for *its* downstream instances, entirely separate from this template's
manifest (it edits the manifest directly and resolves base's additions by
hand) — see that repo's own docs for the two-hop chain this produces.

## Template family

`template-base` is the root of a small family. Four mid-layer templates sit
under it, and each library or app is an instance of exactly one of them (the
three app templates through `template-docker`):

```text
template-base
├── template-python-package ── lib-crud-py, lib-observability-py, lib-crud-sdk-py
├── template-npm-package ───── lib-crud-sdk-ts, lib-crud-views-ts
├── template-rust-crate ────── lib-crud-rs, lib-observability-rs
└── template-docker
    ├── template-fastapi
    ├── template-axum
    └── template-react
```

Each mid-layer template is an instance of base that is itself a template. It
adds one kind of artifact to base's release contract and keeps everything
else from base:

| Template | Adds to base | Tooling it owns (`replace`) | What an instance owns (`ignore`) |
|---|---|---|---|
| `template-docker` | the container-image release: multi-arch build, SBOM, OCI labels, smoke test, Trivy scan, Dockerfile version check, and `publish-packages`/`verify-published` as no-ops | `Makefile`, `scripts/image-*.sh`, `scripts/runner-setup.sh`, `scripts/check-dockerfile-versions.sh`, `scripts/skopeo.sh` | `Dockerfile`, `image.mk`, `scripts/runner-setup.d/*.sh` |
| `template-python-package` | a pure-Python library on PyPI (trusted publishing) | `Makefile`, `scripts/release-*.sh`, `scripts/post-create.d/10-uv-sync.sh`, `codspeed.yml` | `pyproject.toml`, `uv.lock`, `src/`, `tests/` |
| `template-npm-package` | an ESM TypeScript package on npm (staged first publish, then trusted publishing) | `scripts/release-*.sh`, `scripts/develop.d/10-corepack.sh`, `scripts/post-create.d/10-pnpm-install.sh` | `package.json`, `tsconfig*.json`, `tsdown.config.ts`, `vitest.config.ts`, `src/`, `tests/`, `pnpm-lock.yaml`, and the `Makefile` (a thin caller of the scripts) |
| `template-rust-crate` | a library crate on crates.io (trusted publishing, bootstrap-token fallback) | `Makefile`, `scripts/release-*`, `rust-toolchain.toml`, `rustfmt.toml`, the rustup and `cargo fetch` hooks | `Cargo.toml`, `Cargo.lock`, `src/`, `tests/`, `deny.toml` |

Each template's own `docs/TEMPLATE.md` has the details and the exact
manifest; its `docs/adrs/0001-*.md` records why it exists. The three app
templates inherit the image layer from `template-docker`; they are still
the place for their own runtime (FastAPI, axum, React) and their own ADRs.

**Sync tiers across the family.** Every hop uses the same three tiers
(`replace`, `ignore`, `merge`; see "Template sync" and the manifest header).
A mid-layer classifies its own tree for its instances in its own
`.github/template-sync-manifest.yml`, which is `merge` tier in every
instance: base's additions are resolved by hand there.
An instance lists extra paths of its own under `ignore:` in
`.github/template-sync-manifest.local.yml`. Which template an instance
follows is recorded in its `.github/template-sync-state.json`
(`template_repo`, `synced_tag`). An instance follows its immediate parent: a
package or image instance its mid-layer, and the mid-layer follows base.

**Order of a change.** A sync reads the parent's latest tagged release, so a
change travels one released tag at a time:

1. Fix it in `template-base`, merge, and release a new tag.
2. Each mid-layer takes the base release through a sync PR, merges it and
   releases its own tag. A base fix reaches `template-docker` and the
   three package templates this way; the app templates take it from
   `template-docker` in step 3, after it has released.
3. Every instance takes its parent's tag through its own sync PR (the app
   templates take `template-docker`'s; the libraries, their package
   template's).

A fix to a mid-layer's own files starts at step 2 and skips base. A change to
the release contract itself (a new target, a changed job order) is a minor
release of base, and each mid-layer then adapts its own implementation of the
target before instances re-sync; the "Job order" above is the current
result of two such changes. Instances that publish packages follow the
registry's own ordering on top: a package that depends on another is
released after it.

## Versions and config

Every version and config value is defined in exactly one place; nothing
duplicates or re-pins it elsewhere. Everything pinned here (base image,
Actions, hook revisions, `prek`/Claude Code CLI/`snip`/`gh`/`uv`/Python/
Node.js versions) is pinned once, at its single point of use, to an exact
patch version — never a floating range or `latest` — so Renovate can
bump them one at a time and the diff shows exactly what changed. An
instance's own language runtime/package versions follow the same rule in
its own Dockerfile/manifest.

`python3` and Node.js, both installed by `scripts/develop.sh` and pinned
via `.devcontainer/Dockerfile`'s `PYTHON_VERSION`/`NODE_VERSION` ARGs, are
infrastructure tooling only — not an application runtime this template
assumes. `python3` exists because `prek` needs an interpreter to build
the venv for any `language: python` hook (`.pre-commit-config.yaml`'s
`pre-commit-hooks` repo) and because `.github/scripts/*.py` need one
directly; `uv` (its own `UV_VERSION` ARG) is only the mechanism used to
install that exact, checksum-verified CPython build rather than an
unpinned apt package, and stays on `PATH` afterward. Node.js exists
because `npx` (the `clear-thought` MCP server in `.mcp.json`) needs it.
An instance that adds Python or Node.js as its own application runtime
can reuse these directly rather than installing a second copy.

## Package template notes

`template-python-package` extends `template-base` for a pure-Python library.
The sections above still apply; these are the package-specific additions.

- **Package scaffold (instance-owned).** `pyproject.toml`, `uv.lock`, `src/`
  and `tests/` are `ignore` tier: a package renames the distribution and the
  import package (see the root `README.md`) and owns its dependencies, code,
  tests, benchmarks and import-linter contract. Tool *versions* are kept
  fresh by Renovate (`pep621` and `uv` managers, both on under
  `config:recommended`), not by sync.
- **Template-owned tooling (`replace`).** The `Makefile` (release contract:
  `uv build`, Syft SBOM, trusted-publishing `uv publish` in `publish-packages`),
  `scripts/release-check.sh`, `scripts/release-verify-published.sh`, `scripts/post-create.d/10-uv-sync.sh` and
  `.github/workflows/codspeed.yml`. Don't edit them in a package; change
  them here and release a new tag.
- **Checks.** The Python hooks in `.pre-commit-config.yaml` (`ruff`,
  `mypy --strict`, `import-linter`, `pytest` with the 100% coverage gate,
  `uv lock --check`, `pip-audit`) run through `uv run`, so
  `prek run --all-files --hook-stage manual` is the one command, locally and
  in `checks.yml`. Benchmarks live in `tests/bench/`, excluded from the
  default `pytest` run; run them with `uv run pytest tests/bench --no-cov`.
- **Adding backing services for integration tests.** A package that needs a
  service (for example `tool-crud` and its Postgres) uses the compose hook,
  not a new compose file wired into the base one: add an `include:` of a
  stack fragment under `.devcontainer/stack/`, plus the `environment`
  (connection URL, `UV_PROJECT_ENVIRONMENT` if the project directory is a
  bind mount) and `depends_on` for the `develop` service, to
  `.devcontainer/compose.instance.yml` (`ignore` tier, so it survives
  sync). Mark service-dependent tests with a pytest marker and keep them out
  of the default run unless the stack is up; `release-check` runs inside the
  devcontainer with the stack available, so it's the place to run them in
  the release. If the services need extra CLI tooling, add it as a
  `scripts/develop.d/NN-name.sh` hook and list its path under `ignore:` in
  `.github/template-sync-manifest.local.yml`.
- **CodSpeed.** `codspeed.yml` needs the CodSpeed GitHub app installed on the
  repository and nothing else for a public repo (OIDC); see
  `.github/workflows/README.md`.

## Code style

Every file gets a brief header stating what the file *is for* — one
line, sometimes two: a leading comment for most formats, a module
docstring where the language has one. Never describe the file's contents
in the header; that's what reading the file is for. Markdown files' own
title/opening line already serves this purpose. A format with no comment
syntax (`.json`) documents itself via the directory's `README.md`
instead.

A comment earns its place by saying something the code/config next to it
can't: *why* it's written this way, a non-obvious consequence, or a
constraint that isn't visible locally. Don't add a comment that just
restates what the following line already says in code — if removing a
comment loses no information, remove it. Every existing comment in this
repository follows this rule; keep new ones held to the same bar.

## Do

- Read `CLAUDE.md` and the `README.md` of every directory on the path to
  whatever you're changing before you change it.
- Open this repo in the devcontainer rather than assembling the
  toolchain by hand — it's the one environment this template guarantees.

## Don't

- Commit a `.env` file, or read one from application code — configuration
  lives in the compose files; secrets live in `.secrets/`.
- Add a `ports:` mapping or a `networks:` block to any file under
  `.devcontainer/`.
- Assume a language runtime, backing service, or release artifact exists
  in this template — every one of those is instance-owned.
