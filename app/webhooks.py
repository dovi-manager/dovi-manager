from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping

from app.config import Settings
from app.safety import relative_media_path, validate_media_file


@dataclass(frozen=True)
class MappedMediaPath:
    root_id: str
    relative_path: str


def _normalized_external_path(value: str) -> PurePosixPath:
    normalized = value.strip().replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or not path.is_absolute() or ".." in path.parts:
        raise ValueError("invalid external path")
    return path


def normalize_external_prefix(value: str) -> str:
    return str(_normalized_external_path(value))


def map_external_path(
    settings: Settings,
    mappings: Iterable[Mapping[str, Any]],
    integration: str,
    external_path: str,
) -> MappedMediaPath:
    path = _normalized_external_path(external_path)
    matches: list[tuple[int, Mapping[str, Any], PurePosixPath]] = []
    for mapping in mappings:
        if str(mapping["integration"]).casefold() != integration.casefold():
            continue
        prefix = _normalized_external_path(str(mapping["external_prefix"]))
        prefix_parts = tuple(part.casefold() for part in prefix.parts)
        path_parts = tuple(part.casefold() for part in path.parts)
        if path_parts[: len(prefix_parts)] == prefix_parts:
            matches.append((len(prefix.parts), mapping, prefix))
    if not matches:
        raise ValueError(f"{integration.title()} path is outside configured roots")

    _, mapping, prefix = max(matches, key=lambda item: item[0])
    relative_parts = path.parts[len(prefix.parts) :]
    if not relative_parts:
        raise ValueError(f"{integration.title()} path does not identify a file")
    root = settings.media_root_by_id(str(mapping["root_id"]))
    local_path = root.path.joinpath(*relative_parts)
    validated = validate_media_file(root.path, local_path)
    return MappedMediaPath(
        root.id,
        relative_media_path(root.path, validated),
    )


def map_radarr_path(
    settings: Settings,
    root_prefix: str,
    external_path: str,
) -> str:
    return map_external_path(
        settings,
        [
            {
                "integration": "radarr",
                "external_prefix": root_prefix,
                "root_id": "default",
            }
        ],
        "radarr",
        external_path,
    ).relative_path


def radarr_event_paths(payload: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    movie_file = payload.get("movieFile")
    movie = payload.get("movie")
    if isinstance(movie_file, dict):
        direct = movie_file.get("path")
        if isinstance(direct, str) and direct:
            paths.append(direct)
        relative = movie_file.get("relativePath")
        folder = movie.get("folderPath") if isinstance(movie, dict) else None
        if isinstance(relative, str) and isinstance(folder, str):
            paths.append(str(PurePosixPath(folder.replace("\\", "/")) / relative))
    renamed_files = payload.get("renamedFiles")
    if isinstance(renamed_files, list):
        for item in renamed_files:
            if not isinstance(item, dict):
                continue
            value = item.get("newPath") or item.get("path")
            if isinstance(value, str) and value:
                paths.append(value)
    return list(dict.fromkeys(paths))


def sonarr_event_paths(payload: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    episode_file = payload.get("episodeFile")
    if isinstance(episode_file, dict):
        value = episode_file.get("path")
        if isinstance(value, str) and value:
            paths.append(value)
    episode_files = payload.get("episodeFiles")
    if isinstance(episode_files, list):
        for item in episode_files:
            if not isinstance(item, dict):
                continue
            value = item.get("path")
            if isinstance(value, str) and value:
                paths.append(value)
    renamed = payload.get("renamedEpisodeFiles")
    if isinstance(renamed, list):
        for item in renamed:
            if not isinstance(item, dict):
                continue
            value = item.get("path")
            if isinstance(value, str) and value:
                paths.append(value)
    return list(dict.fromkeys(paths))
