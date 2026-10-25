# scripts/

- `develop.sh` — `../.devcontainer/Dockerfile`'s `develop`-stage setup: installs `prek`,
  the Claude Code CLI, `snip` and the GitHub CLI (`gh`), the only tooling this template requires
  regardless of instance language. An instance adds its own language
  runtime/tooling install on top, either by extending this script or
  copying its shape into a second script. Prefer the hook below over
  either. Every download goes through its `fetch` helper (bounded curl
  retries with backoff; GitHub URLs authenticated when the optional
  `github_token` BuildKit secret is present) and the installers that
  download for themselves through `retry`, so CI's shared-IP HTTP 429s
  don't fail the image build. Local builds need no token.
- `post-create.sh` — the devcontainer's `postCreateCommand`: git
  `safe.directory`, `prek install`, then every `post-create.d/*.sh`.
- `release-check.sh` — `make release-check`: installs the built wheel from
  `dist/` into a fresh venv and imports it. It gates the GitHub release and
  publishing, so it checks only the local artifact.
- `release-verify-published.sh` — `make verify-published`, which runs after
  `publish-packages` and only when a `*_PUBLISH` variable is set: if
  `PYPI_PUBLISH` is set (`testpypi` or `pypi`), installs `$RELEASE_VERSION`
  from that index into a fresh venv (plain `python3 -m venv` and `pip`: the
  job is a bare runner with no secrets), retrying with backoff while it
  propagates, and imports it. With `PYPI_PUBLISH` unset it does nothing.
- `develop.d/`, `post-create.d/` — instance hook directories, empty apart
  from their own `README.md`; see `docs/TEMPLATE.md`'s "Instance extension
  points".

## Do

- Pass a token to the build as a BuildKit secret (`github_token`), never
  a `--build-arg`/`ARG`: an ARG is baked into the image history.
- Keep this script's own tools pinned to an exact version (a `# renovate:`
  ARG in `../.devcontainer/Dockerfile`) and checksum-verified where the upstream project
  publishes checksums, matching the pattern each install step here already
  follows.

## Don't

- Add a language runtime or stack-specific tool here — that belongs in the
  instance's own layer (`develop.d/`), not the base template's.
