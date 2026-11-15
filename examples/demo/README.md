# English offline demo

Four invented sources, ten invented items, and one shared proposition: whether the noon gate stays open in the inner basin during January 2030. Harbor Watch, Field Notes, and Quay Gazette say it stays open. North Ledger says it stays shut. Links are under `https://example.com/`, for example `https://example.com/north-ledger/noon-gate-shut`.

Item feeds live in `tests/fixtures/demo/` because a file source must use a `file:tests/fixtures/` path. The four feeds are `harbor-watch.xml`, `north-ledger.xml`, `field-notes.xml`, and `quay-gazette.xml`. Each item body starts with a plain sentence, then an `SBDEMO1` directive. The offline binary at `tests/fakes/grok` turns that directive into a topic, a bucket, and claims. A prompt with no `SBDEMO1` marker still gets the default empty claim list, so the existing offline example is unchanged. With the marker, the digest minority section lists a minority candidate and the snapshot writes one comparison page.

Digest labels are still Chinese. A separate change adds English labels.

## Run

From the checkout, with the pinned packages installed:

```bash
PYTHONPATH=src SECOND_BRAIN_GROK=tests/fakes/grok \
  python3 -m second_brain.cli run --offline \
  --config-dir examples/demo \
  --now 2030-01-15T00:00:00Z \
  --db data/second_brain.sqlite
```

That writes gitignored `data/`, `raw/`, and `wiki/` in the checkout. The clock is fixed at `2030-01-15T00:00:00Z`. Open `wiki/digest/<run-id>.html` and the matching file under `wiki/snapshots/<run-id>/opportunities/`.

## Subcommand

From a source checkout, with the pinned packages installed:

```bash
PYTHONPATH=src python3 -m second_brain.cli demo
```

`--out DIR` chooses the output directory. Omit it to use a new temporary directory. The directory must be missing or empty, and it must not be a symlink. The command copies this folder, `tests/fixtures/demo/`, and the offline binary into that root, then runs the offline pipeline with the clock fixed at `2030-01-15T00:00:00Z`. It prints the status line, the absolute digest HTML path, and the absolute `wiki/index.md` path. The checkout's `data/`, `raw/`, and `wiki/` stay untouched.

## Regenerate the GIF and screenshot

`scripts/make_demo.sh` repeats the run in a temporary directory, then writes `docs/assets/digest.png` and `docs/assets/demo.gif`. It needs `ffmpeg` and one of `chromium`, `chromium-browser`, or `google-chrome`. It does not install packages and it does not write `data/`, `raw/`, or `wiki/` in the checkout.

```bash
scripts/make_demo.sh
```
