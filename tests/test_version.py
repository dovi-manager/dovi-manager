from app.version import load_build_info


def test_build_info_defaults_for_local_development() -> None:
    info = load_build_info({})

    assert info.as_dict() == {
        "version": "dev",
        "revision": "unknown",
        "build_date": "unknown",
    }


def test_build_info_reads_container_metadata() -> None:
    info = load_build_info(
        {
            "DOVI_MANAGER_VERSION": "0.1.0",
            "DOVI_MANAGER_REVISION": "abc123",
            "DOVI_MANAGER_BUILD_DATE": "2026-06-14T17:00:00Z",
        }
    )

    assert info.version == "0.1.0"
    assert info.revision == "abc123"
    assert info.build_date == "2026-06-14T17:00:00Z"
