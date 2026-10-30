# tests/fixtures/

`manifest/` is a verbatim copy of `tests/fixtures/manifest/` from craftainer/lib-crud-py at
commit `1e0ad2d9366ba8ce2853e5c3f69ca7aa5244ed6b` (the shared manifest conformance fixtures,
`docs/manifest.md` "Conformance fixtures"). Do not edit them here; refresh them from that
repository.

`url-parity/` is the URL parity table shared with lib-crud-sdk-ts, vendored byte-identical from
craftainer/lib-crud-sdk-ts `tests/fixtures/` at commit `1cc61e92401872fb79924575e0da911afca6d885`.
`tests/unit/test_url_parity.py` runs every case through this client's public API (a mock transport
records the request) and asserts the same method and URL, or the same error class, with no request
sent on an error. Do not edit the files here (JSON cannot carry a header comment, so this note is
the header):

- `url-parity.json`, sha256 `168402d0c738f3f65042b937d6d512d6d03deda3664bb343977f5ba9453b2869`: the
  table. Its expected values were generated from this SDK's request planner (see its `provenance`).
- `url-parity.manifest.json`, sha256 `ce502f71cf55e003506f3d3b1d0993f649216b24b50bae3ba9b439eb9ed91b78`:
  the synthetic `gadgets`/`v3` manifest the table refers to. The table's `admin.manifest.json` is
  `manifest/admin.manifest.json`.

To refresh: copy both files from the sdk-ts repository's `tests/fixtures/`, put the new commit and
the new `sha256sum` values above (`test_fixture_matches_readme` fails when they disagree), and run
the suite. A failing case is a parity bug in one of the SDKs: fix it there, not by skipping the case.
