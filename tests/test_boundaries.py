"""Offline tripwire, import layers, and the WP0 scope contract."""

from __future__ import annotations

import ast
import os
import socket
import subprocess
import sys
import tomllib
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

MODULES: tuple[tuple[str, str], ...] = (
    ("second_brain/__init__.py", "second_brain"),
    ("second_brain/errors.py", "second_brain.errors"),
    ("second_brain/models.py", "second_brain.models"),
    ("second_brain/ports.py", "second_brain.ports"),
    ("second_brain/store.py", "second_brain.store"),
    ("second_brain/storage/__init__.py", "second_brain.storage"),
    ("second_brain/storage/raw.py", "second_brain.storage.raw"),
    ("second_brain/urlguard.py", "second_brain.urlguard"),
    ("second_brain/http.py", "second_brain.http"),
    ("second_brain/parse/__init__.py", "second_brain.parse"),
    ("second_brain/parse/text.py", "second_brain.parse.text"),
    ("second_brain/parse/podcast.py", "second_brain.parse.podcast"),
    ("second_brain/parse/video.py", "second_brain.parse.video"),
    ("second_brain/llm/__init__.py", "second_brain.llm"),
    ("second_brain/llm/grok_cli.py", "second_brain.llm.grok_cli"),
    ("second_brain/consensus/__init__.py", "second_brain.consensus"),
    ("second_brain/consensus/minority.py", "second_brain.consensus.minority"),
    ("second_brain/review/__init__.py", "second_brain.review"),
    ("second_brain/review/multi_source.py", "second_brain.review.multi_source"),
    ("second_brain/classify/__init__.py", "second_brain.classify"),
    ("second_brain/classify/buckets.py", "second_brain.classify.buckets"),
    ("second_brain/sources/__init__.py", "second_brain.sources"),
    ("second_brain/sources/rss.py", "second_brain.sources.rss"),
    ("second_brain/sources/youtube.py", "second_brain.sources.youtube"),
    ("second_brain/sources/ingest.py", "second_brain.sources.ingest"),
    ("second_brain/wiki/__init__.py", "second_brain.wiki"),
    ("second_brain/wiki/compile.py", "second_brain.wiki.compile"),
    ("second_brain/run.py", "second_brain.run"),
    ("second_brain/config.py", "second_brain.config"),
    ("second_brain/cli.py", "second_brain.cli"),
)

