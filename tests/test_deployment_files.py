from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_compose_uses_pullable_public_edge_image() -> None:
    compose = (ROOT / "docker-compose.example.yml").read_text(encoding="utf-8")

    assert "ghcr.io/dovi-manager/dovi-manager:edge" in compose
    assert "pull_policy: always" in compose
    assert "build:" not in compose


def test_local_compose_override_restores_build() -> None:
    override = (ROOT / "docker-compose.local.yml").read_text(encoding="utf-8")

    assert "image: dovi-manager:local" in override
    assert "build:" in override
    assert "pull_policy: build" in override


def test_ci_publishes_expected_ghcr_tags_and_metadata() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "packages: write" in workflow
    assert "ghcr.io/dovi-manager/dovi-manager" in workflow
    assert "github.repository_owner == 'dovi-manager'" in workflow
    assert "type=raw,value=edge" in workflow
    assert "type=sha,format=long,prefix=sha-" in workflow
    assert "type=semver,pattern={{version}}" in workflow
    assert "type=raw,value=latest" in workflow
    assert "platforms: linux/amd64,linux/arm64" in workflow
    assert "APP_REVISION=${{ github.sha }}" in workflow
    assert "provenance: mode=max" in workflow
    assert "sbom: true" in workflow
    assert 'FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: "true"' in workflow
