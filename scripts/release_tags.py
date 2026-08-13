"""Calculate the Docker tags and release metadata for a Git reference.

The workflow calls :func:`calculate_release` before authenticating to GHCR.  Keeping
the parsing here makes the branch/tag contract unit-testable and prevents an
unexpected Git ref from being published accidentally.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path

_STABLE_TAG = re.compile(
    r"^refs/tags/v(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)$"
)
_PRE_TAG = re.compile(
    r"^refs/tags/v(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)-"
    r"(?P<channel>alpha|beta|rc)\.(?P<number>0|[1-9]\d*)$"
)
_BETA_BRANCH = re.compile(r"^refs/heads/beta/(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)$")


@dataclass(frozen=True)
class ReleaseMetadata:
    """Validated Docker and GitHub release information."""

    kind: str
    tags: tuple[str, ...]
    release_tag: str | None = None
    prerelease: bool = False


def _branch_slug(name: str) -> str:
    # Keep the slug within Docker's 128-character tag limit and avoid tags that
    # differ only by case.  The branch contract has already excluded slashes.
    slug = re.sub(r"[^a-z0-9_.-]+", "-", name.lower()).strip("-._")
    if not slug:
        raise ValueError("beta branch name must contain a Docker-safe identifier")
    return slug[:80]


def calculate_release(ref: str, sha: str) -> ReleaseMetadata:
    """Return metadata for a supported tag or beta branch.

    Stable tags publish the major/minor aliases and ``latest``.  Prerelease
    tags publish only their exact version.  A beta branch publishes a moving
    branch alias, a short-SHA tag, and the shared ``beta`` alias.
    """

    if not re.fullmatch(r"[0-9a-fA-F]{7,64}", sha):
        raise ValueError("GITHUB_SHA must be a hexadecimal commit SHA")

    stable = _STABLE_TAG.fullmatch(ref)
    if stable:
        major, minor, patch = (stable.group(name) for name in ("major", "minor", "patch"))
        version = f"{major}.{minor}.{patch}"
        return ReleaseMetadata(
            kind="stable",
            tags=(version, f"{major}.{minor}", major, "latest"),
            release_tag=f"v{version}",
        )

    prerelease = _PRE_TAG.fullmatch(ref)
    if prerelease:
        version = (
            f"{prerelease.group('major')}.{prerelease.group('minor')}."
            f"{prerelease.group('patch')}-{prerelease.group('channel')}."
            f"{prerelease.group('number')}"
        )
        return ReleaseMetadata(
            kind="prerelease",
            tags=(version,),
            release_tag=f"v{version}",
            prerelease=True,
        )

    beta = _BETA_BRANCH.fullmatch(ref)
    if beta:
        slug = _branch_slug(beta.group("name"))
        short_sha = sha[:12].lower()
        return ReleaseMetadata(
            kind="beta",
            tags=(f"beta-{slug}", f"beta-{slug}-sha-{short_sha}", "beta"),
        )

    raise ValueError(
        "unsupported ref; expected vX.Y.Z, vX.Y.Z-{alpha,beta,rc}.N, or beta/<name>"
    )


def metadata_tag_specs(metadata: ReleaseMetadata) -> str:
    """Render tags in docker/metadata-action's ``type=raw`` format."""

    return "\n".join(f"type=raw,value={tag}" for tag in metadata.tags)


def _write_output(path: Path, metadata: ReleaseMetadata) -> None:
    values = {
        "kind": metadata.kind,
        "tags": metadata_tag_specs(metadata),
        "release_tag": metadata.release_tag or "",
        "prerelease": str(metadata.prerelease).lower(),
    }
    with path.open("a", encoding="utf-8") as output:
        for key, value in values.items():
            if "\n" in value:
                output.write(f"{key}<<ALEXA2HA_EOF\n{value}\nALEXA2HA_EOF\n")
            else:
                output.write(f"{key}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ref", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        metadata = calculate_release(args.ref, args.sha)
    except ValueError as exc:
        parser.error(str(exc))
    if args.output:
        _write_output(args.output, metadata)
    else:
        print(metadata_tag_specs(metadata))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