ALLOW: dict[str, frozenset[str]] = {
    "second_brain": frozenset(),
    "second_brain.errors": frozenset(),
    "second_brain.models": frozenset({"second_brain.errors"}),
    "second_brain.ports": frozenset({"second_brain.errors", "second_brain.models"}),
    "second_brain.storage": frozenset(),
    "second_brain.parse": frozenset(),
    "second_brain.llm": frozenset(),
    "second_brain.consensus": frozenset(),
    "second_brain.review": frozenset(),
    "second_brain.classify": frozenset(),
    "second_brain.sources": frozenset(),
    "second_brain.wiki": frozenset(),
    "second_brain.storage.raw": frozenset({"second_brain.errors", "second_brain.models"}),
    "second_brain.store": frozenset(
        {"second_brain.errors", "second_brain.models", "second_brain.ports", "second_brain.storage.raw"}
    ),
    "second_brain.urlguard": frozenset({"second_brain.errors", "second_brain.models"}),
    "second_brain.http": frozenset(
        {"second_brain.errors", "second_brain.models", "second_brain.ports", "second_brain.urlguard"}
    ),
    "second_brain.parse.text": frozenset({"second_brain.errors", "second_brain.models"}),
    "second_brain.parse.podcast": frozenset({"second_brain.errors", "second_brain.models"}),
    "second_brain.parse.video": frozenset({"second_brain.errors", "second_brain.models"}),
    "second_brain.llm.grok_cli": frozenset({"second_brain.errors", "second_brain.models"}),
    "second_brain.consensus.minority": frozenset({"second_brain.errors", "second_brain.models"}),
    "second_brain.review.multi_source": frozenset(
        {"second_brain.errors", "second_brain.models", "second_brain.ports"}
    ),
    "second_brain.classify.buckets": frozenset(
        {"second_brain.errors", "second_brain.models", "second_brain.ports"}
    ),
    "second_brain.sources.rss": frozenset(
        {
            "second_brain.errors",
            "second_brain.models",
            "second_brain.parse.text",
            "second_brain.parse.podcast",
            "second_brain.parse.video",
            "second_brain.urlguard",
        }
    ),
    "second_brain.sources.youtube": frozenset(
        {
            "second_brain.errors",
            "second_brain.models",
            "second_brain.parse.text",
            "second_brain.parse.video",
            "second_brain.urlguard",
        }
    ),
    "second_brain.sources.ingest": frozenset(
        {
            "second_brain.errors",
            "second_brain.models",
            "second_brain.ports",
            "second_brain.store",
            "second_brain.http",
            "second_brain.urlguard",
            "second_brain.parse.text",
            "second_brain.parse.podcast",
            "second_brain.parse.video",
            "second_brain.sources.rss",
            "second_brain.sources.youtube",
        }
    ),
    "second_brain.wiki.compile": frozenset(
        {
            "second_brain.errors",
            "second_brain.models",
            "second_brain.urlguard",
            "second_brain.consensus.minority",
            "second_brain.review.multi_source",
        }
    ),
    "second_brain.run": frozenset(
        {
            "second_brain.errors",
            "second_brain.models",
            "second_brain.ports",
            "second_brain.store",
            "second_brain.http",
            "second_brain.urlguard",
            "second_brain.parse.text",
            "second_brain.parse.podcast",
            "second_brain.parse.video",
            "second_brain.llm.grok_cli",
            "second_brain.consensus.minority",
            "second_brain.review.multi_source",
            "second_brain.classify.buckets",
            "second_brain.sources.rss",
            "second_brain.sources.youtube",
            "second_brain.sources.ingest",
            "second_brain.wiki.compile",
        }
    ),
    "second_brain.config": frozenset(
        {"second_brain.errors", "second_brain.models", "second_brain.urlguard"}
    ),
    "second_brain.cli": frozenset(
        {
            "second_brain",
            "second_brain.errors",
            "second_brain.models",
            "second_brain.ports",
            "second_brain.store",
            "second_brain.storage",
            "second_brain.storage.raw",
            "second_brain.urlguard",
            "second_brain.http",
            "second_brain.parse",
            "second_brain.parse.text",
            "second_brain.parse.podcast",
            "second_brain.parse.video",
            "second_brain.llm",
            "second_brain.llm.grok_cli",
            "second_brain.consensus",
            "second_brain.consensus.minority",
            "second_brain.review",
            "second_brain.review.multi_source",
            "second_brain.classify",
            "second_brain.classify.buckets",
            "second_brain.sources",
            "second_brain.sources.rss",
            "second_brain.sources.youtube",
            "second_brain.sources.ingest",
            "second_brain.wiki",
            "second_brain.wiki.compile",
            "second_brain.run",
            "second_brain.config",
        }
    ),
}

THIRD_PARTY: dict[str, frozenset[str]] = {
    "second_brain.sources.rss": frozenset({"feedparser"}),
    "second_brain.sources.youtube": frozenset({"feedparser"}),
    "second_brain.config": frozenset({"yaml"}),
}

TYPING_EXTRA: dict[str, frozenset[str]] = {
    "second_brain.errors": frozenset({"second_brain.models"}),
}

