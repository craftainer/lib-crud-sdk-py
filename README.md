# template-python-package

> [!NOTE]
> This repository is vibe coded with [Claude](https://claude.com/product/claude-code).

A template for a typed, tested, benchmarked and publishable Python library:
`src/` layout with a `py.typed` marker, hatchling build, `uv`, strict `ruff`
and `mypy`, `pytest` with a 100% coverage gate, `import-linter`,
`pip-audit`, per-PR CodSpeed benchmarks, and a release that publishes to
PyPI through trusted publishing. It is itself an instance of
[`template-base`](https://github.com/craftainer/template-base) (so it gets the
devcontainer, CI, release workflow and template sync from there) and is
itself a template: a package created from it receives fixes through template
sync. See [`docs/TEMPLATE.md`](docs/TEMPLATE.md) for the conventions shared
by every instance, and
[`docs/adrs/0001-python-package-template.md`](docs/adrs/0001-python-package-template.md)
for why it exists.

## Creating a package from this template

1. On GitHub, use "Use this template" on this repository (or
   `gh repo create <org>/<name> --template craftainer/template-python-package`).
2. Rename the distribution and the import package. Three names move
   together:
   - `pyproject.toml`: `[project] name` (`template-python-package`), the
     description, the import-linter `root_packages`/contract modules and
     any `--cov=src/template_python_package` setting;
   - `src/template_python_package/` to `src/<your_import_name>/`, and every
     `template_python_package` import in `tests/`;
   - this file's title and description.

   Then run `uv lock` to refresh `uv.lock` for the new name.
3. Point template sync at this template. `.github/template-sync-state.json`
   here records the `template-base` tag this template itself last synced
   from; the copy in a new package must instead name this repository and
   the tag it was created at (the latest release of
   `craftainer/template-python-package`):

   ```json
   {"template_repo": "craftainer/template-python-package", "synced_tag": "vX.Y.Z"}
   ```

   Add a `TEMPLATE_SYNC_TOKEN` secret too (see `.github/workflows/README.md`,
   "Template sync"). Files the package owns beyond the manifest's `ignore`
   tier (extra `scripts/post-create.d/` hooks, for instance) go in
   `.github/template-sync-manifest.local.yml` under `ignore:`.
4. Open the repository in the devcontainer, then run
   `prek run --all-files --hook-stage manual`; everything should pass before
   you write any code.
5. Keep `LICENSE` (GPL-3.0) or replace it with the license you choose.

## Publishing to PyPI

Releases use PyPI [trusted publishing](https://docs.pypi.org/trusted-publishers/)
(GitHub OIDC): no API token is stored anywhere. `make publish` is a no-op
until you opt in, per repository:

1. Register a "pending publisher" on the index before the first release
   (PyPI: Your account, Publishing, "Add a new pending publisher"; TestPyPI
   has its own, separate form on test.pypi.org). Enter the project name from
   `pyproject.toml`, the GitHub owner and repository, the workflow name
   `release.yml`, and leave the environment empty (or use the one
   `release.yml` runs under, if you add one).
2. Set the repository variable `PYPI_PUBLISH` (Settings, Secrets and
   variables, Actions, Variables):
   - unset: `make publish` prints a skip message and the release only
     attaches the sdist, wheel and SBOM to the GitHub release;
   - `testpypi`: publish to TestPyPI;
   - `pypi`: publish to PyPI.
3. Do the first release with `testpypi` as a dry run (a pre-release channel
   is enough), confirm that `make release-check` installs the published
   version back from TestPyPI, then register the PyPI pending publisher and
   switch the variable to `pypi`.

The version lives only in the git tag `release.yml` computes; `pyproject.toml`
stays at `0.0.0` and `make build` stamps `RELEASE_VERSION` into the build
without committing it.

## License

Licensed under the [GNU General Public License v3.0](LICENSE).
