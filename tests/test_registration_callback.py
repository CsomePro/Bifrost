import os
from pathlib import Path
from types import SimpleNamespace
import sys

os.environ.setdefault("PROXY_POOL_DATA_DIR", "/tmp/bifrost-test-data")
sys.modules.setdefault("yaml", SimpleNamespace(safe_load=lambda *_args, **_kwargs: None, YAMLError=Exception))

from app.models import AppState, NodeRecord, RegistrationCallbackPayload
from app.service import ProxyPoolService
from app.store import StateStore
from app.config import settings


def _build_node(**overrides) -> NodeRecord:
    payload = {
        "id": "node-1",
        "name": "test-node",
        "protocol": "vmess",
        "outbound": {"type": "vmess"},
        "source": "subscription",
        "hash": "hash-1",
        "bound_port": 20001,
        "runtime_state": "running",
    }
    payload.update(overrides)
    return NodeRecord(**payload)


def test_registration_failure_enters_cooldown_and_success_clears_it(tmp_path):
    service = ProxyPoolService()
    service.store = StateStore(Path(tmp_path) / "state.json")
    service.store.update(lambda state: setattr(state, "nodes", [_build_node()]))

    original_threshold = settings.registration_fail_threshold
    original_cooldown = settings.registration_cooldown_secs
    settings.registration_fail_threshold = 2
    settings.registration_cooldown_secs = 300
    try:
        first = service.record_registration_callback(
            RegistrationCallbackPayload(proxy_url="socks5://bifrost:20001", status="failure")
        )
        assert first.registration_failed_count == 1
        assert first.registration_consecutive_failures == 1
        assert first.registration_cooldown_until is None

        second = service.record_registration_callback(
            RegistrationCallbackPayload(proxy_url="socks5://bifrost:20001", status="failure")
        )
        assert second.registration_failed_count == 2
        assert second.registration_consecutive_failures == 2
        assert second.registration_cooldown_until is not None
        assert service._eligible_nodes(service.store.snapshot()) == []

        recovered = service.record_registration_callback(
            RegistrationCallbackPayload(proxy_url="socks5://bifrost:20001", status="success")
        )
        assert recovered.registration_success_count == 1
        assert recovered.registration_consecutive_failures == 0
        assert recovered.registration_cooldown_until is None
        assert len(service._eligible_nodes(service.store.snapshot())) == 1
    finally:
        settings.registration_fail_threshold = original_threshold
        settings.registration_cooldown_secs = original_cooldown


def test_release_registration_cooldown_returns_node_to_pool(tmp_path):
    service = ProxyPoolService()
    service.store = StateStore(Path(tmp_path) / "state.json")
    expired_node = _build_node(
        registration_consecutive_failures=3,
        registration_cooldown_until="2000-01-01T00:00:00+00:00",
    )
    service.store.update(lambda state: setattr(state, "nodes", [expired_node]))

    service._release_registration_cooldowns()

    snapshot = service.store.snapshot()
    assert snapshot.nodes[0].registration_cooldown_until is None
    assert snapshot.nodes[0].registration_consecutive_failures == 0
    assert len(service._eligible_nodes(snapshot)) == 1
