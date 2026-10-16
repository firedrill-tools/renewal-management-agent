"""An installer's immutable hardlinks are distinct from mutable source aliases."""

import os
from pathlib import Path

import pytest

from renewal_agent import assets
from renewal_agent.workspace import Workspace
from renewal_agent.managed import ManagedWorkspace


def test_pinned_installer_hardlinks_read_exact_bytes_but_never_become_mutable_source(
    tmp_path, monkeypatch
):
    root = tmp_path / "data"
    root.mkdir()
    originals = {name: assets.read_asset(name) for name in assets.MANIFEST}
    for name, raw in originals.items():
        cache = tmp_path / (name + ".cached")
        cache.write_bytes(raw)
        os.link(cache, root / name)
    monkeypatch.setattr(assets, "DATA_ROOT", root)
    for name, raw in originals.items():
        assert assets.read_asset(name) == raw
    with pytest.raises(ValueError, match="unshared"):
        Workspace(root / "starter.json")
    with ManagedWorkspace.seed_bytes(
        tmp_path / "state.db", assets.read_asset("starter.json")
    ) as store:
        assert len(store.snapshot()["business"]["renewals"]) == 4
    assert all((root / name).read_bytes() == raw for name, raw in originals.items())


def test_package_asset_hash_or_alias_never_silently_repairs_or_accepts_changed_original(
    tmp_path, monkeypatch
):
    original = assets.read_asset("starter.json")
    file = tmp_path / "starter.json"
    file.write_bytes(original.replace(b"Northstar", b"ChangedXX"))
    monkeypatch.setattr(assets, "DATA_ROOT", tmp_path)
    with pytest.raises(ValueError, match="hash"):
        assets.read_asset("starter.json")
    damaged = file.read_bytes()
    file.rename(tmp_path / "other.json")
    file.symlink_to(tmp_path / "other.json")
    with pytest.raises(ValueError, match="nonsymlink"):
        assets.read_asset("starter.json")
    assert (tmp_path / "other.json").read_bytes() == damaged
    with pytest.raises(ValueError, match="unknown"):
        assets.read_asset("../other.json")
