from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from app import create_app
from config import Settings

TOKEN = "notification-status-test-" + "x" * 40


def configuration(tmp_path, **overrides):
    return Settings(api_token=TOKEN, db_path=tmp_path / "test.db", mqtt_enabled=False,
                    _env_file=None, **overrides)


def status(client, **overrides):
    params = dict(device="mac-one", platform="mac", environment="sandbox", bundle_id="io.airkeep.mac")
    params.update(overrides)
    return client.get("/v1/notifications/status", params=params)


def private_key(path, curve=ec.SECP256R1()):
    key = ec.generate_private_key(curve)
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))


def test_status_requires_auth_and_checks_registration_environment_and_topic(tmp_path):
    settings = configuration(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        assert status(client).status_code == 401
        client.headers["Authorization"] = "Bearer " + TOKEN
        result = status(client).json()
        assert result == dict(registered=False, configuration="missing", reason="credentials_missing", environment="sandbox", receiveAlerts=False)
        registration = dict(id="mac-one", token="a" * 64, platform="mac", environment="sandbox")
        assert client.post("/v1/devices", json=registration).status_code == 200
        assert status(client).json()["registered"] is True
        assert status(client, environment="production").json()["registered"] is False
        assert status(client, platform="ios", bundle_id="io.airkeep").json()["registered"] is False
        assert status(client, bundle_id="io.example.other").json()["registered"] is False
        assert status(client, environment="unknown").status_code == 422
        client.delete("/v1/devices/mac-one")
        assert status(client).json()["registered"] is False


def test_readiness_is_specific_to_the_requested_environment_and_never_sends(tmp_path):
    key = tmp_path / "sandbox.p8"
    private_key(key)
    settings = configuration(tmp_path, apns_team_id="EXAMPLE", apns_key_id="EXAMPLE",
                             apns_key_path=str(key), apns_key_environment="sandbox")
    app = create_app(settings)
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + TOKEN
        sandbox = status(client).json()
        assert sandbox["configuration"] == "available"
        assert sandbox["reason"] == "local_checks_passed"
        assert sandbox["registered"] is False
        assert status(client, environment="production").json()["configuration"] == "missing"
        wrong_topic = status(client, bundle_id="io.example.other").json()
        assert wrong_topic["reason"] == "topic_mismatch"
        assert wrong_topic["configuration"] == "invalid"
        # No device, job or activity is created by a readiness check.
        for table in ("devices", "outbox", "activities"):
            assert not app.state.store.rows(f"SELECT * FROM {table}")


def test_invalid_and_unreadable_keys_are_not_ready_and_paths_are_private(tmp_path):
    key = tmp_path / "operator-private-key.p8"
    settings = configuration(tmp_path, apns_team_id="EXAMPLE", apns_key_id="EXAMPLE", apns_key_path=str(key))
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + TOKEN
        response = status(client)
        assert response.json()["reason"] == "key_unreadable"
        assert str(tmp_path) not in response.text
        key.write_text("not a private signing key")
        assert status(client).json()["reason"] == "key_invalid"
        private_key(key, ec.SECP384R1())
        assert status(client).json()["configuration"] == "invalid"
        private_key(key)
        assert status(client).json()["configuration"] == "available"
