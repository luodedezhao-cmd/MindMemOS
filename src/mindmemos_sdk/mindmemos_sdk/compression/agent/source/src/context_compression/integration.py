"""Opt-in, per-container OpenClaw routing for the compression proxy."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit
import uuid

from .types import CompressionConfig


logger = logging.getLogger(__name__)
PROVIDER_NAME = "context-compression"
PROXY_PORT = 18181
RUNTIME_ROOT = "/root/wildclaw_context_runtime"
RUNTIME_CONFIG = f"{RUNTIME_ROOT}/runtime.json"
LOG_DIR = "/tmp/openclaw/context_compression"
LOOPBACK_ENV = (
    'export WILDCLAW_CONTEXT_RUNTIME="/root/wildclaw_context_runtime/runtime.json" && '
    'export NO_PROXY="127.0.0.1,localhost${NO_PROXY:+,$NO_PROXY}" && '
    'export no_proxy="127.0.0.1,localhost${no_proxy:+,$no_proxy}" && '
)


@dataclass(frozen=True)
class Upstream:
    base_url: str
    model: str
    api_key: str = field(repr=False)
    headers: dict[str, str] = field(repr=False)
    model_config: dict = field(repr=False)


def provider_model(native):
    """Translate resolved defaults to the pinned OpenClaw provider config schema."""
    result = {k: copy.deepcopy(v) for k,v in native.items() if k not in {'provider','baseUrl'}}
    # These resolved pi-ai fields are not accepted in OpenClaw's config schema.
    # Empty maps equal the native defaults; nonempty values require an explicit adapter.
    for key in ('reasoningEffortMap','openRouterRouting','vercelGatewayRouting'):
        if result.get('compat',{}).pop(key, {}) not in ({}, None):
            raise ValueError('Unsupported non-default native routing/effort mapping')
    return result


def bridge_install_command(task_id):
    # Never execute a .py file inside this package directly: its types.py would
    # shadow stdlib types on sys.path[0]. Use the package parent and module entry.
    return ['exec', '-w', RUNTIME_ROOT, task_id, 'python3', '-m',
            'context_compression.install_bridge']


def load_compression_config(path: Path) -> CompressionConfig:
    return CompressionConfig.from_dict(json.loads(path.read_text(encoding="utf-8")))


def _literal(value: object, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not value.strip() and not allow_empty):
        raise ValueError(f"Compression proxy requires a literal {label}")
    if "${" in value or any(char in value for char in ("\r", "\n", "\0")):
        raise ValueError(f"Compression proxy cannot safely resolve {label}")
    return value


def _base_url(value: object) -> str:
    value = _literal(value, "upstream base URL")
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in {"http", "https"} and parsed.hostname
                 and not parsed.username and not parsed.password
                 and not parsed.query and not parsed.fragment)
        port = parsed.port
    except ValueError:
        valid = False
        port = None
    if not valid:
        raise ValueError("Compression proxy requires an HTTP(S) base URL without credentials or query")
    if parsed.hostname in {"localhost", "127.0.0.1", "::1"} and port == PROXY_PORT:
        raise ValueError("Compression proxy upstream must not point to its own listener")
    if parsed.path.rstrip("/").endswith("/chat/completions"):
        raise ValueError("Compression proxy expects a base URL, not a chat/completions URL")
    return value.rstrip("/")


def resolve_upstream(
    model: str,
    models_config: dict | None,
    *,
    openrouter_base_url: str,
    openrouter_api_key: str,
) -> Upstream:
    """Resolve only identities/protocols we can forward without guessing."""
    provider_name, separator, model_id = model.partition("/")
    if not separator or not provider_name or not model_id or provider_name == PROVIDER_NAME:
        raise ValueError("Compression proxy requires an original provider/model identity")
    _literal(model, "model identity")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]*", model):
        raise ValueError("Compression proxy cannot safely resolve the model identity")
    if models_config is not None and not isinstance(models_config, dict):
        raise ValueError("Models config must be a JSON object")
    providers = (models_config or {}).get("providers", {})
    if not isinstance(providers, dict):
        raise ValueError("Models providers must be a JSON object")
    if PROVIDER_NAME in providers:
        raise ValueError(f"Models config uses reserved provider {PROVIDER_NAME}")

    if provider_name not in providers:
        if provider_name != "openrouter":
            raise ValueError("Compression proxy cannot resolve the selected provider from models config")
        return Upstream(
            base_url=_base_url(openrouter_base_url),
            model=model_id,
            api_key=_literal(openrouter_api_key, "OpenRouter API key"),
            headers={},
            model_config={"id": model_id, "name": model_id},
        )

    provider = providers[provider_name]
    if not isinstance(provider, dict) or not isinstance(provider.get("models"), list):
        raise ValueError("Compression proxy requires an explicit provider model list")
    matches = [item for item in provider["models"]
               if isinstance(item, dict) and item.get("id") == model_id]
    if len(matches) != 1:
        raise ValueError("Compression proxy requires exactly one matching model ID in its provider")
    selected = matches[0]
    protocol = selected.get("api", provider.get("api"))
    if protocol != "openai-completions":
        raise ValueError("Compression proxy only supports explicit openai-completions providers/models")
    if provider.get("auth") not in (None, "api-key") or provider.get("authHeader", True) is not True:
        raise ValueError("Compression proxy requires standard API-key Bearer authentication")
    raw_headers = provider.get("headers", {})
    if not isinstance(raw_headers, dict):
        raise ValueError("Compression proxy requires literal provider headers")
    headers = {}
    blocked_headers = {"authorization", "host", "content-length", "transfer-encoding", "connection"}
    for key, value in raw_headers.items():
        key = _literal(key, "header name")
        if key.lower() in blocked_headers or any(char.isspace() for char in key) or ":" in key:
            raise ValueError("Compression proxy cannot safely forward a reserved provider header")
        headers[key] = _literal(value, "header value", allow_empty=True)
    if selected.get("headers"):
        raise ValueError("Compression proxy cannot safely resolve model-specific headers")
    return Upstream(
        base_url=_base_url(provider.get("baseUrl")),
        model=model_id,
        api_key=_literal(provider.get("apiKey"), "provider API key"),
        headers=headers,
        model_config=copy.deepcopy(selected),
    )


def _docker(args: list[str], operation: str, *, input: str | None = None, timeout: float = 15):
    # Never include stderr/config contents in errors: providers may echo credentials.
    try:
        result = subprocess.run(
            ["docker", *args], input=input, capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Compression proxy {operation} failed ({type(exc).__name__})") from None
    if result.returncode:
        raise RuntimeError(f"Compression proxy {operation} failed")
    return result


_INSTALL_PROVIDER = """\
import json, os, pathlib, sys
path = pathlib.Path('/root/.openclaw/openclaw.json')
config = json.loads(path.read_text()) if path.exists() else {}
providers = config.setdefault('models', {}).setdefault('providers', {})
payload = json.load(sys.stdin)
name = payload['name']
if name in providers:
    raise ValueError('compression provider already exists')
