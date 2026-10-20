# craftainer-crud-sdk

> [!NOTE]
> This repository is vibe coded with [Claude](https://claude.com/product/claude-code).

Python client SDK for craftainer-crud APIs, built on
[`httpx2`](https://pypi.org/project/httpx2/): sync and async clients,
HTTP caching with pluggable stores, and server-sent events. Import name:
`craftainer_crud_sdk`; requires Python 3.14+.

```sh
pip install craftainer-crud-sdk            # in-memory cache only
pip install "craftainer-crud-sdk[redis]"   # adds the Redis cache store
```

It is an instance of
[`template-python-package`](https://github.com/craftainer/template-python-package)
(see [`docs/TEMPLATE.md`](docs/TEMPLATE.md) for the conventions shared by
every instance). The cross-module contracts live in
`src/craftainer_crud_sdk/interfaces.py`.

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
