# scripts/

- `develop.sh` — `../.devcontainer/Dockerfile`'s `develop`-stage setup: installs `prek`,
  the Claude Code CLI, `snip` and the GitHub CLI (`gh`), the only tooling this template requires
  regardless of instance language. An instance adds its own language
  runtime/tooling install on top, either by extending this script or
  copying its shape into a second script. Prefer the hook below over
  either.
- `post-create.sh` — the devcontainer's `postCreateCommand`: git
  `safe.directory`, `prek install`, then every `post-create.d/*.sh`.
- `release-check.sh` — `make release-check`: installs the built wheel from
  `dist/` into a fresh venv and imports it; if `PYPI_PUBLISH` is set
  (`testpypi` or `pypi`), also installs `$RELEASE_VERSION` from that index,
  retrying with backoff while it propagates, and imports that too.
- `develop.d/`, `post-create.d/` — instance hook directories, empty apart
  from their own `README.md`; see `docs/TEMPLATE.md`'s "Instance extension
  points".

## Do

- Keep this script's own tools pinned to an exact version (a `# renovate:`
  ARG in `../.devcontainer/Dockerfile`) and checksum-verified where the upstream project
  publishes checksums, matching the pattern each install step here already
  follows.

## Don't

- Add a language runtime or stack-specific tool here — that belongs in the
  instance's own layer (`develop.d/`), not the base template's.
