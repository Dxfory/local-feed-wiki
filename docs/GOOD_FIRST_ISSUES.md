# Good first issues

Each item is a small change with a file to edit and a test to add. Read [`CONTRIBUTING.md`](../CONTRIBUTING.md) first.

Do not edit `WikiPublisher.stage()`, `src/second_brain/models.py`, `src/second_brain/errors.py`, `src/second_brain/ports.py`, or the import allow-list. Put new checks in `config.py`, `cli.py`, `run.py`, or a script under `scripts/`.

## 1. `check` subcommand

`src/second_brain/cli.py` only implements `run`. Add `check`, which loads one config directory and prints the source count, each source id, its kind, and the classify and review budgets. It must not open a socket, start a completion process, or create `data/`.

Acceptance: `tests/test_cli.py` runs `check --example-config` against the public examples and `check --config-dir examples/offline` against the synthetic files. A bad config still exits with the existing error code on stderr. Update the exact help string in that test together with `_USAGE`.

## 2. Reject duplicate canonical URLs

`AppConfig` rejects a repeated source id and does not compare URLs. Two entries can share one feed.

In `src/second_brain/config.py`, after the `Source` objects are built, reject two sources whose `url` is identical. Error code: `duplicate_source_url`. Leave `models.py` alone.

Acceptance: a test loads two otherwise valid sources with the same https URL and expects `ConfigError("duplicate_source_url")`. Distinct URLs still load. The four public examples stay distinct.

## 3. Cap `backfill_days`

`Source.backfill_days` accepts any non-negative integer. A typo of several thousand days would widen the first fetch.

In `src/second_brain/config.py`, before the `Source` is constructed, reject `backfill_days` above 366 with `backfill_too_long`. Do not change `_budget` in `models.py`, so the other counters stay unbounded there.

Acceptance: 14 and 366 load; 367 raises `backfill_too_long`. Omitted days still default to 14.

## 4. Reject an empty source list

An empty `sources:` list currently becomes an empty tuple and the run continues.

In `src/second_brain/config.py`, raise `sources_required` when the list is empty.

Acceptance: a temp config with `sources: []` and the other example files fails with that code. `examples/offline` and `config/sources.example.yaml` still load.

## 5. Write `wiki/latest.txt` after publish

Readers have to open `wiki/index.md` to find the newest digest.

In `src/second_brain/run.py`, after a successful publish, write `wiki/latest.txt` containing the relative `html_path` from the report and a trailing newline. Do not add this write inside `WikiPublisher.stage()`. If publish did not produce an html path, do not create the file.

Acceptance: the offline demo leaves `wiki/latest.txt` equal to the `html=` value plus a newline. A failed run does not publish a stale pointer over a newer file. A symlink at `wiki/latest.txt` is refused with the existing symlink error, not followed.

## 6. Print failures and budgets on stdout

`_write_report` prints counts and the html path. The failed source ids and the configured budgets stay hidden.

Extend that one line in `src/second_brain/cli.py` with `failed_sources=<count>`, `classify_budget=<max_classify_batches>`, and `review_budget=<max_review_batches>`. Read the budgets from the loaded policy. Do not add fields to `RunReport`.

Acceptance: update every test that matches the full status line. The offline demo still exits 0, and a live run that stops on `live_not_authorized` still prints no status line.

## 7. OPML to stdout

Add `scripts/opml_to_sources.py`. It reads one local OPML file with the standard-library XML parser and writes a `sources:` YAML fragment to stdout. Use `xml` outline attributes for the title and the https URL. Skip outlines that are not http(s), and skip any URL whose query looks like a credential. Do not write `config/sources.yaml`. Do not add a dependency. Do not fetch the outlines.

Acceptance: `tests/test_opml.py` feeds a synthetic OPML with one https outline, one `http://` outline, and one query that contains `token`. Stdout has only the https source. The process never opens a socket.

## 8. Point the smoke script at `--root`

`scripts/live_smoke.sh` assigns `config.PROJECT_ROOT` inside a generated launcher, then calls `main`. The CLI now accepts `--root`.

Pass `--root` to `python -m second_brain.cli` and delete the launcher's assignment. Keep the temporary copy, the item cap, and the refusal to flip `live_authorized` or the verified flags. Keep the isolation checks in `tests/test_live_smoke.py`: the smoke root is not the checkout, and the checkout does not gain `data/smoke.sqlite`.

## 9. Document every runtime key

Add `docs/CONFIG.md` with one table row per key in `config/runtime.example.yaml`: meaning, the example value, and the code default where it differs. State the budget rule in one paragraph: classify batches default to 21 in the example because a batch holds at most 8 items, and review batches default to 8 because a batch holds at most 4 topics. State that `examples/offline/runtime.yaml` keeps 3 and 1 for the synthetic demo.

Acceptance: the table names `offline`, `live_authorized`, `billing_path`, `billing_path_verified`, `tool_policy_verified`, `review_llm`, `max_classify_batches`, `max_review_batches`, and `max_transcript_fetches`. No new config key.
