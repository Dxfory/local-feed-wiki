Draft. Issues/posts are created by a maintainer; this file does not create anything.

# Issue seeds

Each section is one issue. It can be claimed on its own. Read [`CONTRIBUTING.md`](../CONTRIBUTING.md) first.

### Add a `check` subcommand

Context

`src/second_brain/cli.py` only implements `run`. Read [`CONTRIBUTING.md`](../CONTRIBUTING.md) first.

What to change

In `src/second_brain/cli.py`, add `check`. It loads one config directory and prints the source count, each source id, its kind, and the classify and review budgets. It must not open a socket, start a completion process, or create `data/`.

Put new checks in `config.py`, `cli.py`, `run.py`, or a script under `scripts/`.

Do not touch

- `WikiPublisher.stage()`
- `src/second_brain/models.py`
- `src/second_brain/errors.py`
- `src/second_brain/ports.py`
- the import allow-list

Acceptance

- `tests/test_cli.py` runs `check --example-config` against the public examples and `check --config-dir examples/offline` against the synthetic files.
- A bad config still exits with the existing error code on stderr.
- Update the exact help string in that test together with `_USAGE`.
- The suite must stay offline.

Labels: good first issue, enhancement

### Reject duplicate canonical URLs

Context

`AppConfig` rejects a repeated source id and does not compare URLs. Two entries can share one feed. Read [`CONTRIBUTING.md`](../CONTRIBUTING.md) first.

What to change

In `src/second_brain/config.py`, after the `Source` objects are built, reject two sources whose `url` is identical. Error code: `duplicate_source_url`. Leave `models.py` alone.

Put new checks in `config.py`, `cli.py`, `run.py`, or a script under `scripts/`.

Do not touch

- `WikiPublisher.stage()`
- `src/second_brain/models.py`
- `src/second_brain/errors.py`
- `src/second_brain/ports.py`
- the import allow-list

Acceptance

- Add a test that loads two otherwise valid sources with the same https URL and expects `ConfigError("duplicate_source_url")`.
- Distinct URLs still load.
- The four public examples stay distinct.
- The suite must stay offline.

Labels: good first issue, enhancement

### Cap `backfill_days`

Context

`Source.backfill_days` accepts any non-negative integer. A typo of several thousand days would widen the first fetch. Read [`CONTRIBUTING.md`](../CONTRIBUTING.md) first.

What to change

In `src/second_brain/config.py`, before the `Source` is constructed, reject `backfill_days` above 366 with `backfill_too_long`. Do not change `_budget` in `models.py`, so the other counters stay unbounded there.

Put new checks in `config.py`, `cli.py`, `run.py`, or a script under `scripts/`.

Do not touch

- `WikiPublisher.stage()`
- `src/second_brain/models.py`
- `src/second_brain/errors.py`
- `src/second_brain/ports.py`
- the import allow-list

Acceptance

- Add a test where 14 and 366 load, 367 raises `backfill_too_long`, and omitted days still default to 14.
- The suite must stay offline.

Labels: good first issue, enhancement

### Reject an empty source list

Context

An empty `sources:` list currently becomes an empty tuple and the run continues. Read [`CONTRIBUTING.md`](../CONTRIBUTING.md) first.

What to change

In `src/second_brain/config.py`, raise `sources_required` when the list is empty.

Put new checks in `config.py`, `cli.py`, `run.py`, or a script under `scripts/`.

Do not touch

- `WikiPublisher.stage()`
- `src/second_brain/models.py`
- `src/second_brain/errors.py`
- `src/second_brain/ports.py`
- the import allow-list

Acceptance

- Add a test where a temp config with `sources: []` and the other example files fails with `sources_required`.
- `examples/offline` and `config/sources.example.yaml` still load.
- The suite must stay offline.

Labels: good first issue, enhancement

### Write `wiki/latest.txt` after publish

Context

Readers have to open `wiki/index.md` to find the newest digest. Read [`CONTRIBUTING.md`](../CONTRIBUTING.md) first.

What to change

In `src/second_brain/run.py`, after a successful publish, write `wiki/latest.txt` containing the relative `html_path` from the report and a trailing newline. Do not add this write inside `WikiPublisher.stage()`. If publish did not produce an html path, do not create the file.

Put new checks in `config.py`, `cli.py`, `run.py`, or a script under `scripts/`.

Do not touch

- `WikiPublisher.stage()`
- `src/second_brain/models.py`
- `src/second_brain/errors.py`
- `src/second_brain/ports.py`
- the import allow-list

