import base64
import json

import httpx
import pytest

from conftest import windows_only
from sarae_agent.activation import dpapi
from sarae_agent.activation.secrets import credential_file
from sarae_agent.activation.service import ActivationService
from sarae_agent.cloud.client import CloudClient
from sarae_agent.config import paths, write_json
from sarae_agent.errors import AgentError

KEY = "SARAE-ACT-" + "k" * 40


def mock_cloud(handler):
    return CloudClient("https://cloud.example/api", transport=httpx.MockTransport(handler))


@windows_only
def test_dpapi_roundtrip_and_entropy():
    blob = dpapi.protect(b"secret")
    assert b"secret" not in blob
    assert dpapi.unprotect(blob) == b"secret"
    with pytest.raises(AgentError):
        dpapi.unprotect(blob, entropy=b"other-app")


@windows_only
def test_activation_persists_identity_protected(home):
    calls = []

    def handler(request: httpx.Request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"agent_id": "agt_123", "agent_credential": "cred-xyz", "owner_id": "o1"})

    service = ActivationService()
    status = service.activate("https://cloud.example/api", KEY, client=mock_cloud(handler))
    assert status["agent_id"] == "agt_123" and calls[0]["activation_key"] == KEY
    raw = credential_file.path.read_text(encoding="utf-8")
    assert "cred-xyz" not in raw and "agt_123" not in raw  # DPAPI blob only
    assert "cred-xyz" not in paths.config_file.read_text(encoding="utf-8") if paths.config_file.exists() else True
    reloaded = ActivationService()  # "restart": same agent_id comes back
    assert reloaded.identity.agent_id == "agt_123" and reloaded.identity.credential == "cred-xyz"
    with pytest.raises(AgentError) as exc:
        reloaded.activate("https://cloud.example/api", KEY, client=mock_cloud(handler))
    assert exc.value.code == "ALREADY_ACTIVATED"
    reloaded.deactivate_local()
    assert ActivationService().identity is None


@windows_only
@pytest.mark.parametrize("status,header,code", [
    (403, "ACTIVATION_KEY_USED", "ACTIVATION_KEY_USED"),
    (403, "ACTIVATION_KEY_INVALID", "ACTIVATION_KEY_INVALID"),
    (403, "LICENSE_EXPIRED", "LICENSE_EXPIRED"),
    (409, "AGENT_LIMIT_REACHED", "AGENT_LIMIT_REACHED"),
    (403, None, "ACTIVATION_KEY_INVALID"),   # older Cloud without X-Sarae-Error
    (500, None, "CLOUD_SERVER_ERROR"),
])
def test_activation_errors(home, status, header, code):
    def handler(_request):
        headers = {"X-Sarae-Error": header} if header else {}
        return httpx.Response(status, json={"detail": "Mã kích hoạt không hợp lệ"}, headers=headers)

    service = ActivationService()
    with pytest.raises(AgentError) as exc:
        service.activate("https://cloud.example/api", KEY, client=mock_cloud(handler))
    assert exc.value.code == code
    assert service.identity is None and not credential_file.exists()


def test_activation_offline_and_bad_key(home):
    def offline(_request):
        raise httpx.ConnectError("no route")

    service = ActivationService()
    with pytest.raises(AgentError) as exc:
        service.activate("https://cloud.example/api", KEY, client=mock_cloud(offline))
    assert exc.value.code == "CLOUD_UNREACHABLE"
    with pytest.raises(AgentError) as exc:
        service.activate("https://cloud.example/api", "short")
    assert exc.value.code == "ACTIVATION_KEY_INVALID"


@windows_only
def test_legacy_credential_imported_keeps_agent_id(home):
    plain = json.dumps({"agent_id": "agt_legacy", "agent_credential": "old-cred"}).encode()
    blob = dpapi.protect(plain, entropy=None)  # legacy win32crypt format: no entropy
    paths.legacy_data.mkdir(parents=True)
    write_json(paths.legacy_data / "agent_activation.json",
               {"version": 2, "dpapi": base64.b64encode(blob).decode()})
    service = ActivationService()
    assert service.identity.agent_id == "agt_legacy"
    assert credential_file.exists()
