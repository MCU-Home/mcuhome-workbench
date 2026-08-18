# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Asking a container registry three questions, anonymously.

Choosing a build environment means knowing which tags a repository has,
which bytes a tag points at, and what an image says about itself. All
three are answered by the OCI distribution API over plain HTTPS, without
a container runtime, without credentials and **without pulling the
image** — which is what makes client-side pinning affordable at all: the
build container is 1.33 GB on the wire and the three requests below add
up to a few kilobytes.

**Anonymous, and that is the whole authentication story.** A public
registry hands out a pull token to anyone who asks (``GET /token`` at the
realm its ``401`` names), and this asks for one and no more. A registry
that refuses even that is a private one, and private registries are
answered by :mod:`~mcuhome.workbench.resolve_env` handing the question to
the local container program, which already has the operator's
credentials. Teaching this module to log in would mean owning credential
storage, and docker owns it better.

**Stdlib HTTP on purpose.** Three GETs against one host do not justify a
dependency, and a workbench that resolves an environment on every
container build would be pushing that dependency into every install.

The one non-obvious thing it does is **drop the ``Authorization``
header when a redirect leaves the host.** Registries answer blob
requests with a redirect to storage that carries its own signature in the
URL, and forwarding a bearer token to a third party is a credential leak
that costs nothing to avoid.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from mcuhome.model.errors import BuildError
from mcuhome.model.imageref import DOCKER_HUB, Reference

__all__ = [
    "ACCEPT_MANIFEST",
    "Registry",
    "RegistryError",
    "Response",
    "RegistryUnauthorized",
    "RegistryUnreachable",
]

#: The media types a manifest request accepts, **index types first**. The
#: order is the whole reason the list is written out: a multi-architecture
#: image is an index over per-architecture manifests, the digest that
#: identifies the *image* is the index's, and a request that did not offer
#: to take an index would be answered with one architecture's manifest and
#: would pin an environment no other architecture can use.
ACCEPT_MANIFEST = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)

#: One ``key="value"`` pair out of a ``WWW-Authenticate`` challenge.
_CHALLENGE = re.compile(r'([A-Za-z_]+)="([^"]*)"')

_DEFAULT_TIMEOUT = 15.0

#: Docker Hub's two spellings. ``docker.io`` is the name in a reference
#: and serves no API; ``registry-1.docker.io`` is the API host. And a
#: single-component path on Hub is short for ``library/<name>`` — the two
#: normalizations docker itself performs, done here because a reference
#: keeps the spelling a person wrote and an HTTP call cannot.
_HUB_API = "registry-1.docker.io"


def _api(reference: Reference) -> Reference:
    """*reference* as the distribution API wants it addressed."""
    if reference.registry != DOCKER_HUB:
        return reference
    path = reference.path if "/" in reference.path else f"library/{reference.path}"
    return replace(reference, registry=_HUB_API, path=path)


class RegistryError(BuildError):
    """The registry answered, and the answer was not usable."""


class RegistryUnauthorized(RegistryError):
    """The registry refused an anonymous request — it is a private one."""


class RegistryUnreachable(RegistryError):
    """The registry could not be reached at all: DNS, TLS, timeout, proxy."""


class _Headers(dict):
    """Response headers, looked up without caring about case.

    HTTP header names are case insensitive and servers exercise that
    freedom: ghcr.io answers ``Www-Authenticate`` where the spec's own
    prose writes ``WWW-Authenticate``, and a plain dict lookup for the
    second finds nothing. Every header this module reads is one a
    resolution turns on, so the miss would not have been loud.
    """

    def __init__(self, items: Mapping[str, str] | None = None) -> None:
        super().__init__()
        for name, value in (items or {}).items():
            self[name] = value

    def __setitem__(self, name: str, value: str) -> None:
        super().__setitem__(name.lower(), value)

    def __getitem__(self, name: str) -> str:
        return super().__getitem__(name.lower())

    def get(self, name: str, default: Any = None) -> Any:
        return super().get(name.lower(), default)


@dataclass(frozen=True)
class Response:
    """One HTTP answer, as this module's opener seam hands it over.

    Public because it *is* the seam: whoever replaces :class:`Registry`'s
    opener — a test, an embedder with its own HTTP stack — has to produce
    one of these, and the header normalization above is part of what a
    correct answer means rather than an implementation detail of the
    real opener. A seam whose replacement is subtly stricter than the
    original is a seam that hides the bug it was meant to expose.
    """

    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", _Headers(dict(self.headers)))


