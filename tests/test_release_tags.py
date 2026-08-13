import pytest

from scripts.release_tags import _branch_slug, calculate_release, metadata_tag_specs

SHA = "0123456789abcdef" * 4


def test_stable_tag_gets_version_aliases_and_latest() -> None:
    metadata = calculate_release("refs/tags/v1.2.3", SHA)
    assert metadata.kind == "stable"
    assert metadata.tags == ("1.2.3", "1.2", "1", "latest")
    assert metadata.release_tag == "v1.2.3"
    assert not metadata.prerelease


@pytest.mark.parametrize("tag", ["v1.2.3-alpha.0", "v1.2.3-beta.2", "v1.2.3-rc.4"])
def test_prerelease_tag_is_exact_only(tag: str) -> None:
    metadata = calculate_release(f"refs/tags/{tag}", SHA)
    assert metadata.kind == "prerelease"
    assert metadata.tags == (tag.removeprefix("v"),)
    assert metadata.release_tag == tag
    assert metadata.prerelease


def test_beta_branch_gets_safe_mobile_and_sha_tags() -> None:
    metadata = calculate_release("refs/heads/beta/Cookie-Rotation.v2", SHA)
    assert metadata.kind == "beta"
    assert metadata.tags == (
        "beta-cookie-rotation.v2",
        "beta-cookie-rotation.v2-sha-0123456789ab",
        "beta",
    )
    assert metadata.release_tag is None
    assert "latest" not in metadata_tag_specs(metadata)


def test_beta_branch_slug_normalizes_runs_and_truncates() -> None:
    assert _branch_slug("---Cookie***Rotation---") == "cookie-rotation"
    long_name = "a" * 100
    assert _branch_slug(long_name) == "a" * 80


def test_beta_branch_slug_rejects_only_invalid_characters() -> None:
    with pytest.raises(ValueError, match="Docker-safe"):
        _branch_slug("***")


def test_sha_validation_boundaries() -> None:
    with pytest.raises(ValueError):
        calculate_release("refs/heads/beta/test", "a" * 6)
    with pytest.raises(ValueError):
        calculate_release("refs/heads/beta/test", "a" * 65)

    lowercase = calculate_release("refs/heads/beta/test", "abc1234")
    assert lowercase.tags[1] == "beta-test-sha-abc1234"

    uppercase = calculate_release("refs/heads/beta/test", "A" * 64)
    assert uppercase.tags[1] == "beta-test-sha-aaaaaaaaaaaa"


@pytest.mark.parametrize(
    "ref",
    [
        "refs/tags/v1.2",
        "refs/tags/v1.2.3+build.1",
        "refs/tags/v1.2.3-beta",
        "refs/tags/v01.2.3",
        "refs/heads/feature/beta-test",
        "refs/heads/beta/with/slash",
    ],
)
def test_unsupported_refs_fail_closed(ref: str) -> None:
    with pytest.raises(ValueError):
        calculate_release(ref, SHA)


def test_invalid_sha_fails_closed() -> None:
    with pytest.raises(ValueError):
        calculate_release("refs/tags/v1.2.3", "not-a-sha")
