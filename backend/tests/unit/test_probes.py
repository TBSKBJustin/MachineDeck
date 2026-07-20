from app.phase0.system_metrics import permission_snapshot, read_system_metrics


def test_system_metrics_are_json_safe() -> None:
    result = read_system_metrics()
    assert result.available
    assert result.data["memory"]["total_bytes"] > 0
    assert isinstance(result.to_dict(), dict)


def test_permission_probe_is_read_only_snapshot() -> None:
    result = permission_snapshot()
    assert set(result.data["commands"]) == {"systemctl", "journalctl", "docker", "nvidia-smi"}

