"""Offline tests with a mocked SadCaptcha HTTP sender: no credits are consumed."""
from __future__ import annotations

import base64
import importlib.util
import json
import sys
import types
from pathlib import Path

import cv2
import numpy as np
import pytest

SOURCE = Path(__file__).resolve().parents[1] / "src/sarae_agent/monitoring/sadcaptcha.py"


class AgentError(Exception):
    def __init__(self, code, message, *args, **kwargs):
        super().__init__(message)
        self.code = code


class Store:
    def __init__(self):
        self.data = None

    def save(self, v):
        self.data = v

    def load(self):
        return self.data

    def exists(self):
        return self.data is not None

    def delete(self):
        self.data = None


@pytest.fixture

def provider_module(monkeypatch, tmp_path):
    root = types.ModuleType("sarae_agent")
    root.__path__ = []
    activation = types.ModuleType("sarae_agent.activation")
    activation.__path__ = []
    secrets = types.ModuleType("sarae_agent.activation.secrets")
    secrets.SecretFile = lambda name: Store()
    config = types.ModuleType("sarae_agent.config")
    config.paths = types.SimpleNamespace(root=tmp_path)
    config.read_json = lambda path, default=None: json.loads(path.read_text()) if path.exists() else default
    config.write_json = lambda path, data: path.write_text(json.dumps(data))
    errors = types.ModuleType("sarae_agent.errors")
    errors.AgentError = AgentError
    monitoring = types.ModuleType("sarae_agent.monitoring")
    monitoring.__path__ = []
    for module in [root, activation, secrets, config, errors, monitoring]:
        monkeypatch.setitem(sys.modules, module.__name__, module)
    spec = importlib.util.spec_from_file_location("sarae_agent.monitoring.sadcaptcha", SOURCE)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def image():
    pic = np.zeros((64, 80, 3), np.uint8)
    cv2.rectangle(pic, (8, 8), (70, 50), (250, 250, 250), -1)
    ok, png = cv2.imencode(".png", pic)
    assert ok
    return base64.b64encode(png.tobytes()).decode("ascii")


class Response:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


def test_shapes_sends_correct_schema(provider_module, tmp_path):
    sent = []
    def sender(url, **kwargs):
        sent.append((url, kwargs))
        return Response({"pointOneProportionX": 0.1, "pointOneProportionY": 0.3,
                         "pointTwoProportionX": 0.4, "pointTwoProportionY": 0.8})
    p = provider_module.SadCaptchaAnalyzer(secret_store=Store(), usage_file=tmp_path / 'usage.json', sender=sender)
    p.set_api_key('fake-token-12345')
    result = p.analyze('shapes', image())
    assert sent[0][0].endswith('/shapes')
    assert list(sent[0][1]['json']) == ['imageB64']
    assert sent[0][1]['params']['licenseKey'] == 'fake-token-12345'
    assert result['result']['pointTwoProportionX'] == .4
    assert p.status()['attempts'] == 1
    assert 'fake-token' not in str(result)


def test_puzzle_and_duplicate(provider_module, tmp_path):
    seen = []
    def sender(url, **kwargs):
        seen.append(kwargs)
        return Response({'slideXProportion': 0.6})
    p = provider_module.SadCaptchaAnalyzer(secret_store=Store(), usage_file=tmp_path / 'usage.json', sender=sender)
    p.set_api_key('fake-token-12345')
    assert p.analyze('puzzle', image(), image())['result']['slideXProportion'] == .6
    assert sorted(seen[0]['json']) == ['pieceImageB64', 'puzzleImageB64']
    with pytest.raises(AgentError) as err:
        p.analyze('puzzle', image(), image())
    assert err.value.code == 'SADCAPTCHA_DUPLICATE'
    assert p.status()['attempts'] == 1


def test_invalid_input_does_not_spend_credit(provider_module, tmp_path):
    p = provider_module.SadCaptchaAnalyzer(secret_store=Store(), usage_file=tmp_path / 'usage.json', sender=lambda *_a, **_kw: None)
    p.set_api_key('fake-token-12345')
    with pytest.raises(AgentError) as err:
        p.analyze('shapes', 'garbage')
    assert err.value.code == 'SADCAPTCHA_IMAGE_INVALID'
    assert p.status()['attempts'] == 0


def test_bad_response_spends_one_attempt(provider_module, tmp_path):
    p = provider_module.SadCaptchaAnalyzer(secret_store=Store(), usage_file=tmp_path / 'usage.json', sender=lambda *_a, **_kw: Response({'wrong': 5}))
    p.set_api_key('fake-token-12345')
    with pytest.raises(AgentError) as err:
        p.analyze('shapes', image())
    assert err.value.code == 'SADCAPTCHA_BAD_RESPONSE'
    assert p.status()['attempts'] == 1


def test_budget_rejects_without_request(provider_module, tmp_path):
    calls = []
    def sender(*args, **kwargs):
        calls.append(True)
        return Response({'slideXProportion': .5})
    p = provider_module.SadCaptchaAnalyzer(secret_store=Store(), usage_file=tmp_path / 'usage.json', sender=sender, max_attempts=0)
    p.set_api_key('fake-token-12345')
    with pytest.raises(AgentError) as err:
        p.analyze('puzzle', image(), image())
    assert err.value.code == 'SADCAPTCHA_BUDGET_EXCEEDED'
    assert calls == []