providers[name] = payload['provider']
path.parent.mkdir(parents=True, exist_ok=True)
path.touch(mode=0o600, exist_ok=True)
os.chmod(path, 0o600)
path.write_text(json.dumps(config, ensure_ascii=False, indent=2))
"""

_HEALTH_CHECK = f"""\
import json, sys, time, urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({{}}))
expected_instance = sys.argv[1]
deadline = time.monotonic() + 8
while time.monotonic() < deadline:
    try:
        with opener.open('http://127.0.0.1:{PROXY_PORT}/health', timeout=0.4) as response:
            if response.status == 200:
                payload = json.loads(response.read(4096))
                if isinstance(payload, dict) and payload.get('instance_id') == expected_instance:
                    raise SystemExit(0)
    except Exception:
        pass
    time.sleep(0.1)
raise SystemExit(1)
"""

_SHUTDOWN = f"""\
import json, pathlib, urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({{}}))
request = urllib.request.Request('http://127.0.0.1:{PROXY_PORT}/shutdown', data=b'', method='POST')
try:
    expected_instance = json.loads(pathlib.Path('{RUNTIME_CONFIG}').read_text())['instance_id']
    with opener.open('http://127.0.0.1:{PROXY_PORT}/health', timeout=0.4) as response:
        payload = json.loads(response.read(4096))
        if (response.status != 200 or not isinstance(payload, dict)
                or payload.get('instance_id') != expected_instance):
            raise SystemExit(0)
    with opener.open(request, timeout=2.6) as response:
        status = json.loads(response.read(4096))
        if status.get('ledger_finalized') is not True:
            raise SystemExit(2)
        if status.get('compression_drained') is not True:
            raise SystemExit(3)
