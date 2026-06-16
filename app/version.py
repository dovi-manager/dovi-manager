from dataclasses import asdict, dataclass
from os import environ
from typing import Mapping


@dataclass(frozen=True)
class BuildInfo:
    version: str
    revision: str
    build_date: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def load_build_info(env: Mapping[str, str] | None = None) -> BuildInfo:
    values = environ if env is None else env
    return BuildInfo(
        version=values.get("DOVI_MANAGER_VERSION", "dev"),
        revision=values.get("DOVI_MANAGER_REVISION", "unknown"),
        build_date=values.get("DOVI_MANAGER_BUILD_DATE", "unknown"),
    )
