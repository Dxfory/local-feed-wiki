# Security

Report a vulnerability through this repository's private security advisories. Do not open a public issue that includes a live feed URL, a token, an authorization file, or a private note.

In scope:

- A public URL check that accepts a non-public host, a credential in the query, or a path that escapes the fixture root.
- A symlink the process follows for the project root, the database, the lock, or a fixture.
- Offline mode opening a network connection or accepting a completion binary other than the checked-in fake.
- A live run that starts when authorization, the billing path, or either verified flag is still closed.
- A secret copied into `wiki/`, `raw/` names, stdout, or the SQLite log.

Out of scope for a private report, and already documented:

- The operator turning the live gates on and sending configured text to the completion binary.
- DNS rebinding. The client does not resolve names. A public-looking name can still point at a private address. Say so if you demonstrate a bypass of the checks that do exist, and leave the request details out of the public issue.

This tree does not ship credentials. Real source lists stay in the gitignored `config/sources.yaml`.
