# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Asking a container registry three questions, without a registry.

Every test here drives :class:`~mcuhome.workbench.ociregistry.ImageRegistry`
through its opener seam, so the suite never makes an HTTP request. What
is checked is the part that turned out to be easy to get wrong: the
anonymous token dance, what an answer's *headers* are read as, which
media types a manifest request asks for, and where a bearer token is
allowed to travel.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from mcuhome.model.errors import BuildError
from mcuhome.model.imageref import DOCKER_HUB, parse_reference

from mcuhome.workbench.ociregistry import (
    ImageRegistry,
    ImageRegistryError,
    ImageRegistryUnauthorized,
    ImageRegistryUnreachable,
    Response,
)

#: The real opener, captured before the suite's autouse guard replaces
#: it. One test drives the genuine article with only ``urllib`` stubbed —
#: translating a network error is exactly what that half does — and the
#: guard is there to catch the tests that forgot, not this one.
REAL_URLOPEN = ImageRegistry._urlopen

REPO = "ghcr.io/other/environment"
DIGEST = "sha256:" + "ab" * 32
CONFIG_DIGEST = "sha256:" + "cd" * 32


def reference(text: str = REPO):
    return parse_reference(text, default_registry=DOCKER_HUB)


class Opener:
    """A scripted registry: a URL prefix answers, and every call is recorded."""

    def __init__(self, answers: dict[str, Response]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> Response:
        del timeout
        self.calls.append((url, dict(headers)))
        for prefix, answer in self.answers.items():
            if url.startswith(prefix):
                return answer
        return Response(status=404)


def manifest_body(**extra: Any) -> bytes:
    return json.dumps({"config": {"digest": CONFIG_DIGEST}, **extra}).encode()


def config_body(labels: dict[str, str] | None) -> bytes:
    return json.dumps({"config": {"Labels": labels}}).encode()


# --------------------------------------------------------------------------
# the token dance
# --------------------------------------------------------------------------


def test_a_401_is_answered_by_asking_the_realm_it_names() -> None:
    """Anonymous access is a request, not a guess: the 401 says where."""
    calls: list[str] = []

    def opener(url: str, headers: dict[str, str], timeout: float) -> Response:
        del timeout
        calls.append(url)
        if url.startswith("https://ghcr.io/token"):
            return Response(status=200, body=json.dumps({"token": "t-42"}).encode())
        if "Authorization" not in headers:
            return Response(
                status=401,
                headers={
                    "WWW-Authenticate": (
                        'Bearer realm="https://ghcr.io/token",service="ghcr.io",'
                        'scope="repository:other/environment:pull"'
                    )
                },
            )
        assert headers["Authorization"] == "Bearer t-42"
        return Response(status=200, body=manifest_body(), headers={"Docker-Content-Digest": DIGEST})

    found = ImageRegistry(opener=opener).facts(reference(f"{REPO}:zephyr-4.4.0-r10")).digest
    assert found == DIGEST
    assert any("scope=repository" in url for url in calls)
    assert any("service=ghcr.io" in url for url in calls)


def test_the_token_is_reused_for_the_next_question_about_one_repository() -> None:
    """Three questions, one round of authentication."""
    tokens = 0

    def opener(url: str, headers: dict[str, str], timeout: float) -> Response:
        del timeout
        nonlocal tokens
        if url.startswith("https://ghcr.io/token"):
            tokens += 1
            return Response(status=200, body=json.dumps({"token": "t"}).encode())
        if "Authorization" not in headers:
            return Response(
                status=401, headers={"WWW-Authenticate": 'Bearer realm="https://ghcr.io/token"'}
            )
        if "/manifests/" in url:
            return Response(
                status=200, headers={"Docker-Content-Digest": DIGEST}, body=manifest_body()
            )
        return Response(status=200, body=config_body({"a": "b"}))

    registry = ImageRegistry(opener=opener)
    ref = reference(f"{REPO}:zephyr-4.4.0-r10")
    assert registry.facts(ref).digest == DIGEST
    assert registry.facts(ref).labels == {"a": "b"}
    assert tokens == 1


def test_a_header_is_read_however_the_server_spelled_it() -> None:
    """ghcr.io answers ``Www-Authenticate``; the spec's prose writes it shouting.

    HTTP header names are case insensitive and servers exercise that
    freedom. Every header this module reads is one a resolution turns
    on, so a case-sensitive lookup fails by finding nothing at all —
    which reads exactly like a private registry.
    """

    def opener(url: str, headers: dict[str, str], timeout: float) -> Response:
        del timeout
        if url.startswith("https://ghcr.io/token"):
            return Response(status=200, body=json.dumps({"token": "t"}).encode())
        if "Authorization" not in headers:
            return Response(
                status=401, headers={"Www-Authenticate": 'Bearer realm="https://ghcr.io/token"'}
            )
        return Response(status=200, body=manifest_body(), headers={"docker-content-digest": DIGEST})

    assert ImageRegistry(opener=opener).facts(reference(f"{REPO}:t")).digest == DIGEST


def test_a_registry_that_hands_out_no_token_is_a_private_one() -> None:
    opener = Opener(
        {
            "https://ghcr.io/token": Response(status=403),
            "https://ghcr.io/v2/": Response(
                status=401, headers={"WWW-Authenticate": 'Bearer realm="https://ghcr.io/token"'}
            ),
        }
    )
    with pytest.raises(ImageRegistryUnauthorized) as refusal:
        ImageRegistry(opener=opener).facts(reference(f"{REPO}:t"))
    assert "docker login ghcr.io" in str(refusal.value)


def test_a_401_naming_no_realm_leaves_nothing_to_ask() -> None:
    opener = Opener({"https://ghcr.io/v2/": Response(status=401)})
    with pytest.raises(ImageRegistryUnauthorized):
        ImageRegistry(opener=opener).facts(reference(f"{REPO}:t"))


# --------------------------------------------------------------------------
# what the three questions answer
# --------------------------------------------------------------------------


def test_a_tag_that_does_not_exist_is_a_refusal_naming_it() -> None:
    """There is nothing to answer with: a name the repository does not
    carry is not an image with no labels, it is a name nobody published.

    A resolution that walks several candidates catches this one and
    records it as a candidate that could not be read, which is where
    "keep looking" belongs — here it would be a `None` every caller had
    to check for.
    """
    opener = Opener({"https://ghcr.io/v2/": Response(status=404)})
    with pytest.raises(ImageRegistryError) as refusal:
        ImageRegistry(opener=opener).facts(reference(f"{REPO}:nope"))
    assert "nope" in str(refusal.value)


def test_a_manifest_request_offers_to_take_an_index_first() -> None:
    """The digest that identifies a multi-architecture image is the index's.

    A request that did not offer to take an index would be answered with
    one architecture's manifest and would pin an environment no other
    architecture can use.
    """
    opener = Opener(
        {
            "https://ghcr.io/v2/": Response(
                status=200, body=manifest_body(), headers={"Docker-Content-Digest": DIGEST}
            )
        }
    )
    ImageRegistry(opener=opener).facts(reference(f"{REPO}:t"))
    accept = opener.calls[0][1]["Accept"]
    assert accept.index("index.v1+json") < accept.index("manifest.v2+json")
    assert "manifest.list.v2+json" in accept


def test_an_answer_without_a_digest_header_cannot_pin_anything() -> None:
    opener = Opener({"https://ghcr.io/v2/": Response(status=200)})
    with pytest.raises(ImageRegistryError) as refusal:
        ImageRegistry(opener=opener).facts(reference(f"{REPO}:t"))
    assert "digest" in str(refusal.value)


def test_a_tag_listing_follows_the_link_header_to_the_end() -> None:
    """Registries paginate; a resolution that read page one would miss releases."""
    pages = {
        "https://ghcr.io/v2/other/environment/tags/list?n=100": Response(
            status=200,
            headers={"Link": '</v2/other/environment/tags/list?n=100&last=b>; rel="next"'},
            body=json.dumps({"tags": ["a", "b"]}).encode(),
        ),
        "https://ghcr.io/v2/other/environment/tags/list?n=100&last=b": Response(
            status=200, body=json.dumps({"tags": ["c"]}).encode()
        ),
    }

    def opener(url: str, headers: dict[str, str], timeout: float) -> Response:
        del headers, timeout
        return pages.get(url, Response(status=404))

    assert ImageRegistry(opener=opener).tags(reference()) == ("a", "b", "c")


def test_a_repository_with_no_tags_lists_none_rather_than_refusing() -> None:
    opener = Opener({"https://ghcr.io/v2/": Response(status=404)})
    assert ImageRegistry(opener=opener).tags(reference()) == ()


def test_labels_come_from_the_config_blob_the_manifest_names() -> None:
    def opener(url: str, headers: dict[str, str], timeout: float) -> Response:
        del headers, timeout
        if "/manifests/" in url:
            return Response(
                status=200, body=manifest_body(), headers={"Docker-Content-Digest": DIGEST}
            )
        assert url.endswith(f"/blobs/{CONFIG_DIGEST}")
        return Response(status=200, body=config_body({"org.mcuhome.x": "1"}))

    assert ImageRegistry(opener=opener).facts(reference(f"{REPO}:t")).labels == {
        "org.mcuhome.x": "1"
    }


AMD64 = "sha256:" + "ef" * 32
ARM64 = "sha256:" + "cd" * 32
ATTESTATION = "sha256:" + "ab" * 32

#: What the two architectures of one build environment declare. The
#: workspace package is architecture-neutral and identical in both; the
#: tools package is per platform and is exactly why an index may not be
#: read at whichever manifest comes first.
AMD64_LABELS = {"org.mcuhome.build-environment.packages.mcuhome-build-tools_linux-amd64": "1.0.0"}
ARM64_LABELS = {"org.mcuhome.build-environment.packages.mcuhome-build-tools_linux-arm64": "1.0.0"}


def index_opener(entries: list[dict[str, Any]], configs: dict[str, dict[str, str]]) -> Any:
    """A registry answering one index, its manifests and their configs.

    *entries* is the index verbatim, *configs* maps a manifest digest to
    the labels its config blob carries — each manifest names a config of
    its own, so the test can tell which one was read.
    """
    blobs = {digest: "sha256:" + f"{index:02x}" * 32 for index, digest in enumerate(configs, 1)}

    def opener(url: str, headers: dict[str, str], timeout: float) -> Response:
        del headers, timeout
        if "/manifests/" in url:
            for digest, blob in blobs.items():
                if url.endswith(digest):
                    return Response(
                        status=200,
                        body=json.dumps({"config": {"digest": blob}}).encode(),
                        headers={"Docker-Content-Digest": digest},
                    )
            return Response(
                status=200,
                body=json.dumps({"manifests": entries}).encode(),
                headers={"Docker-Content-Digest": "sha256:" + "11" * 32},
            )
        for digest, blob in blobs.items():
            if url.endswith(blob):
                return Response(status=200, body=config_body(configs[digest]))
        return Response(status=404)

    return opener


def test_an_index_is_followed_to_this_hosts_platform() -> None:
    """An index carries no config of its own, and its entries are not
    interchangeable: the tools package a build environment declares is
    per platform, so reading whichever manifest is listed first would
    answer with another architecture's package set."""
    opener = index_opener(
        [
            {"digest": ARM64, "platform": {"os": "linux", "architecture": "arm64"}},
            {"digest": AMD64, "platform": {"os": "linux", "architecture": "amd64"}},
        ],
        {AMD64: AMD64_LABELS, ARM64: ARM64_LABELS},
    )
    registry = ImageRegistry(opener=opener)
    assert registry.facts(reference(f"{REPO}:t"), platform="linux-amd64").labels == AMD64_LABELS
    assert registry.facts(reference(f"{REPO}:t"), platform="linux-arm64").labels == ARM64_LABELS


def test_the_digest_answered_is_the_platform_manifests_and_not_the_indexs() -> None:
    """What was checked is what is pinned. An index digest stands for
    every architecture at once, including the ones whose labels nobody
    read, so it is not a name for the bytes that will run here."""
    opener = index_opener(
        [
            {"digest": AMD64, "platform": {"os": "linux", "architecture": "amd64"}},
            {"digest": ARM64, "platform": {"os": "linux", "architecture": "arm64"}},
        ],
        {AMD64: AMD64_LABELS, ARM64: ARM64_LABELS},
    )
    registry = ImageRegistry(opener=opener)
    assert registry.facts(reference(f"{REPO}:t"), platform="linux-amd64").digest == AMD64
    assert registry.facts(reference(f"{REPO}:t"), platform="linux-arm64").digest == ARM64


def test_an_image_that_is_no_index_is_pinned_by_the_digest_it_answered_with() -> None:
    """A single-architecture image has no index above it, and the digest
    the registry named it by is the one to pin."""
    digest = "sha256:" + "77" * 32

    def opener(url: str, headers: dict[str, str], timeout: float) -> Response:
        del headers, timeout
        if "/manifests/" in url:
            return Response(
                status=200,
                body=manifest_body(),
                headers={"Docker-Content-Digest": digest},
            )
        return Response(status=200, body=config_body({"k": "v"}))

    facts = ImageRegistry(opener=opener).facts(reference(f"{REPO}:t"))
    assert facts.digest == digest
    assert facts.labels == {"k": "v"}


def test_an_attestation_in_the_index_is_not_mistaken_for_an_architecture() -> None:
    """buildx writes SBOM and provenance manifests into the index it pushes.

    They are marked ``platform: unknown/unknown``, carry a config blob
    like any manifest, and carry no image labels at all — so following
    one reports an environment that states nothing about itself, and the
    label gate refuses an image that does carry its labels. Nothing in
    the OCI spec orders an index; the one this project publishes happens
    to list its architectures first, which is luck rather than a promise.

    The attestation is first here on purpose: that is the ordering the
    old code could not survive.
    """
    opener = index_opener(
        [
            {"digest": ATTESTATION, "platform": {"architecture": "unknown", "os": "unknown"}},
            {"digest": AMD64, "platform": {"os": "linux", "architecture": "amd64"}},
        ],
        {AMD64: AMD64_LABELS, ATTESTATION: {}},
    )
    assert (
        ImageRegistry(opener=opener).facts(reference(f"{REPO}:t"), platform="linux-amd64").labels
        == AMD64_LABELS
    )


def test_an_index_without_this_hosts_platform_is_refused_by_name() -> None:
    """An image that is not published for this machine cannot be run on
    it, and taking a foreign architecture's manifest would report a
    package set this host can never use."""
    opener = index_opener(
        [{"digest": ARM64, "platform": {"os": "linux", "architecture": "arm64"}}],
        {ARM64: ARM64_LABELS},
    )
    with pytest.raises(ImageRegistryError) as refusal:
        ImageRegistry(opener=opener).facts(reference(f"{REPO}:t"), platform="linux-amd64")
    assert "linux-amd64" in str(refusal.value)
    assert "linux/arm64" in str(refusal.value)


def test_an_index_of_nothing_but_attestations_describes_no_architecture() -> None:
    """Which is the refusal that already existed, for a case that can now happen."""
    opener = index_opener(
        [{"digest": ATTESTATION, "platform": {"architecture": "unknown", "os": "unknown"}}],
        {ATTESTATION: {}},
    )
    with pytest.raises(ImageRegistryError) as refusal:
        ImageRegistry(opener=opener).facts(reference(f"{REPO}:t"), platform="linux-amd64")
    assert "no architecture" in str(refusal.value)


def test_an_image_with_no_labels_states_nothing_rather_than_failing() -> None:
    def opener(url: str, headers: dict[str, str], timeout: float) -> Response:
        del headers, timeout
        if "/manifests/" in url:
            return Response(
                status=200, body=manifest_body(), headers={"Docker-Content-Digest": DIGEST}
            )
        return Response(status=200, body=config_body(None))

    assert ImageRegistry(opener=opener).facts(reference(f"{REPO}:t")).labels == {}


def test_an_answer_that_is_not_json_names_the_host_rather_than_crashing() -> None:
    opener = Opener({"https://ghcr.io/v2/": Response(status=200, body=b"<html>")})
    with pytest.raises(ImageRegistryError) as refusal:
        ImageRegistry(opener=opener).tags(reference())
    assert "ghcr.io" in str(refusal.value)


# --------------------------------------------------------------------------
# Docker Hub's two spellings
# --------------------------------------------------------------------------


def test_docker_hub_is_addressed_at_its_api_host_under_library() -> None:
    """``docker.io`` is the name in a reference and serves no API.

    A single-component path there is short for ``library/<name>``. Both
    normalizations are docker's own; a reference keeps what a person
    wrote and an HTTP call cannot.
    """
    opener = Opener(
        {
            "https://": Response(
                status=200, body=manifest_body(), headers={"Docker-Content-Digest": DIGEST}
            )
        }
    )
    ImageRegistry(opener=opener).facts(reference("busybox:latest"))
    url = opener.calls[0][0]
    assert url.startswith("https://registry-1.docker.io/v2/library/busybox/manifests/latest")


def test_a_hub_path_that_already_has_an_owner_keeps_it() -> None:
    opener = Opener(
        {
            "https://": Response(
                status=200, body=manifest_body(), headers={"Docker-Content-Digest": DIGEST}
            )
        }
    )
    ImageRegistry(opener=opener).facts(reference("someone/thing:latest"))
    assert "/v2/someone/thing/manifests/" in opener.calls[0][0]


# --------------------------------------------------------------------------
# the one security property
# --------------------------------------------------------------------------


def test_a_bearer_token_never_leaves_the_host_it_was_issued_for() -> None:
    """Blob requests redirect to object storage that authenticates itself.

    Forwarding the registry's token to a third party is a credential leak
    that costs nothing to avoid, so the redirect handler drops the header
    when the host changes. Checked on the handler directly, because the
    thing under test is what ``urllib`` does with a 307.
    """
    import urllib.request

    from mcuhome.workbench.ociregistry import _NoCrossHostAuth

    handler = _NoCrossHostAuth()
    request = urllib.request.Request(
        "https://ghcr.io/v2/x/blobs/sha256:1", headers={"Authorization": "Bearer secret"}
    )

    same = handler.redirect_request(request, None, 307, "", {}, "https://ghcr.io/v2/x/blobs/other")
    assert same is not None
    assert same.get_header("Authorization") == "Bearer secret"

    across = handler.redirect_request(
        request, None, 307, "", {}, "https://objects.example.net/signed"
    )
    assert across is not None
    assert across.get_header("Authorization") is None
    assert "Authorization" not in across.unredirected_hdrs


def test_a_host_that_cannot_be_reached_says_how_to_build_without_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DNS, TLS, a proxy: the same answer, and it names the way out.

    Driven through the *real* opener with only ``urllib`` replaced,
    because translating a network error is exactly what that half does
    and a scripted opener would skip it.
    """
    import urllib.error
    import urllib.request

    def build_opener(*handlers: object) -> object:
        del handlers

        class Refusing:
            def open(self, request: object, timeout: float) -> None:
                del request, timeout
                raise urllib.error.URLError("Name or service not known")

        return Refusing()

    monkeypatch.setattr(ImageRegistry, "_urlopen", REAL_URLOPEN)
    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    with pytest.raises(ImageRegistryUnreachable) as refusal:
        ImageRegistry().facts(reference(f"{REPO}:t"))
    assert isinstance(refusal.value, BuildError)
    assert "ghcr.io" in str(refusal.value)
    # The way out of an unreachable registry is a pin, and it is named.
    assert "@sha256" in str(refusal.value)


def test_an_answer_without_a_digest_header_is_a_refusal_naming_the_registry() -> None:
    """The digest comes from the registry because the registry is what
    binds a name to bytes.

    An answer that carries neither the header nor a digest in the name it
    was asked under leaves nothing to pin, and it is refused here — where
    the registry can still be named — rather than half a resolution
    later, where an empty string reads as a malformed digest instead of
    as a registry that did not answer.
    """
    opener = Opener({"https://ghcr.io/v2/": Response(status=200, body=manifest_body())})
    with pytest.raises(ImageRegistryError) as refusal:
        ImageRegistry(opener=opener).facts(reference(f"{REPO}:t"))
    assert REPO in str(refusal.value)
    assert "Docker-Content-Digest" in str(refusal.value)


def test_a_reference_that_names_its_own_digest_needs_no_header() -> None:
    """Asked by digest, the answer is that digest: the name already is
    the binding."""
    pinned = "sha256:" + "9a" * 32
    opener = Opener({"https://ghcr.io/v2/": Response(status=200, body=manifest_body())})
    assert ImageRegistry(opener=opener).facts(reference(f"{REPO}@{pinned}")).digest == pinned
