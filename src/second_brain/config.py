"""Strict local configuration and synthetic fixture reads.

Paths stay under the caller's fixture root. ``PROJECT_ROOT`` is the checkout
that contains this package. The CLI may override it with ``--root`` or
``SECOND_BRAIN_ROOT``. This module does not read the environment, open a
network connection, or write a config file.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from second_brain.errors import ConfigError, UnsafeUrl
from second_brain.models import BILIBILI_HOSTS, AppConfig, KeyPolicy, RuntimePolicy, Source
from second_brain.urlguard import guard_public_url

def _default_project_root() -> Path:
    """Return the checkout that contains ``src/second_brain``."""

    return Path(__file__).resolve().parents[2]


PROJECT_ROOT = _default_project_root()

_MAX_CONFIG_BYTES = 1024 * 1024
_PART = re.compile(r"[A-Za-z0-9_.-]+")
_FIXTURE_PREFIX = "file:tests/fixtures/"
_SOURCE_FIELDS = frozenset(
    {"id", "kind", "url", "title", "backfill_days", "file_format", "transcript_hosts"}
)
_RUNTIME_FIELDS = (
    "offline",
    "live_authorized",
    "billing_path",
    "billing_path_verified",
    "tool_policy_verified",
    "review_llm",
    "max_classify_batches",
    "max_review_batches",
    "max_transcript_fetches",
)
_FILES = {
    "sources": ("sources.yaml", "sources.example.yaml"),
    "preferences": ("preferences.md", "preferences.example.md"),
    "aliases": ("topic_aliases.yaml", "topic_aliases.example.yaml"),
    "runtime": ("runtime.yaml", "runtime.example.yaml"),
}


class _ConfigLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate keys."""


def _construct_map(loader: yaml.SafeLoader, node: yaml.Node) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in mapping:
            raise ConfigError("duplicate_field")
        mapping[key] = loader.construct_object(value_node, deep=True)
    return mapping


_ConfigLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_map)


def load_config(config_dir: Path, *, offline: bool, example: bool = False) -> AppConfig:
    """Load one config directory.

    ``offline`` replaces only ``RuntimePolicy.offline``. Authorization and the
    two verified flags stay as written. ``example`` selects the tracked
    ``*.example.*`` names and does not write the real names.
    """

    if not isinstance(offline, bool) or not isinstance(example, bool):
        raise ConfigError("invalid_config")
    directory = _directory(config_dir)
    loaded = _load_yaml(_text(directory, "sources", example))
    sources = tuple(_source(item) for item in _source_items(loaded))
    preferences = _text(directory, "preferences", example)
    if "\x00" in preferences:
        raise ConfigError("invalid_preferences")
    keys = _keys(_load_yaml(_text(directory, "aliases", example)))
    runtime = _runtime(_load_yaml(_text(directory, "runtime", example)), offline=offline)
    return AppConfig(sources=sources, preferences=preferences, keys=keys, runtime=runtime)


def read_fixture(uri: str, *, fixture_root: Path) -> bytes:
    """Read one ``file:tests/fixtures/...`` URI that stays inside ``fixture_root``."""

    relative = _fixture_relative(uri)
    root = _directory(fixture_root, missing="invalid_fixture_root", bad="invalid_fixture_root")
    current = root
    for part in relative.split("/"):
        current = current / part
        if current.is_symlink() or not _inside(root, current):
            raise ConfigError("fixture_escape")
    if not current.exists():
        raise FileNotFoundError("missing_fixture")
    if not current.is_file():
        raise ConfigError("fixture_escape")
    try:
        return current.read_bytes()
    except OSError:
        raise FileNotFoundError("missing_fixture") from None


def _directory(path: Path, *, missing: str = "missing_config", bad: str = "invalid_config") -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise ConfigError(bad)
    if path.is_symlink():
        raise ConfigError("symlink_escape")
    if not path.exists():
        raise ConfigError(missing)
    if not path.is_dir():
        raise ConfigError("invalid_config")
    return path


def _filename(kind: str, example: bool) -> str:
    real_name, example_name = _FILES[kind]
    return example_name if example else real_name


def _text(directory: Path, kind: str, example: bool) -> str:
    path = directory / _filename(kind, example)
    if path.is_symlink() or not path.is_file():
        raise ConfigError("missing_config")
    try:
        data = path.read_bytes()
    except OSError:
        raise ConfigError("unreadable_config") from None
    if len(data) > _MAX_CONFIG_BYTES:
        raise ConfigError("config_too_large")
    try:
        return data.decode("utf-8")
    except UnicodeError:
        raise ConfigError("invalid_config") from None


