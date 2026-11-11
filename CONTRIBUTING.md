# Contributing

Issues and pull requests are welcome in English or 中文.

## Setup

Python 3.13 or newer. Do not lower `requires-python`. Install only the pinned packages:

```bash
python -m pip install feedparser==6.0.14 PyYAML==6.0.3 pytest==9.2.0 setuptools
PYTHONPATH=src python -m pytest -q
```

The suite must stay offline. It must not call a feed host, a model API, ffmpeg, or a package index. `SECOND_BRAIN_GROK` in tests is `tests/fakes/grok`.

## Pull requests

- Describe the behavior change and the test that locks it.
- Do not add a runtime dependency, a web framework, a vector database, Whisper, yt-dlp, Torch, or a player.
- Do not commit `config/sources.yaml`, `config/preferences.md`, `config/topic_aliases.yaml`, `config/runtime.yaml`, `data/`, `raw/`, `wiki/`, `.grok-sandbox/`, or `.grok-tmp/`.
- Do not commit credentials, private notes, or feed URLs that carry a token. Public examples belong in `config/sources.example.yaml` and must be plain https URLs.
- Do not put personal names, home-directory paths, or machine-specific roots in the tree.

## Leave these gates intact

These are load-bearing. A pull request that weakens them will not be merged.

- Live authorization: `live_authorized`, `billing_path` of `session` or `api`, `billing_path_verified`, and `tool_policy_verified` must all be set before a live process starts.
- Symlink refusal for the project root, the database, the lock, and fixture paths.
- The offline binary must be the same file as `tests/fakes/grok` and must not be a symlink.
- `WikiPublisher.stage()` in `src/second_brain/wiki/compile.py`. Add a later step in `src/second_brain/run.py` instead of changing that method.
- The import allow-list and module list in `tests/test_boundaries.py` and `tests/conftest.py`. A new import that the allow-list rejects needs a contract change, not a quieter test.
- `src/second_brain/models.py`, `src/second_brain/errors.py`, and `src/second_brain/ports.py` are the frozen contract. Extend behavior in `config.py`, `cli.py`, or `run.py` when the existing types already carry the data.

`AGENTS.md` is the short working agreement. `docs/BUILD_PLAN_CN_v3.md` is the longer implementation contract. `docs/DECISIONS.md` is append-only.

## Good first issues

See [`docs/GOOD_FIRST_ISSUES.md`](docs/GOOD_FIRST_ISSUES.md).
