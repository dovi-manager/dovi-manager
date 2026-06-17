from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_compose_uses_pullable_public_stable_image() -> None:
    compose = (ROOT / "docker-compose.example.yml").read_text(encoding="utf-8")

    assert "ghcr.io/dovi-manager/dovi-manager:latest" in compose
    assert "pull_policy: always" in compose
    assert "build:" not in compose
    assert "${MEDIA_PATH:-./dev-media}:/media2/movies" in compose
    assert "${SHOWS_PATH:-./dev-shows}:/media2/shows" in compose
    assert "SHOWS_ROOT: /media2/shows" in compose
    assert "TV_MEDIA_PATH" not in compose
    assert "ADDITIONAL_MEDIA_ROOTS_JSON" not in compose


def test_env_example_uses_movies_and_shows_without_root_json() -> None:
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "DOVI_MANAGER_IMAGE=ghcr.io/dovi-manager/dovi-manager:latest" in env_example
    assert "MEDIA_PATH=/path/on/host/to/movies" in env_example
    assert "SHOWS_PATH=/path/on/host/to/shows" in env_example
    assert "SHOWS_ROOT_LABEL=Shows" in env_example
    assert "TV_MEDIA_PATH" not in env_example
    assert "ADDITIONAL_MEDIA_ROOTS_JSON" not in env_example


def test_readme_documents_media_roots_config_file() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "/config/media-roots.json" in readme
    assert '"id": "anime"' in readme
    assert "Root IDs should remain stable" in readme


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
    assert "value=stable" not in workflow
    assert "platforms: linux/amd64,linux/arm64" in workflow
    assert "APP_REVISION=${{ github.sha }}" in workflow
    assert "provenance: mode=max" in workflow
    assert "sbom: true" in workflow
    assert 'FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: "true"' in workflow


def test_readme_documents_docker_image_channels_and_bundled_cli() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "No separate `cryptochrome/dovi_convert` container is required." in readme
    assert "`latest`" in readme
    assert "`edge`" in readme
    assert "`stable` is not published" in readme
