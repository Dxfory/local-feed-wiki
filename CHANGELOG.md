# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Entries are added here as changes merge.

## [0.3.0] - 2026-11-11

First public version.

### Added

- Offline pipeline for public RSS, podcast RSS, YouTube channel Atom, and synthetic file sources. `python3 -m second_brain.cli run` (console script `second-brain`) accepts `--offline` or `--live`, `--example-config`, `--config-dir`, `--db`, `--now`, and `--root`. `--now` is accepted only with `--offline`. Offline mode rejects every non-file source.
- Immutable originals under `raw/`. A repeat of the same bytes does not rewrite the file. The catalog is a SQLite database; the default path is `data/second_brain.sqlite`.
- Classification batches of at most 8 items into must, recommend, and skip, shown in the digest as 必读, 推荐阅读, and 不推荐. When several sources touch one topic, review batches hold at most 4 topics. `config/runtime.example.yaml` budgets 21 classify batches and 8 review batches. The code defaults are 3 and 1. `examples/offline/runtime.yaml` uses those defaults for the synthetic demo.
- Static HTML digest at `wiki/digest/<run_id>.html`, `wiki/index.md`, and a snapshot under `wiki/snapshots/<run_id>/` with `manifest.json`, topic pages, and minority pages. The digest is a self-contained page: untrusted text is escaped, and no script, image, font, or stylesheet is emitted. A run prints `status`, `classified`, `inserted`, `updated`, `pending`, `deferred`, and `html`.
- Minority pages place sentences already in the library side by side. The page compares that wording. It is not a market conclusion, a position, or a target price.
- Items with audio and no text stay pending for a transcript. Transcript text (plain, VTT, or SRT) can be fetched up to the configured limit, which defaults to 20. Audio enclosures are not requested.
- A run lock at `data/run.lock`. A second run stops with `run_locked` before migrate or fetch.
- Public URL checks reject non-global addresses, localhost, metadata host names, userinfo, credential query keys, and a fixed set of video-site hosts. They do not resolve DNS, so they are not a full SSRF defense. The HTTP client follows redirects itself, at most three requests, and checks each target again. The transport does not inherit proxy environment variables.
- Live runs stay closed unless `offline` is false, `live_authorized` is true, `billing_path` is `session` or `api`, `billing_path_verified` is true, and `tool_policy_verified` is true. Missing any one of those stops the process before a request and before a completion call. The checked-in example leaves every live flag closed.
- Offline completion accepts only the regular file `tests/fakes/grok` selected by `SECOND_BRAIN_GROK`. That file must not be a symlink. The offline demo does not open a socket.
- Symlink refusal for the project root, the database, the lock, fixture paths, and the run directories. The project root is `--root`, else a non-empty `SECOND_BRAIN_ROOT`, else the checkout that contains `src/second_brain`. A relative override is resolved from the working directory.
- Local run state lives in `data/`, `raw/`, `wiki/`, `.grok-sandbox/`, and `.grok-tmp/`. Those paths are gitignored. `--example-config` reads the tracked `*.example.*` files and does not write `config/sources.yaml`.
- Python 3.13 or newer. Runtime dependencies are `feedparser==6.0.14` and `PyYAML==6.0.3`. The test dependency is `pytest==9.2.0`. The suite stays offline. GitHub Actions runs that suite on Python 3.13 and on Python 3.13.5.
- MIT license.

[Unreleased]: https://github.com/Dxfory/local-feed-wiki/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/Dxfory/local-feed-wiki/releases/tag/v0.3.0
