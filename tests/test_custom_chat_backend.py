"""Tests for the generic OpenAI-compatible custom_chat backend."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
from collections.abc import Iterator
from dataclasses import fields
from typing import Any

import pytest


_CUSTOM_CONFIG_ENV_KEYS = (
    "BASE_URL",
    "API_KEY",
    "MODEL",
    "TEMPERATURE",
    "TIMEOUT_SECONDS",
    "MAX_TOKENS",
)
_ENV_KEYS = ("OPTIMIZER_BACKEND", "TARGET_BACKEND", "OPTIMIZER_DEPLOYMENT", "TARGET_DEPLOYMENT") + tuple(
    f"{prefix}CUSTOM_CHAT_{key}"
    for prefix in ("", "OPTIMIZER_", "TARGET_")
    for key in _CUSTOM_CONFIG_ENV_KEYS
)


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


class _UrlopenRecorder:
    def __init__(self, content: str = "custom answer") -> None:
        self.content = content
        self.calls: list[dict[str, Any]] = []

    def __call__(self, request: Any, timeout: float | None = None) -> _FakeResponse:
        request_data = request.data.decode("utf-8")
        self.calls.append(
            {
                "url": request.full_url,
                "headers": dict(request.header_items()),
                "payload": json.loads(request_data),
                "timeout": timeout,
            }
        )
        return _FakeResponse(
            {
                "choices": [
                    {
                        "message": {"content": self.content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 1,
                    "total_tokens": 3,
                },
            }
        )


class _OpenAIClientStub:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs


def _install_openai_stub() -> None:
    if "openai" in sys.modules or importlib.util.find_spec("openai") is not None:
        return
    openai_stub = types.ModuleType("openai")
    openai_stub.AzureOpenAI = _OpenAIClientStub
    openai_stub.OpenAI = _OpenAIClientStub
    sys.modules["openai"] = openai_stub


def _import_model_modules() -> tuple[Any, Any, Any]:
    _install_openai_stub()
    import skillopt.model as model_module
    from skillopt.model import backend_config, custom_chat_backend

    return model_module, backend_config, custom_chat_backend


def _snapshot_config(config: Any) -> dict[str, Any]:
    return {field.name: getattr(config, field.name) for field in fields(config)}


def _restore_config(config: Any, snapshot: dict[str, Any]) -> None:
    for key, value in snapshot.items():
        setattr(config, key, value)


@pytest.fixture(autouse=True)
def isolate_custom_state() -> Iterator[tuple[Any, Any]]:
    model_module, backend_config, custom_chat_backend = _import_model_modules()
    optimizer_config = _snapshot_config(custom_chat_backend.OPTIMIZER_CONFIG)
    target_config = _snapshot_config(custom_chat_backend.TARGET_CONFIG)
    optimizer_backend = backend_config.get_optimizer_backend()
    target_backend = backend_config.get_target_backend()
    env = {key: os.environ.get(key) for key in _ENV_KEYS}
    custom_chat_backend.reset_token_tracker()
    yield model_module, custom_chat_backend
    custom_chat_backend.reset_token_tracker()
    _restore_config(custom_chat_backend.OPTIMIZER_CONFIG, optimizer_config)
    _restore_config(custom_chat_backend.TARGET_CONFIG, target_config)
    backend_config.set_optimizer_backend(optimizer_backend)
    backend_config.set_target_backend(target_backend)
    for key, value in env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _record_urlopen(
    monkeypatch: pytest.MonkeyPatch,
    custom_chat_backend: Any,
    content: str = "custom answer",
) -> _UrlopenRecorder:
    recorder = _UrlopenRecorder(content)
    monkeypatch.setattr(custom_chat_backend.urllib.request, "urlopen", recorder)
    return recorder


def test_configure_custom_chat_uses_shared_and_role_overrides(
    monkeypatch: pytest.MonkeyPatch,
    isolate_custom_state: tuple[Any, Any],
) -> None:
    model_module, custom_chat_backend = isolate_custom_state
    recorder = _record_urlopen(monkeypatch, custom_chat_backend)

    model_module.configure_custom_chat(
        base_url="http://shared.example/v1",
        api_key="shared-key",
        model="shared-model",
        target_base_url="http://target.example/v1",
        target_api_key="target-key",
        target_model="target-model",
        target_temperature="",
    )
    model_module.set_target_backend("custom_chat")
    text, usage = model_module.chat_target("system", "user", max_completion_tokens=128, retries=1, timeout=10.0)

    assert text == "custom answer"
    assert usage["total_tokens"] == 3
    call = recorder.calls[0]
    assert call["url"] == "http://target.example/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer target-key"
    assert call["payload"]["model"] == "target-model"
    assert "temperature" not in call["payload"]
    assert call["timeout"] == 10.0


def test_custom_chat_omits_authorization_header_when_api_key_empty(
    monkeypatch: pytest.MonkeyPatch,
    isolate_custom_state: tuple[Any, Any],
) -> None:
    model_module, custom_chat_backend = isolate_custom_state
    recorder = _record_urlopen(monkeypatch, custom_chat_backend)

    model_module.configure_custom_chat(
        base_url="http://custom.example/v1",
        api_key="",
        model="custom-model",
    )
    model_module.set_optimizer_backend("custom_chat")
    model_module.chat_optimizer("system", "user", retries=1)

    assert "Authorization" not in recorder.calls[0]["headers"]
    assert recorder.calls[0]["payload"]["model"] == "custom-model"


def test_custom_chat_tracks_tokens_and_reset_clears_summary(
    monkeypatch: pytest.MonkeyPatch,
    isolate_custom_state: tuple[Any, Any],
) -> None:
    model_module, custom_chat_backend = isolate_custom_state
    _record_urlopen(monkeypatch, custom_chat_backend)

    model_module.configure_custom_chat(base_url="http://custom.example/v1", model="custom-model")
    model_module.set_target_backend("custom_chat")
    model_module.chat_target("system", "user", stage="custom-stage", retries=1)

    summary = model_module.get_token_summary()
    assert summary["custom-stage"]["calls"] == 1
    assert summary["custom-stage"]["total_tokens"] == 3

    model_module.reset_token_tracker()
    assert "custom-stage" not in model_module.get_token_summary()


def test_custom_chat_requires_base_url(isolate_custom_state: tuple[Any, Any]) -> None:
    model_module, _custom_chat_backend = isolate_custom_state

    model_module.configure_custom_chat(base_url="", model="custom-model")
    model_module.set_target_backend("custom_chat")

    with pytest.raises(RuntimeError, match="base_url is not configured"):
        model_module.chat_target("system", "user", retries=1)


def test_flatten_config_exposes_custom_chat_keys() -> None:
    from skillopt.config import flatten_config

    flat = flatten_config(
        {
            "model": {
                "backend": "custom_chat",
                "custom_chat_base_url": "http://custom.example/v1",
                "custom_chat_api_key": "secret",
                "custom_chat_model": "custom-model",
                "target_custom_chat_model": "target-model",
            }
        }
    )

    assert flat["model_backend"] == "custom_chat"
    assert flat["custom_chat_base_url"] == "http://custom.example/v1"
    assert flat["custom_chat_api_key"] == "secret"
    assert flat["custom_chat_model"] == "custom-model"
    assert flat["target_custom_chat_model"] == "target-model"