NETWORK_MODULES = frozenset({"second_brain.http"})
SUBPROCESS_MODULES = frozenset({"second_brain.llm.grok_cli"})
ENV_MODULES = frozenset({"second_brain.llm.grok_cli", "second_brain.cli"})
BANNED_STDLIB = frozenset(
    {
        "asyncio",
        "ctypes",
        "multiprocessing",
        "ftplib",
        "smtplib",
        "poplib",
        "imaplib",
        "nntplib",
        "telnetlib",
        "webbrowser",
        "xmlrpc",
    }
)
BANNED_PACKAGES = frozenset(
    {
        "flask",
        "django",
        "fastapi",
        "starlette",
        "uvicorn",
        "requests",
        "httpx",
        "aiohttp",
        "numpy",
        "torch",
        "whisper",
        "yt_dlp",
        "yt-dlp",
        "openai",
        "anthropic",
        "transformers",
        "sentence-transformers",
        "chromadb",
        "faiss",
        "playwright",
        "selenium",
    }
)

CLASS_HOME: dict[str, str] = {
    "ConfigError": "second_brain.errors",
    "UnsafeUrl": "second_brain.errors",
    "FramesNotAllowed": "second_brain.errors",
    "SchemaError": "second_brain.errors",
    "InvalidBucket": "second_brain.errors",
    "InvalidStance": "second_brain.errors",
    "IdentityCollision": "second_brain.errors",
    "FetchError": "second_brain.errors",
    "RateLimited": "second_brain.errors",
    "GrokUnavailable": "second_brain.errors",
    "BudgetExceeded": "second_brain.errors",
    "StorageError": "second_brain.errors",
    "PublishError": "second_brain.errors",
    "ConcurrentRun": "second_brain.errors",
    "Source": "second_brain.models",
    "SourceState": "second_brain.models",
    "Item": "second_brain.models",
    "Asset": "second_brain.models",
    "IngestRecord": "second_brain.models",
    "EntryIssue": "second_brain.models",
    "FeedBatch": "second_brain.models",
    "FetchResult": "second_brain.models",
    "ParsedPodcast": "second_brain.models",
    "VideoMeta": "second_brain.models",
    "Usage": "second_brain.models",
    "CompletionResult": "second_brain.models",
    "BatchOutcome": "second_brain.models",
    "Classification": "second_brain.models",
    "Claim": "second_brain.models",
    "ClassifiedItem": "second_brain.models",
    "ClassificationResult": "second_brain.models",
    "Opportunity": "second_brain.models",
    "ClaimAssessment": "second_brain.models",
    "ConsensusResult": "second_brain.models",
    "ReviewSentence": "second_brain.models",
    "TopicReview": "second_brain.models",
    "HttpResponse": "second_brain.models",
    "RateState": "second_brain.models",
    "FetchEvent": "second_brain.models",
    "LlmEvent": "second_brain.models",
    "Publication": "second_brain.models",
    "RunReport": "second_brain.models",
    "RuntimePolicy": "second_brain.models",
    "KeyPolicy": "second_brain.models",
    "AppConfig": "second_brain.models",
    "Clock": "second_brain.ports",
    "HttpTransport": "second_brain.ports",
    "HttpStateStore": "second_brain.ports",
    "CompletionFn": "second_brain.ports",
    "FixtureReader": "second_brain.ports",
}

GITIGNORE_REQUIRED = (
    "/raw/",
    "/wiki/",
    "/data/",
    "/.grok-sandbox/",
    "/.grok-tmp/",
    "/.venv/",
    "config/sources.yaml",
    "config/preferences.md",
    "config/topic_aliases.yaml",
    "config/runtime.yaml",
)

SOURCES_EXAMPLE = """# Public feeds only. Copy to config/sources.yaml and edit. That copy stays gitignored.
# Offline runs reject every non-file source. Use examples/offline for the synthetic demo.
sources:
  - id: hacker-news
    kind: rss
    url: https://hnrss.org/frontpage
    title: Hacker News
    backfill_days: 14
  - id: bbc-world
    kind: rss
    url: https://feeds.bbci.co.uk/news/world/rss.xml
    title: BBC World
    backfill_days: 14
  - id: planet-money
    kind: podcast_rss
    url: https://feeds.npr.org/510289/podcast.xml
    title: Planet Money
    backfill_days: 14
  - id: youtube-google-developers
    kind: youtube_rss
    url: https://www.youtube.com/feeds/videos.xml?channel_id=UC_x5XG1OV2P6uZZ5FSM9Ttw
    title: Google Developers
    backfill_days: 14
"""