except Exception:
    raise SystemExit(2)
"""


def install_and_start_proxy(
    task_id: str,
    upstream: Upstream,
    compression: CompressionConfig,
    task_prompt: str,
) -> str:
    """Install an independent proxy; return the main-model alias only once ready."""
    instance_id = uuid.uuid4().hex
    runtime = {
        "instance_id": instance_id,
        "session_id": "chat",
        "upstream_base_url": upstream.base_url,
        "upstream_api_key": upstream.api_key,
        "upstream_model": upstream.model,
        "upstream_headers": upstream.headers,
        "compression": compression.to_dict(),
        "task": task_prompt,
        "port": PROXY_PORT,
        "log_dir": LOG_DIR,
        "main_upstream_timeout_seconds": None,
    }
    _docker(["exec", task_id, "mkdir", "-p", RUNTIME_ROOT, LOG_DIR], "directory setup")
    _docker(["exec", task_id, "chmod", "700", RUNTIME_ROOT], "private directory setup")
    with tempfile.TemporaryDirectory(prefix="wildclaw-context-") as staging:
        stage = Path(staging)
        package = stage / "context_compression"
        shutil.copytree(
            Path(__file__).parent, package,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        config_path = stage / "runtime.json"
        config_path.touch(mode=0o600)
        config_path.write_text(json.dumps(runtime, ensure_ascii=False), encoding="utf-8")
        _docker(["cp", str(package), f"{task_id}:{RUNTIME_ROOT}/"], "package copy")
        _docker(["cp", str(config_path), f"{task_id}:{RUNTIME_CONFIG}"], "runtime config copy")
    _docker(["exec", task_id, "chmod", "600", RUNTIME_CONFIG], "runtime config protection")
    model_config = copy.deepcopy(upstream.model_config)
    if set(model_config) == {'id', 'name'}:
        # Resolve the same unknown-model fallback as the original Host, rather
        # than letting a new provider silently pick different defaults.
        probe = _docker(['exec', task_id, 'node', f'{RUNTIME_ROOT}/context_compression/model_probe.mjs',
                         'openrouter', upstream.model], 'native model defaults')
        native = json.loads(probe.stdout.strip().splitlines()[-1])
        model_config = provider_model(native)
        _docker(['exec', '-i', task_id, 'python3', '-c',
                 "import sys; from pathlib import Path; Path('/tmp/openclaw/context_compression/native_model.json').write_text(sys.stdin.read())"],
                'record native defaults', input=json.dumps(native))
    _docker(bridge_install_command(task_id),
            "version-checked incremental message bridge installation")
    provider = {
        "name": PROVIDER_NAME,
        "provider": {
            "baseUrl": f"http://127.0.0.1:{PROXY_PORT}/v1",
            "apiKey": "context-compression-local",
            "api": "openai-completions",
            "models": [model_config],
        },
    }
    _docker(
        ["exec", "-i", task_id, "python3", "-c", _INSTALL_PROVIDER],
        "provider setup", input=json.dumps(provider, ensure_ascii=False),
    )
    try:
        _docker(
            ["exec", "-d", "-w", RUNTIME_ROOT, task_id, "/bin/sh", "-c",
             f"{LOOPBACK_ENV}exec python3 -m context_compression.proxy --config {RUNTIME_CONFIG} "
             f"> {LOG_DIR}/proxy.log 2>&1"],
            "startup",
        )
        _docker(
            ["exec", task_id, "python3", "-c", _HEALTH_CHECK, instance_id],
            "health check", timeout=10,
        )
    except Exception:
        stop_proxy(task_id)
        raise
    logger.info("[%s] Context compression proxy ready on container loopback", task_id)
    return f"{PROVIDER_NAME}/{upstream.model}"


def stop_proxy(task_id: str) -> None:
    """Cancel in-flight compression and allow bounded local ledger draining."""
    try:
        result = subprocess.run(
            ["docker", "exec", task_id, "python3", "-c", _SHUTDOWN],
            capture_output=True, text=True, timeout=4,
        )
        if result.returncode == 3:
            logger.warning("[%s] Compression ledger sealed with unfinished calls; usage remains unknown", task_id)
        elif result.returncode:
            logger.warning("[%s] Compression shutdown unavailable; container cleanup will stop it", task_id)
    except (OSError, subprocess.SubprocessError):
        logger.warning("[%s] Compression shutdown timed out/unavailable; container cleanup will stop it", task_id)
