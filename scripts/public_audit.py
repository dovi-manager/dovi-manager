from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKIPPED_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tmp",
    ".uv-cache",
    ".venv",
    "__pycache__",
    "config",
    "dev-cache",
    "dev-media",
    "node_modules",
}
SKIPPED_SUFFIXES = {
    ".db",
    ".log",
    ".pyc",
    ".woff",
    ".woff2",
}


@dataclass(frozen=True)
class AuditRule:
    name: str
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    rule: str
    excerpt: str


def _literal(*parts: str) -> str:
    return "".join(parts)


RULES = (
    AuditRule(
        "personal-name",
        re.compile(
            "|".join(
                re.escape(value)
                for value in (
                    _literal("Ceys", "sens"),
                    _literal("Hel", "der"),
                )
            ),
            re.IGNORECASE,
        ),
    ),
    AuditRule(
        "personal-email",
        re.compile(
            re.escape(_literal("hel", "der.ceys", "sens1", "@hot", "mail.com")),
            re.IGNORECASE,
        ),
    ),
    AuditRule(
        "old-ghcr-namespace",
        re.compile(
            re.escape(
                _literal("ghcr.io/", "ceys", "sens", "hel", "der", "/dovi-manager")
            ),
            re.IGNORECASE,
        ),
    ),
    AuditRule(
        "windows-user-path",
        re.compile(r"\b[A-Z]:\\Users\\[^\\\r\n]+", re.IGNORECASE),
    ),
    AuditRule(
        "private-registry-docs",
        re.compile(
            "|".join(
                re.escape(value)
                for value in (
                    _literal("Personal Access ", "Token"),
                    _literal("read", ":packages"),
                    _literal("Git ", "credential"),
                    _literal("dovi-manager-", "ghcr"),
                    _literal("private multi-", "architecture image"),
                )
            ),
            re.IGNORECASE,
        ),
    ),
    AuditRule(
        "github-token",
        re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}"),
    ),
    AuditRule(
        "openai-token",
        re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    ),
    AuditRule(
        "aws-access-key",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    ),
    AuditRule(
        "private-key",
        re.compile(r"BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY"),
    ),
)


def _is_skipped(path: Path) -> bool:
    parts = set(path.parts)
    return bool(parts & SKIPPED_DIRS) or path.suffix.lower() in SKIPPED_SUFFIXES


def _iter_files(paths: Iterable[Path]) -> Iterable[Path]:
    for path in paths:
        if not path.exists():
            continue
        if path.is_file():
            if path.suffix.lower() not in SKIPPED_SUFFIXES:
                yield path
            continue
        for child in path.rglob("*"):
            if child.is_file() and not _is_skipped(child.relative_to(path)):
                yield child


def scan_paths(paths: Iterable[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for path in _iter_files(paths):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            for rule in RULES:
                if rule.pattern.search(line):
                    findings.append(
                        Finding(
                            path=path,
                            line=line_number,
                            rule=rule.name,
                            excerpt=line.strip()[:160],
                        )
                    )
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scan the public release tree for personal markers and secrets."
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=[ROOT],
        help="Files or directories to scan. Defaults to the repository root.",
    )
    args = parser.parse_args(argv)
    findings = scan_paths(args.paths)
    for finding in findings:
        path = finding.path
        try:
            path = path.relative_to(ROOT)
        except ValueError:
            pass
        print(
            f"{path}:{finding.line}: {finding.rule}: {finding.excerpt}",
            file=sys.stderr,
        )
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