PREFERENCE_HEADINGS = ("## 关注", "## 降权", "## 必读触发", "## 非共识约束")
KNOWN = {name for _path, name in MODULES}


def _is_stdlib(module: str) -> bool:
    top = module.split(".", 1)[0]
    return top in sys.stdlib_module_names or top == "__future__"


def _imported_names(current: str, node: ast.ImportFrom) -> list[str]:
    if any(alias.name == "*" for alias in node.names):
        return ["*"]
    if node.level:
        parts = current.split(".")
        if node.level > len(parts):
            return ["<bad-relative>"]
        base_parts = parts[: len(parts) - node.level]
        if node.module:
            base_parts.extend(node.module.split("."))
        base = ".".join(base_parts)
    else:
        base = node.module or ""
    found = [base] if base else []
    for alias in node.names:
        candidate = f"{base}.{alias.name}" if base else alias.name
        if candidate in KNOWN:
            found.append(candidate)
    return found


class _ImportScan(ast.NodeVisitor):
    def __init__(self, module: str) -> None:
        self.module = module
        self.runtime: set[str] = set()
        self.typing: set[str] = set()
        self.deferred = False
        self.star = False
        self._typing_depth = 0
        self._function_depth = 0

    def _store(self, names: list[str], *, typing_only: bool) -> None:
        if "*" in names:
            self.star = True
        target = self.typing if typing_only else self.runtime
        target.update(name for name in names if name and name != "*")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function_depth += 1
        self.generic_visit(node)
        self._function_depth -= 1

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        typing_only = (
            isinstance(node.test, ast.Name)
            and node.test.id == "TYPE_CHECKING"
            or (
                isinstance(node.test, ast.Attribute)
                and node.test.attr == "TYPE_CHECKING"
            )
        )
        if typing_only:
            self._typing_depth += 1
            for child in node.body:
                self.visit(child)
            self._typing_depth -= 1
            for child in node.orelse:
                self.visit(child)
            return
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        names = [alias.name for alias in node.names]
        if self._function_depth:
            self.deferred = True
        self._store(names, typing_only=self._typing_depth > 0 and self._function_depth == 0)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        names = _imported_names(self.module, node)
        if self._function_depth:
            self.deferred = True
        self._store(names, typing_only=self._typing_depth > 0 and self._function_depth == 0)


def _existing_modules() -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    for relative, module in MODULES:
        path = SRC / relative
        if path.is_file():
            found.append((relative, module, path.read_text(encoding="utf-8")))
    return found