def _load_yaml(text: str) -> object:
    try:
        documents = list(yaml.load_all(text, Loader=_ConfigLoader))
    except ConfigError:
        raise
    except yaml.YAMLError:
        raise ConfigError("invalid_yaml") from None
    if len(documents) != 1 or documents[0] is None:
        raise ConfigError("invalid_yaml")
    _plain(documents[0])
    return documents[0]


def _plain(value: object) -> None:
    if value is None or isinstance(value, str):
        if isinstance(value, str) and "\x00" in value:
            raise ConfigError("invalid_config")
        return
    if isinstance(value, (bool, int, float)):
        return
    if isinstance(value, list):
        for item in value:
            _plain(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or "\x00" in key:
                raise ConfigError("invalid_config")
            _plain(item)
        return
    raise ConfigError("invalid_config")


def _source_items(document: object) -> list[object]:
    if not isinstance(document, dict):
        raise ConfigError("invalid_config")
    extra = set(document) - {"sources"}
    if extra:
        raise ConfigError("unknown_field")
    raw = document.get("sources")
    if not isinstance(raw, list):
        raise ConfigError("invalid_config")
    return raw


def _source(raw: object) -> Source:
    if not isinstance(raw, dict):
        raise ConfigError("invalid_config")
    if set(raw) - _SOURCE_FIELDS:
        raise ConfigError("unknown_field")
    for name in ("id", "kind", "url", "title"):
        if not isinstance(raw.get(name), str):
            raise ConfigError("invalid_config")
    kwargs: dict[str, object] = {
        "id": raw["id"],
        "kind": raw["kind"],
        "url": raw["url"],
        "title": raw["title"],
    }
    if "backfill_days" in raw:
        days = raw["backfill_days"]
        if isinstance(days, bool) or not isinstance(days, int):
            raise ConfigError("invalid_budget")
        kwargs["backfill_days"] = days
    if "file_format" in raw and not isinstance(raw["file_format"], str):
        raise ConfigError("invalid_file_format")
    if "file_format" in raw:
        kwargs["file_format"] = raw["file_format"]
    if "transcript_hosts" in raw:
        hosts = raw["transcript_hosts"]
        if not isinstance(hosts, list) or any(not isinstance(host, str) for host in hosts):
            raise ConfigError("invalid_transcript_host")
        kwargs["transcript_hosts"] = tuple(hosts)
    source = Source(**kwargs)
    if source.kind != "file":
        try:
            guard_public_url(source.url)
        except UnsafeUrl as exc:
            raise ConfigError(exc.code) from None
        host = urlsplit(source.url).hostname
        if host in BILIBILI_HOSTS:
            raise ConfigError("bilibili_forbidden")
    return source


def _keys(document: object) -> KeyPolicy:
    if not isinstance(document, dict):
        raise ConfigError("invalid_config")
    expected = {"topic_aliases", "claim_aliases"}
    extra = set(document) - expected
    if extra:
        raise ConfigError("unknown_field")
    if set(document) != expected:
        raise ConfigError("invalid_config")
    return KeyPolicy(
        topic_aliases=_alias_map(document["topic_aliases"]),
        claim_aliases=_alias_map(document["claim_aliases"]),
    )


def _alias_map(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ConfigError("alias_type")
    mapped: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise ConfigError("alias_type")
        mapped[key] = item
    return mapped


def _runtime(document: object, *, offline: bool) -> RuntimePolicy:
    if not isinstance(document, dict):
        raise ConfigError("invalid_config")
    expected = set(_RUNTIME_FIELDS)
    extra = set(document) - expected
    if extra:
        raise ConfigError("unknown_field")
    if set(document) != expected:
        raise ConfigError("invalid_config")
    runtime = RuntimePolicy(
        offline=document["offline"],
        live_authorized=document["live_authorized"],
        billing_path=document["billing_path"],
        billing_path_verified=document["billing_path_verified"],
        tool_policy_verified=document["tool_policy_verified"],
        review_llm=document["review_llm"],
        max_classify_batches=document["max_classify_batches"],
        max_review_batches=document["max_review_batches"],
        max_transcript_fetches=document["max_transcript_fetches"],
    )
    if runtime.offline != offline:
        runtime = replace(runtime, offline=offline)
    return runtime


def _fixture_relative(uri: str) -> str:
    if not isinstance(uri, str) or not uri.startswith(_FIXTURE_PREFIX) or "\\" in uri or "\x00" in uri:
        raise ConfigError("invalid_fixture_url")
    relative = uri[len(_FIXTURE_PREFIX) :]
    if relative.startswith("/"):
        raise ConfigError("invalid_fixture_url")
    parts = relative.split("/")
    if not parts:
        raise ConfigError("invalid_fixture_url")
    for part in parts:
        if part in {"", ".", ".."}:
            raise ConfigError("fixture_escape")
        if _PART.fullmatch(part) is None:
            raise ConfigError("invalid_fixture_url")
    return relative


def _inside(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
