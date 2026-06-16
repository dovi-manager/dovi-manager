from pathlib import Path

from scripts.public_audit import main, scan_paths


def test_public_audit_passes_current_tree() -> None:
    assert main([]) == 0


def test_public_audit_catches_personal_and_secret_markers(tmp_path: Path) -> None:
    sample = tmp_path / "bad.txt"
    sample.write_text(
        "\n".join(
            [
                "old author: " + "hel" + "der.ceys" + "sens1" + "@hot" + "mail.com",
                "old image: ghcr.io/" + "ceys" + "sens" + "hel" + "der/dovi-manager",
                "local path: C:\\Users\\Example\\Documents\\Projects\\dovi-manager",
                "token: ghp_" + ("x" * 36),
            ]
        ),
        encoding="utf-8",
    )

    rules = {finding.rule for finding in scan_paths([sample])}

    assert {
        "personal-email",
        "old-ghcr-namespace",
        "windows-user-path",
        "github-token",
    } <= rules
