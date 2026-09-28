# tests/goldens/ -- committed CI golden for the offline verifier

`golden_bundle.json` is the frozen input for `tests/test_verifier_golden.py`. It packs the four
files a signed normalization-run bundle contains -- `run.json`, `inputs.json`, `result.json`,
`public-key.pem` -- into one JSON object (keys named after those files). The test unpacks a fresh
copy into a temp dir and runs the real `normalize.verify.verify_bundle`, which reads exactly those
four files.

## (Re)generate it

The golden bytes are produced on a developer machine and **committed** -- CI never generates them
(it has no signing key and runs no DB). To create or refresh the golden:

```
python tools/make_golden_bundle.py
git add tests/goldens/golden_bundle.json
git commit -m "test: commit verifier CI golden"
```

The generator uses a fixed key seed, a fixed tiny fixture, `seed=0` and a pinned lambda, so it
writes byte-identical output every run, and it self-verifies before writing (a broken golden fails
in the generator, not in CI).

## What the CI test asserts

- valid bundle -> **PASS** (signature valid, both content hashes match, and the ranking reproduces
  from the pinned inputs);
- one byte flipped in an input score -> **FAIL** (`inputs.json` no longer matches `inputs_hash`);
- one byte flipped in a published `q` -> **FAIL** (`result.json` no longer matches `result_hash`);
- one hex char flipped in the signature -> **FAIL** (`run signature valid` fails).

If `golden_bundle.json` is absent, the test module skips with a clear message, so CI stays green
until the real signed bytes are committed.
