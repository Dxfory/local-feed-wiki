# Publication audit

The history audit below was recorded on 2026-11-11. This file was rewritten on 2026-11-12 so the current tree does not repeat the strings that the history still holds. History was not rewritten.

## History verdict: not clean

This repository's existing history cannot be published. It was not rewritten, and this change does not rewrite it. The repository stays private. A later public repository has to be a new snapshot of a publishable tree, not a clone of these commits.

### Personal name

Commit `012525ce0e90c311fa0a7eb76485d205e9c9846a` introduced `docs/BUILD_PLAN_CN_v3.md`. Blob `b7aca2393f64e4e1e89fd0eb3e5ecfbb9053d6f8` contains a legal personal name. At the 2026-11-11 audit that blob was still reachable from every commit on the repository. The spelling is omitted from this tree on purpose. Deleting it from later files does not delete the blob.

The same legal name is the Git author of these merge commits. The commit objects are unchanged:

- `b36a575a99dd8ea39f6da4253e130f6c757e0862`
- `3efb03c2ce577fff4ad419d2b779f84a0af01e5e`
- `fc3d6f6ec566afd20af146d3541b649b80b1df8e`
- `6a547434a3963f00d68b3b20dad9ae81d55bf7b2`
- `82ba0581946b0b7f47262c3efa9f58ade632b840`
- `156e9d14faf73edd4ca7c9fadb84cd48acfb4fa0`
- `0d2092a4262b796b1927abd89bbb1a2abfb0eb98`

### Paths and private notes

History also contains a home-directory path of a completion binary and a hardcoded absolute project root. This tree deletes both literals.

History contains private research notes. This tree deletes these paths. They remain reachable from older commits:

- `docs/REPOS_CN.md`
- `docs/REVIEW_GROK_CN.md`
- `docs/PIPELINE_CN.md`
- `docs/ACCEPTANCE_V3_CN.md`
- `docs/BUILD_REPORT_CN.md`
- `docs/CHANGES_V2_TO_V3_CN.md`

Those notes included a public profile link and a public social-account id. The strings are not repeated here.

### What the history scan did not find

gitleaks 8.28.0 and trufflehog 3.90.6, plus a walk of every blob, found no API key, cookie value, authorization-file body, or private feed URL. Scanner noise was a sentinel string in tests and fixture addresses on `example.com`. Commit metadata also contains noreply addresses and automation identities. Those addresses are not repeated in this tree. Finding no secret does not make the history publishable.

## This snapshot

The current tree removes the personal name, the home-directory path, the hardcoded project root, the private notes, and real source credentials from the files at HEAD. `config/sources.yaml` stays gitignored. `config/sources.example.yaml` lists four public https feeds. The project root is `--root`, else `SECOND_BRAIN_ROOT`, else the checkout directory.

That makes HEAD copyable into a new repository. It does not make this repository's history clean.

## Clean snapshot audit

Scanned on 2026-11-12. Scope is the working tree at HEAD after the edits in this change, not the commit history. History stays not clean. See the verdict above.

### Scanners

- gitleaks 8.28.0, `gitleaks detect --no-git` on this tree: 1 finding. Rule `generic-api-key` at `tests/test_text.py` line 322. The string is the fixture `SENTINEL_wp3_secret`, used to prove a parser error does not echo its input. It is not a credential.
- trufflehog 3.90.6, `trufflehog filesystem` on this tree, `.git` excluded: 0 verified secrets, 2 unverified hits. Both are the userinfo fixture `https://user:SUPERSECRET@example.com/a` in `tests/test_models.py` (`test_canonical_url_keeps_semantic_query`). The test asserts the secret is absent from the error text.

### Grep

No match in the working tree for the legal name, for a home-directory prefix, for a hardcoded checkout root, or for the deleted research-note identifiers. No email address other than `user@example.com` and `SUPERSECRET@example.com`. No noreply address remains in the tree. No private key block, cloud token prefix, or real source credential.

Absolute paths that remain are synthetic fixture strings: media paths under a temp directory in parser tests, and `/opt/second-brain-tests/grok` in `tests/test_cli.py` as a binary that offline mode must reject. They are not a person's home directory and not a machine-specific checkout root.

### Verdict

HEAD is a publishable snapshot. This repository's history is not. Do not publish this history, and do not treat a clone of this repository as the public copy.
