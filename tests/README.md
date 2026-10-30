# tests/

Two independent suites:

- `unit/` — no external services. A plain `uv run pytest` collects only this
  directory and enforces 100% line and branch coverage of
  `src/craftainer_crud_sdk` (`[tool.coverage.report]` in
  `pyproject.toml`). `unit/test_url_parity.py` runs the URL parity table shared
  with lib-crud-sdk-ts (`fixtures/url-parity/`; refresh steps in
  `fixtures/README.md`).
- `bench/` — `pytest-benchmark` microbenchmarks, excluded from the default
  run (`--ignore=tests/bench` in `addopts`). Run locally with
  `uv run pytest tests/bench --no-cov`, or under CodSpeed with
  `uv run pytest tests/bench --no-cov --codspeed`.

Test code is type-checked under the same `mypy --strict` settings as `src/`
(the hook runs `uv run mypy src tests`), so tests carry full annotations too.