def _walk_cycles(edges: dict[str, set[str]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def walk(node: str) -> None:
        if node in visited:
            return
        if node in visiting:
            raise AssertionError(f"import cycle at {node}")
        visiting.add(node)
        for nxt in edges.get(node, ()):
            if nxt.startswith("second_brain"):
                walk(nxt)
        visiting.remove(node)
        visited.add(node)

    for node in list(edges):
        walk(node)


def _call_names(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            found.add(func.attr)
            if isinstance(func.value, ast.Name):
                found.add(f"{func.value.id}.{func.attr}")
        elif isinstance(func, ast.Name):
            found.add(func.id)
    return found


def _names_used(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.Name):
            found.add(node.id)
    return found


def test_network_tripwire(tripwire) -> None:
    secret = "SUPERSECRET"
    url = f"http://203.0.113.1/?token={secret}"
    start = len(tripwire.events)
    checks = (
        ("socket.connect", socket.socket.connect, lambda sock: sock.connect(("127.0.0.1", 9))),
        ("socket.connect_ex", socket.socket.connect_ex, lambda sock: sock.connect_ex(("203.0.113.9", 9))),
        ("socket.create_connection", socket.create_connection, lambda _sock: socket.create_connection(("127.0.0.1", 9), timeout=0.01)),
        ("socket.getaddrinfo", socket.getaddrinfo, lambda _sock: socket.getaddrinfo("example.invalid", 80)),
        ("urllib.request.urlopen", urllib.request.urlopen, lambda _sock: urllib.request.urlopen(url, timeout=0.01)),
        (
            "urllib.request.OpenerDirector.open",
            urllib.request.OpenerDirector.open,
            lambda _sock: urllib.request.OpenerDirector().open("http://127.0.0.1/"),
        ),
        ("subprocess.run", subprocess.run, lambda _sock: subprocess.run(["/bin/true"], check=False)),
        ("subprocess.Popen", subprocess.Popen, lambda _sock: subprocess.Popen(["/bin/true"])),
        ("subprocess.call", subprocess.call, lambda _sock: subprocess.call(["/bin/true"])),
        ("subprocess.check_call", subprocess.check_call, lambda _sock: subprocess.check_call(["/bin/true"])),
        ("subprocess.check_output", subprocess.check_output, lambda _sock: subprocess.check_output(["/bin/true"])),
        ("os.system", os.system, lambda _sock: os.system("true")),
        ("open-secret", open, lambda _sock: open(ROOT / ".grok" / "auth.json", encoding="utf-8")),
        ("subprocess.run", subprocess.run, lambda _sock: subprocess.run(["grok"], check=False)),
        # tests/fakes/grok is the one allow-listed binary. A sibling path stays blocked.
        (
            "subprocess.run",
            subprocess.run,
            lambda _sock: subprocess.run([str(ROOT / "tests" / "fakes" / "not-grok")], check=False),
        ),
        ("subprocess.run", subprocess.run, lambda _sock: subprocess.run(f"echo {secret}", shell=True, check=False)),
    )
    sock = socket.socket()
    try:
        for kind, fn, action in checks:
            assert getattr(fn, "__tripwire__", False) is True
            with pytest.raises(RuntimeError, match="^network tripwire: ") as caught:
                action(sock)
            assert caught.value.args == (f"network tripwire: {kind}",)
            text = f"{caught.value}"
            assert secret not in text
            assert "auth.json" not in text
            assert "203.0.113" not in text
    finally:
        sock.close()
    happened = tripwire.events[start:]
    assert happened == [kind for kind, _fn, _action in checks]
    tripwire.accept(len(happened))


def test_import_layers() -> None:
    present = {path.relative_to(SRC).as_posix() for path in SRC.rglob("*.py")}
    known = {relative for relative, _module in MODULES}
    extra = sorted(present - known)
    assert extra == []
    missing_inits = [
        relative
        for relative, _module in MODULES
        if relative.endswith("__init__.py") and not (SRC / relative).is_file()
    ]
    assert missing_inits == []
    edges: dict[str, set[str]] = {}
    for relative, module, text in _existing_modules():
        if relative.endswith("__init__.py"):
            assert ast.parse(text).body == []
        tree = ast.parse(text)
        scan = _ImportScan(module)
        scan.visit(tree)
        assert scan.deferred is False
        assert scan.star is False
        allowed = ALLOW[module]
        typing_allowed = allowed | TYPING_EXTRA.get(module, frozenset())
        third = THIRD_PARTY.get(module, frozenset())
        for imported in scan.runtime | scan.typing:
            top = imported.split(".", 1)[0]
            assert top not in BANNED_STDLIB
            if imported.startswith("second_brain"):
                pool = allowed if imported in scan.runtime else typing_allowed
                assert imported in pool, f"{module} imports {imported}"
            elif _is_stdlib(imported):
                if imported == "urllib.request" or imported.startswith("urllib.request."):
                    assert module in NETWORK_MODULES
                if imported in {"socket", "ssl", "http.client"} or imported.startswith(
                    ("socket.", "ssl.", "http.client.")
                ):
                    assert module in NETWORK_MODULES
                if imported == "subprocess" or imported.startswith("subprocess."):
                    assert module in SUBPROCESS_MODULES
            else:
                assert top in third, f"{module} imports {imported}"
        calls = _call_names(tree)
        names = _names_used(tree)
        if "system" in calls or "popen" in calls or "execl" in names or "execv" in names:
            raise AssertionError(f"{module} calls a banned process helper")
        if module not in ENV_MODULES and ("environ" in names or "getenv" in names):
            raise AssertionError(f"{module} reads the environment")
        edges[module] = {name for name in scan.runtime if name.startswith("second_brain")}
    if (SRC / "second_brain" / "cli.py").is_file():
        missing = [relative for relative, _module in MODULES if not (SRC / relative).is_file()]
        assert missing == []
    _walk_cycles(edges)


def test_scope_contract() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    meta = project["project"]
    assert meta["requires-python"] == ">=3.13"
    assert meta["dependencies"] == ["feedparser==6.0.14", "PyYAML==6.0.3"]
    assert meta["optional-dependencies"]["dev"] == ["pytest==9.2.0"]
    assert meta["scripts"]["second-brain"] == "second_brain.cli:main"
    assert "license" not in meta
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in license_text
    assert "second-brain contributors" in license_text
    for dependency in meta["dependencies"]:
        name = dependency.split("==", 1)[0].lower()
        assert name not in BANNED_PACKAGES
    runtime = (ROOT / "config" / "runtime.example.yaml").read_text(encoding="utf-8")
    values = {}
    for line in runtime.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, raw = stripped.split(":", 1)
        values[key.strip()] = raw.strip()
    assert values == {
        "offline": "true",
        "live_authorized": "false",
        "billing_path": "test",
        "billing_path_verified": "false",
        "tool_policy_verified": "false",
        "review_llm": "true",
        "max_classify_batches": "21",
        "max_review_batches": "8",
        "max_transcript_fetches": "20",
    }
    assert "21 sources" in runtime
    assert "8 items" in runtime
    sources = (ROOT / "config" / "sources.example.yaml").read_text(encoding="utf-8")
    assert sources == SOURCES_EXAMPLE
    aliases = (ROOT / "config" / "topic_aliases.example.yaml").read_text(encoding="utf-8")
    assert "topic_aliases: {}" in aliases
    assert "claim_aliases: {}" in aliases
    preferences = (ROOT / "config" / "preferences.example.md").read_text(encoding="utf-8")
    for heading in PREFERENCE_HEADINGS:
        assert heading in preferences.splitlines()
    assert "已读过的发布说明降权" in preferences
    assert "不是买卖建议" in preferences
    for path in (ROOT / "config").iterdir():
        if not path.is_file():
            continue
        lowered = path.read_text(encoding="utf-8").lower()
        for banned in ("api_key", "cookie", "password", "authorization", "bearer"):
            assert banned not in lowered, path.name
        if path.name == "sources.example.yaml":
            assert "http://" not in lowered
            assert "https://" in lowered
        else:
            assert "http://" not in lowered and "https://" not in lowered, path.name
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for required in GITIGNORE_REQUIRED:
        assert required in ignore
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f" in agents
    assert "designed to be copy pasted" not in agents.lower()
    assert len(agents) < 12000
    homes: dict[str, list[str]] = {}
    for _relative, module, text in _existing_modules():
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ClassDef):
                homes.setdefault(node.name, []).append(module)
    for name, home in CLASS_HOME.items():
        assert homes.get(name) == [home]
    for name in ("Store", "HttpClient", "WikiPublisher", "RawArchive", "BudgetedCompletion"):
        assert len(homes.get(name, [])) <= 1
    from second_brain.models import RuntimePolicy

    policy = RuntimePolicy()
    assert policy.live_authorized is False
    assert policy.offline is True
    if (SRC / "second_brain" / "cli.py").is_file():
        missing = [relative for relative, _module in MODULES if not (SRC / relative).is_file()]
        assert missing == []