class _NoCrossHostAuth(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but never carry a bearer token off the host.

    A registry answers a blob request with a redirect to object storage,
    and that URL authenticates itself. Sending the registry's token along
    would hand a third party a credential it never needed.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        if urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            new.headers = {
                name: value
                for name, value in new.headers.items()
                if name.lower() != "authorization"
            }
            # `unredirected_hdrs` is where AbstractHTTPHandler stashes
            # headers it added itself; a token put there would survive
            # the filter above.
            new.unredirected_hdrs.pop("Authorization", None)
        return new


class Registry:
    """One registry host, asked about one repository at a time.

    Holds the pull token it was handed for the duration of a resolution,
    which is what keeps a tag listing plus a manifest plus a config blob
    at one round of authentication rather than three.

    *opener* is the seam every test uses: it takes a URL, a header
    mapping and a timeout, and answers with a :class:`Response`, so the
    whole module is exercisable without a network and without a registry.
    """

    def __init__(
        self,
        *,
        opener: Any = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._timeout = timeout
        self._open = opener if opener is not None else self._urlopen
        self._tokens: dict[str, str] = {}

    # -- the three questions -------------------------------------------

    def tags(self, reference: Reference) -> tuple[str, ...]:
        """Every tag the repository carries, in the order the registry lists them.

        The expensive question — paginated on large repositories, rate
        limited on some hosts — and the reason
        :mod:`~mcuhome.workbench.resolve_env` tries to answer without it
        first. Pagination is followed through the ``Link`` header, which
        is what the distribution spec says a registry signals it with.
        """
        collected: list[str] = []
        api = _api(reference)
        url = f"https://{api.registry}/v2/{api.path}/tags/list?n=100"
        seen: set[str] = set()
        while url and url not in seen:
            seen.add(url)
            response = self._get(reference, url, accept="application/json")
            if response is None:
                return ()
            document = self._json(response, reference, what="tag list")
            listed = document.get("tags")
            if isinstance(listed, list):
                collected.extend(str(tag) for tag in listed)
            url = self._next_page(reference, response)
        return tuple(collected)

    def digest_of(self, reference: Reference) -> str | None:
        """The digest a tag currently points at, or ``None`` when it has none.

        ``None`` means the tag does not exist, which is an ordinary
        answer during resolution rather than a failure: a publisher who
        does not keep an aggregate tag is answered by the fallback.

        The value comes from the ``Docker-Content-Digest`` header rather
        than from hashing the body, because the header is what the
        registry itself binds the name to.
        """
        api = _api(reference)
        name = api.digest or api.tag or "latest"
        url = f"https://{api.registry}/v2/{api.path}/manifests/{name}"
        response = self._get(reference, url, accept=ACCEPT_MANIFEST)
        if response is None:
            return None
        digest = response.headers.get("Docker-Content-Digest")
        if not digest:
            raise RegistryError(
                f"{reference.repository} answered about {name} without naming a digest.",
                hint=(
                    "the registry did not send a Docker-Content-Digest header, which "
                    "the OCI distribution spec requires — the environment cannot be "
                    "pinned against it"
                ),
            )
        return digest

    def labels(self, reference: Reference) -> dict[str, str]:
        """What the image says about itself: the config blob's labels.

        Two requests deep — the manifest names the config blob, the blob
        holds the labels — and the second one is the redirect the
        header-stripping above exists for.

        A **manifest index** is followed one level: an index carries no
        config of its own, so the labels come from the first
        architecture's manifest. That is sound because the labels this
        project constrains on describe the *environment* (contract
        version, Zephyr release, toolchain), which every architecture of
        one build environment shares by construction.
        """
        manifest = self._manifest(reference)
        entries = manifest.get("manifests")
        if isinstance(entries, list) and entries:
            first = next(
                (item for item in entries if isinstance(item, dict) and item.get("digest")),
                None,
            )
            if first is None:
                raise RegistryError(
                    f"{reference.repository} answered with an index naming no manifests.",
                    hint="the image cannot be inspected — it describes no architecture",
                )
            manifest = self._manifest(reference.with_digest(str(first["digest"])))
        config = manifest.get("config")
        digest = config.get("digest") if isinstance(config, dict) else None
        if not isinstance(digest, str):
            raise RegistryError(
                f"{reference.repository} answered with a manifest naming no config.",
                hint="an image without a config blob states nothing about itself",
            )
        api = _api(reference)
        url = f"https://{api.registry}/v2/{api.path}/blobs/{digest}"
        response = self._get(reference, url, accept="application/json")
        if response is None:
            raise RegistryError(
                f"{reference.repository} names a config blob it does not have.",
                hint=f"{digest} is referenced by the manifest and answered with 404",
            )
        blob = self._json(response, reference, what="image config")
        inner = blob.get("config")
        labels = inner.get("Labels") if isinstance(inner, dict) else None
        if not isinstance(labels, dict):
            return {}
        return {str(key): str(value) for key, value in labels.items() if value is not None}

    # -- plumbing ------------------------------------------------------

    def _manifest(self, reference: Reference) -> dict[str, Any]:
        api = _api(reference)
        name = api.digest or api.tag or "latest"
        url = f"https://{api.registry}/v2/{api.path}/manifests/{name}"
        response = self._get(reference, url, accept=ACCEPT_MANIFEST)
        if response is None:
            raise RegistryError(
                f"{reference.repository} has nothing under {name}.",
                hint="the tag or digest does not exist in that repository",
            )
        return self._json(response, reference, what="manifest")

    def _get(self, reference: Reference, url: str, *, accept: str) -> Response | None:
        """One GET with the pull-token dance, or ``None`` for a 404."""
        headers = {"Accept": accept}
        token = self._tokens.get(reference.repository)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        response = self._open(url, headers, self._timeout)
        if response.status == 401:
            token = self._authorize(reference, response)
            headers["Authorization"] = f"Bearer {token}"
            response = self._open(url, headers, self._timeout)
        if response.status == 404:
            return None
        if response.status == 401 or response.status == 403:
            raise RegistryUnauthorized(
                f"{reference.registry} does not serve {reference.path} anonymously.",
                hint=(
                    "MCUHome reads a registry without credentials of its own. Log the "
                    "container program in to that registry and name the environment "
                    "with a tag, so the image can be resolved through it:\n"
                    f"    docker login {reference.registry}"
                ),
            )
        if response.status != 200:
            raise RegistryError(
                f"{reference.registry} answered {response.status} for {reference.path}.",
                hint="the registry rejected an ordinary read — nothing can be resolved from it",
            )
        return response

    def _authorize(self, reference: Reference, refusal: Response) -> str:
        """Take the ``401``'s own word for where to get a pull token."""
        challenge = refusal.headers.get("WWW-Authenticate") or ""
        fields = dict(_CHALLENGE.findall(challenge))
        realm = fields.get("realm")
        if not realm:
            raise RegistryUnauthorized(
                f"{reference.registry} refused an anonymous read of {reference.path}.",
                hint=(
                    "it answered 401 without saying where a token comes from, so there "
                    "is nothing to ask. Log the container program in to that registry "
                    "and name the environment with a tag:\n"
                    f"    docker login {reference.registry}"
                ),
            )
        api = _api(reference)
        query = {
            "scope": fields.get("scope") or f"repository:{api.path}:pull",
            "service": fields.get("service") or api.registry,
        }
        url = f"{realm}?{urllib.parse.urlencode(query)}"
        answer = self._open(url, {"Accept": "application/json"}, self._timeout)
        if answer.status != 200:
            raise RegistryUnauthorized(
                f"{reference.registry} would not hand out a pull token for {reference.path}.",
                hint=(
                    "anonymous access is what MCUHome resolves an environment with. Log "
                    "the container program in to that registry and name the environment "
                    "with a tag:\n"
                    f"    docker login {reference.registry}"
                ),
            )
        document = self._json(answer, reference, what="token")
        # Registries disagree about the name and both spellings are in the
        # wild; the value is the same bearer token.
        token = document.get("token") or document.get("access_token")
        if not isinstance(token, str) or not token:
            raise RegistryUnauthorized(
                f"{reference.registry} answered a token request without a token.",
                hint="nothing can be read from that registry anonymously",
            )
        self._tokens[reference.repository] = token
        return token

    @staticmethod
    def _next_page(reference: Reference, response: Response) -> str:
        """The ``Link: <…>; rel="next"`` a paginated tag listing continues at."""
        link = response.headers.get("Link") or ""
        found = re.search(r"<([^>]+)>\s*;\s*rel=\"?next\"?", link)
        if not found:
            return ""
        return urllib.parse.urljoin(f"https://{reference.registry}/", found.group(1))

    @staticmethod
    def _json(response: Response, reference: Reference, *, what: str) -> dict[str, Any]:
        try:
            document = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise RegistryError(
                f"{reference.registry} answered with a {what} that is not JSON.",
                hint="the response cannot be read — the host may not be a registry",
            ) from error
        if not isinstance(document, dict):
            raise RegistryError(
                f"{reference.registry} answered with a {what} that is not an object.",
                hint="the response cannot be read — the host may not be a registry",
            )
        return document

    def _urlopen(self, url: str, headers: Mapping[str, str], timeout: float) -> Response:
        """The real HTTP call. A ``401``/``404`` is an answer, not an exception."""
        request = urllib.request.Request(url, headers=dict(headers), method="GET")  # noqa: S310
        opener = urllib.request.build_opener(_NoCrossHostAuth)
        try:
            with opener.open(request, timeout=timeout) as answer:  # noqa: S310
                return Response(
                    status=answer.status,
                    headers=_Headers(dict(answer.headers.items())),
                    body=answer.read(),
                )
        except urllib.error.HTTPError as error:
            return Response(
                status=error.code,
                headers=_Headers(dict(error.headers.items()) if error.headers else {}),
                body=error.read(),
            )
        except (urllib.error.URLError, OSError) as error:
            host = urllib.parse.urlsplit(url).netloc
            raise RegistryUnreachable(
                f"MCUHome cannot reach {host}: {error}.",
                hint=(
                    "the build environment is chosen by asking the registry which "
                    "image it currently recommends, which needs network access. Pin "
                    "the environment to a digest to build without asking:\n"
                    "    mcuhome device build --container-image <repository>@sha256:…"
                ),
            ) from error
