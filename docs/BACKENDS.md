# Completion backends

The completion step runs an external program. `SECOND_BRAIN_LLM_CMD` is the path of that program. `SECOND_BRAIN_GROK` is an alias of the same setting. If both are set and the values differ, the run stops with `llm_cmd_conflict` and does not start the program. The same text in both variables is one path. An empty value is unset.

Offline mode still runs only `tests/fakes/grok`, and it still refuses a symlink. A local server or a hosted endpoint is a live program.

A neutral built-in command protocol is pending a contract change. Until that change, the program is started with the project's current flags, including a fixed `--model` value. The adapter below accepts those flags. The model name sent to the endpoint is `SECOND_BRAIN_LLM_MODEL`.

The process that starts the program removes environment variables whose names end with `_API_KEY`, `_API_BASE`, or `_BASE_URL` before the program starts. Put the endpoint root in `SECOND_BRAIN_LLM_BASE`. Put the credential in a variable whose name does not end with those suffixes, and put that variable's name in `SECOND_BRAIN_LLM_KEY_ENV`.

The adapter reads the credential from the named variable, sends it only in an `Authorization` header, and does not write it to disk or print it. If the base URL, the model name, or the credential is missing, the adapter exits non-zero and does not send a request.

These five gates are still required before a live program starts:

- `offline` is false (`--live`)
- `live_authorized` is true
- `billing_path` is `session` or `api`
- `billing_path_verified` is true
- `tool_policy_verified` is true

## Local model server

Point `SECOND_BRAIN_LLM_CMD` at `scripts/adapters/chat_completions.py`.

```
SECOND_BRAIN_LLM_BASE=http://127.0.0.1:8080/v1
SECOND_BRAIN_LLM_MODEL=local-demo
SECOND_BRAIN_LLM_KEY_ENV=ENDPOINT_CREDENTIAL
```

Set `ENDPOINT_CREDENTIAL` in the environment to the credential the local server expects. The adapter posts to `http://127.0.0.1:8080/v1/chat/completions`.

The same five gates are required: `offline` false, `live_authorized` true, `billing_path` of `session` or `api`, `billing_path_verified` true, and `tool_policy_verified` true.

## Hosted chat-completions endpoint

Point `SECOND_BRAIN_LLM_CMD` at the same file, `scripts/adapters/chat_completions.py`.

```
SECOND_BRAIN_LLM_BASE=https://example.com/v1
SECOND_BRAIN_LLM_MODEL=hosted-demo
SECOND_BRAIN_LLM_KEY_ENV=ENDPOINT_CREDENTIAL
```

`https://example.com/v1` stands for the hosted endpoint root. The adapter posts to that root plus `/chat/completions`. Set `ENDPOINT_CREDENTIAL` to the credential that endpoint expects. The adapter does not store that value.

The same five gates are required: `offline` false, `live_authorized` true, `billing_path` of `session` or `api`, `billing_path_verified` true, and `tool_policy_verified` true.
