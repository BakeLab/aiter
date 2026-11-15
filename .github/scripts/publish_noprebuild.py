#!/usr/bin/env python3
"""Publish the no-prebuild wheel and its immutable per-commit manifest."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import Request, urlopen


GITEA_HOST = "gitea.int.bb.pub"
GITEA_ORIGIN = f"https://{GITEA_HOST}"
GITEA_PACKAGES = f"{GITEA_ORIGIN}/api/packages"
PYPI_UPLOAD = f"{GITEA_PACKAGES}/BakeLab/pypi/"
PYPI_SIMPLE = f"{GITEA_PACKAGES}/BakeLab/pypi/simple/"
MANIFEST_PACKAGE = "aiter-noprebuild-manifest"


class WheelLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)


def published_wheel_url(wheel_name: str) -> str:
    project_url = PYPI_SIMPLE + "amd-aiter/"
    request = Request(project_url, headers={"Accept": "text/html"})
    with urlopen(request, timeout=30) as response:
        page = response.read(16 * 1024 * 1024 + 1)
    if len(page) > 16 * 1024 * 1024:
        raise RuntimeError("Gitea PyPI project page is too large")
    parser = WheelLinks()
    parser.feed(page.decode("utf-8"))
    for href in parser.links:
        url = urljoin(project_url, href)
        parsed = urlsplit(url)
        if (parsed.scheme == "https" and parsed.netloc == GITEA_HOST
                and Path(parsed.path).name == wheel_name):
            return url
    raise RuntimeError(f"published wheel is missing from Gitea Simple index: {wheel_name}")


def commit_timestamp(commit: str) -> str:
    value = subprocess.run(
        ["git", "show", "-s", "--format=%cI", commit],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return datetime.fromisoformat(value).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def put_immutable_manifest(path: Path, *, commit: str, token: str) -> str:
    package = quote(MANIFEST_PACKAGE, safe="")
    version = quote(commit, safe="")
    filename = quote(path.name, safe="")
    route = f"/api/packages/BakeLab/generic/{package}/{version}/{filename}"
    body = path.read_bytes()
    digest = hashlib.sha256(body).hexdigest()

    connection = http.client.HTTPSConnection(GITEA_HOST, timeout=60)
    try:
        connection.request("PUT", route, body=body, headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
        })
        response = connection.getresponse()
        status = response.status
        response.read(65536)
    finally:
        connection.close()

    if status == 409:
        connection = http.client.HTTPSConnection(GITEA_HOST, timeout=60)
        try:
            connection.request("GET", route, headers={"Authorization": f"Bearer {token}"})
            response = connection.getresponse()
            existing = response.read(16 * 1024 * 1024 + 1)
            if response.status != 200:
                raise RuntimeError(f"cannot inspect existing manifest: HTTP {response.status}")
        finally:
            connection.close()
        if hashlib.sha256(existing).hexdigest() != digest:
            raise RuntimeError("immutable manifest already exists with different content")
    elif status != 201:
        raise RuntimeError(f"Gitea Generic Packages upload failed: HTTP {status}")

    url = GITEA_ORIGIN + route
    print(f"Manifest: {url} sha256:{digest}")
    return url


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: publish_noprebuild.py WHEEL_DIRECTORY")
    commit = os.environ["GITHUB_SHA"]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError("GITHUB_SHA must be a full commit SHA")

    wheels = sorted(Path(sys.argv[1]).glob("amd_aiter*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"expected exactly one amd_aiter wheel, found {len(wheels)}")
    wheel = wheels[0]

    sys.path.insert(0, str(Path(".shared-publisher").resolve()))
    import pypi_publish

    pypi_publish.publish_files(
        [wheel],
        upload_url=PYPI_UPLOAD,
        check_url=PYPI_UPLOAD.rstrip("/"),
        token=None,
        dry_run=False,
    )

    wheel_url = published_wheel_url(wheel.name)
    manifest = {
        "branch": os.environ["GITHUB_REF_NAME"],
        "timestamp": commit_timestamp(commit),
        "commit": commit,
        "wheels": {"py3": {"wheel_name": wheel.name, "wheel_url": wheel_url}},
        "wheel_name": wheel.name,
        "wheel_url": wheel_url,
    }
    manifest_path = Path("latest-noprebuild.json")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Manifest data: {json.dumps(manifest, indent=2)}")

    token = pypi_publish.ci_identity_token(pypi_publish.GITEA_PACKAGES)
    put_immutable_manifest(manifest_path, commit=commit, token=token)


if __name__ == "__main__":
    main()