Acceptance

- Add a test where the offline demo leaves `wiki/latest.txt` equal to the `html=` value plus a newline.
- A failed run does not publish a stale pointer over a newer file.
- A symlink at `wiki/latest.txt` is refused with the existing symlink error, not followed.
- The suite must stay offline.

Labels: good first issue, enhancement

### Print failures and budgets on stdout

Context

`_write_report` prints counts and the html path. The failed source ids and the configured budgets stay hidden. Read [`CONTRIBUTING.md`](../CONTRIBUTING.md) first.

What to change

Extend that one line in `src/second_brain/cli.py` with `failed_sources=<count>`, `classify_budget=<max_classify_batches>`, and `review_budget=<max_review_batches>`. Read the budgets from the loaded policy. Do not add fields to `RunReport`.

Put new checks in `config.py`, `cli.py`, `run.py`, or a script under `scripts/`.

Do not touch

- `WikiPublisher.stage()`
- `src/second_brain/models.py`
- `src/second_brain/errors.py`
- `src/second_brain/ports.py`
- the import allow-list

Acceptance

- Update every test that matches the full status line.
- The offline demo still exits 0, and a live run that stops on `live_not_authorized` still prints no status line.
- The suite must stay offline.

Labels: good first issue, enhancement

### OPML to stdout

Context

A local OPML file should become a `sources:` fragment on stdout. Read [`CONTRIBUTING.md`](../CONTRIBUTING.md) first.

What to change

Add `scripts/opml_to_sources.py`. It reads one local OPML file with the standard-library XML parser and writes a `sources:` YAML fragment to stdout. Use `xml` outline attributes for the title and the https URL. Skip outlines that are not http(s), and skip any URL whose query looks like a credential. Do not write `config/sources.yaml`. Do not add a dependency. Do not fetch the outlines.

Put new checks in `config.py`, `cli.py`, `run.py`, or a script under `scripts/`.

Do not touch

- `WikiPublisher.stage()`
- `src/second_brain/models.py`
- `src/second_brain/errors.py`
- `src/second_brain/ports.py`
- the import allow-list

Acceptance

- Add `tests/test_opml.py`. It feeds a synthetic OPML with one https outline, one `http://` outline, and one query that contains `token`. Stdout has only the https source. The process never opens a socket.
- The suite must stay offline.

Labels: good first issue, enhancement

### Point the smoke script at `--root`

Context

`scripts/live_smoke.sh` assigns `config.PROJECT_ROOT` inside a generated launcher, then calls `main`. The CLI now accepts `--root`. Read [`CONTRIBUTING.md`](../CONTRIBUTING.md) first.

What to change

Pass `--root` to `python -m second_brain.cli` and delete the launcher's assignment. Keep the temporary copy, the item cap, and the refusal to flip `live_authorized` or the verified flags.

Put new checks in `config.py`, `cli.py`, `run.py`, or a script under `scripts/`.

Do not touch

- `WikiPublisher.stage()`
- `src/second_brain/models.py`
- `src/second_brain/errors.py`
- `src/second_brain/ports.py`
- the import allow-list

Acceptance

- Keep the isolation checks in `tests/test_live_smoke.py`: the smoke root is not the checkout, and the checkout does not gain `data/smoke.sqlite`.
- The suite must stay offline.

Labels: good first issue, enhancement

### Document every runtime key

Context

`config/runtime.example.yaml` is the key list to document. Read [`CONTRIBUTING.md`](../CONTRIBUTING.md) first.

What to change

Add `docs/CONFIG.md` with one table row per key in `config/runtime.example.yaml`: meaning, the example value, and the code default where it differs. State the budget rule in one paragraph: classify batches default to 21 in the example because a batch holds at most 8 items, and review batches default to 8 because a batch holds at most 4 topics. State that `examples/offline/runtime.yaml` keeps 3 and 1 for the synthetic demo.

Do not touch

- `WikiPublisher.stage()`
- `src/second_brain/models.py`
- `src/second_brain/errors.py`
- `src/second_brain/ports.py`
- the import allow-list

Acceptance

- The table names `offline`, `live_authorized`, `billing_path`, `billing_path_verified`, `tool_policy_verified`, `review_llm`, `max_classify_batches`, `max_review_batches`, and `max_transcript_fetches`.
- No new config key.
- The suite must stay offline.

Labels: good first issue, documentation
