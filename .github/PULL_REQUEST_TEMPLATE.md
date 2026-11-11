## What changed

## How to test

```bash
PYTHONPATH=src python -m pytest -q
```

## Checklist

- [ ] Tests cover the new behavior and stay offline
- [ ] No new runtime dependency
- [ ] No credentials, private notes, or tokenized feed URLs
- [ ] Live authorization, symlink refusal, and the offline fake binary are unchanged
- [ ] `WikiPublisher.stage()` and the import allow-list are unchanged
- [ ] `config/sources.yaml` and local `data/`, `raw/`, and `wiki/` are not in the diff
