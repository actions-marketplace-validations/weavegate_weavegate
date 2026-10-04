#!/usr/bin/env python3
"""Set a tag version, assemble, verify, and publish a signed Central bundle."""

import argparse
import base64
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path


GROUP = "io.github.weavegate"
ARTIFACT = "weavegate-spring"
NS = {"m": "http://maven.apache.org/POM/4.0.0"}
VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?")
CENTRAL_URL = "https://central.sonatype.com"
REPOSITORY_URL = "https://repo.maven.apache.org/maven2"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def version(value):
    require(VERSION_RE.fullmatch(value) is not None, f"invalid release version: {value}")
    require(not value.endswith("-SNAPSHOT"), "snapshot versions cannot be published")
    return value


def set_version(pom_path, target):
    version(target)
    source = pom_path.read_text(encoding="utf-8")
    pattern = re.compile(
        r"(<groupId>io\.github\.weavegate</groupId>\s*"
        r"<artifactId>weavegate-spring</artifactId>\s*<version>)"
        r"([^<]+)(</version>)"
    )
    matches = list(pattern.finditer(source))
    require(len(matches) == 1, "expected exactly one weavegate-spring project version")
    old = matches[0].group(2)
    require(old == "0.0.0-SNAPSHOT" or old == target, f"unexpected project version: {old}")
    updated = source[: matches[0].start(2)] + target + source[matches[0].end(2) :]
    pom_path.write_text(updated, encoding="utf-8")


def xml_value(root, path):
    value = root.findtext(path, namespaces=NS)
    require(bool(value and value.strip()), f"missing POM metadata: {path}")
    return value.strip()


def build_bundle(target, directory, output):
    version(target)
    prefix = f"io/github/weavegate/{ARTIFACT}/{target}/{ARTIFACT}-{target}"
    source_files = {
        ".pom": Path("sdk/java/pom.xml"),
        ".jar": directory / f"{ARTIFACT}-{target}.jar",
        "-sources.jar": directory / f"{ARTIFACT}-{target}-sources.jar",
        "-javadoc.jar": directory / f"{ARTIFACT}-{target}-javadoc.jar",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for suffix, source in source_files.items():
            name = prefix + suffix
            signature = directory / f"{ARTIFACT}-{target}{suffix}.asc"
            data = source.read_bytes()
            require(data and signature.is_file(), f"missing signed artifact: {source}")
            archive.writestr(name, data)
            archive.write(signature, name + ".asc")
            for algorithm in ("md5", "sha1", "sha256", "sha512"):
                archive.writestr(name + "." + algorithm, hashlib.new(algorithm, data).hexdigest())


def verify_bundle(bundle, target):
    version(target)
    prefix = f"io/github/weavegate/{ARTIFACT}/{target}/{ARTIFACT}-{target}"
    base = [f"{prefix}{suffix}" for suffix in (".pom", ".jar", "-sources.jar", "-javadoc.jar")]
    with zipfile.ZipFile(bundle) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)), "duplicate bundle paths")
        files = {name for name in names if not name.endswith("/")}
        expected = {name + suffix for name in base
                    for suffix in ("", ".asc", ".md5", ".sha1", ".sha256", ".sha512")}
        require(expected <= files, f"missing bundle files: {sorted(expected - files)}")
        require(files <= expected, f"unexpected bundle files: {sorted(files - expected)}")

        pom = ET.fromstring(archive.read(base[0]))
        for field, expected_value in (("m:groupId", GROUP), ("m:artifactId", ARTIFACT), ("m:version", target)):
            require(xml_value(pom, field) == expected_value, f"incorrect POM {field}")
        for field in ("m:name", "m:description", "m:url", "m:licenses/m:license/m:name",
                      "m:licenses/m:license/m:url", "m:developers/m:developer/m:name",
                      "m:developers/m:developer/m:url", "m:scm/m:connection", "m:scm/m:url"):
            xml_value(pom, field)
        require(xml_value(pom, "m:parent/m:groupId") == "org.springframework.boot", "unexpected POM parent")
        dependencies = {(xml_value(dep, "m:groupId"), xml_value(dep, "m:artifactId"))
                        for dep in pom.findall("m:dependencies/m:dependency", NS)}
        require(("org.springframework.boot", "spring-boot-starter-jdbc") in dependencies,
                "missing runtime Spring dependency")

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for name in base:
                data = archive.read(name)
                require(data, f"empty artifact: {name}")
                for algorithm in ("md5", "sha1", "sha256", "sha512"):
                    digest = archive.read(name + "." + algorithm).decode("ascii").strip()
                    require(digest == hashlib.new(algorithm, data).hexdigest(), f"bad {algorithm}: {name}")
                artifact_path = directory / Path(name).name
                signature_path = directory / (Path(name).name + ".asc")
                artifact_path.write_bytes(data)
                signature_path.write_bytes(archive.read(name + ".asc"))
                result = subprocess.run(["gpg", "--batch", "--quiet", "--verify", str(signature_path),
                                         str(artifact_path)], capture_output=True, text=True)
                require(result.returncode == 0, f"invalid GPG signature: {name}")
                if name.endswith(".jar"):
                    with zipfile.ZipFile(artifact_path) as jar:
                        members = jar.namelist()
                        if name.endswith("-sources.jar"):
                            require(any(item.endswith("/Weavegate.java") for item in members), "sources JAR lacks API")
                        elif name.endswith("-javadoc.jar"):
                            require("index.html" in members, "Javadoc JAR lacks index")
                        else:
                            require(any(item.endswith("/Weavegate.class") for item in members), "binary JAR lacks API")


def published_coordinate(data, target, repository_url=REPOSITORY_URL):
    """Verify published bytes and signatures after Portal deployment cleanup."""
    prefix = f"io/github/weavegate/{ARTIFACT}/{target}/{ARTIFACT}-{target}"
    names = [prefix + suffix for suffix in (".pom", ".jar", "-sources.jar", "-javadoc.jar")]
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        with tempfile.TemporaryDirectory() as temporary:
            for index, name in enumerate(names):
                url = repository_url + "/" + name
                try:
                    with urllib.request.urlopen(url, timeout=60) as response:
                        require(response.status == 200, f"public artifact returned HTTP {response.status}: {name}")
                        published = response.read()
                except urllib.error.HTTPError as error:
                    if index == 0 and error.code == 404:
                        return False
                    raise
                require(published == archive.read(name), f"published artifact differs from verified bundle: {name}")
                with urllib.request.urlopen(url + ".asc", timeout=60) as response:
                    require(response.status == 200, f"public signature returned HTTP {response.status}: {name}")
                    signature = response.read()
                artifact_path = Path(temporary) / Path(name).name
                signature_path = Path(temporary) / (Path(name).name + ".asc")
                artifact_path.write_bytes(published)
                signature_path.write_bytes(signature)
                result = subprocess.run(["gpg", "--batch", "--quiet", "--verify", str(signature_path),
                                         str(artifact_path)], capture_output=True, text=True)
                require(result.returncode == 0, f"invalid published signature: {name}")
    return True


def existing_deployment(name, target, token, base_url=CENTRAL_URL):
    """Find an accepted deployment for this exact tag and commit, across pages."""
    matches = set()
    page = 0
    while True:
        query = urllib.parse.urlencode({"namespace": GROUP, "deploymentName": name,
                                        "page": page, "size": 100})
        request = urllib.request.Request(base_url + "/api/v1/publisher/deployments?" + query,
                                         headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(request, timeout=60) as response:
            require(response.status == 200, f"Central deployment list returned HTTP {response.status}")
            listing = json.load(response)
        require(isinstance(listing, dict), "invalid Central deployment list")
        deployments = listing.get("deployments")
        page_count = listing.get("pageCount")
        require(isinstance(deployments, list) and isinstance(page_count, int) and page_count >= 0,
                "invalid Central deployment pagination")
        for item in deployments:
            require(isinstance(item, dict), "invalid Central deployment item")
            if item.get("deploymentName") != name or item.get("namespace") != GROUP:
                continue  # The API name filter is a case-insensitive substring match.
            components = item.get("deploymentComponents") or []
            purl = f"pkg:maven/{GROUP}/{ARTIFACT}@{target}"
            require(isinstance(components, list) and all(isinstance(component, dict) for component in components),
                    "invalid matching deployment components")
            require(all(component.get("purl") in (None, purl) for component in components),
                    "matching deployment has another coordinate")
            deployment_id = item.get("deploymentId")
            require(isinstance(deployment_id, str), "matching deployment lacks an ID")
            uuid.UUID(deployment_id)
            matches.add(deployment_id)
        page += 1
        if page >= page_count:
            break
    require(len(matches) <= 1, "multiple deployments match this release")
    return next(iter(matches), None)


def wait_for_publication(deployment_id, target, token, base_url=CENTRAL_URL):
    deadline = time.monotonic() + 1800
    status_url = base_url + "/api/v1/publisher/status?" + urllib.parse.urlencode({"id": deployment_id})
    while True:
        status = urllib.request.Request(status_url, data=b"",
                                        headers={"Authorization": f"Bearer {token}"}, method="POST")
        with urllib.request.urlopen(status, timeout=60) as response:
            require(response.status == 200, f"Central status returned HTTP {response.status}")
            result = json.load(response)
        require(isinstance(result, dict), "invalid Central status response")
        require(result.get("deploymentId") == deployment_id, "Central status returned another deployment")
        state = result.get("deploymentState")
        if state == "PUBLISHED":
            purl = f"pkg:maven/{GROUP}/{ARTIFACT}@{target}"
            require(purl in (result.get("purls") or []), "published deployment lacks expected coordinate")
            print(f"JAVA_CENTRAL_PUBLISH_RESULT version={target} deployment={deployment_id} state=published")
            return
        require(state in ("PENDING", "VALIDATING", "PUBLISHING"),
                f"Central deployment did not publish: {state}")
        require(time.monotonic() < deadline, "timed out waiting for Central publication")
        time.sleep(min(15, max(0, deadline - time.monotonic())))


def publish_bundle(bundle, target, username, password, release_revision, base_url=CENTRAL_URL,
                   repository_url=REPOSITORY_URL):
    # Verify and upload the same in-memory bytes. A second Maven invocation would
    # rebuild the artifacts and could upload a different, unverified bundle.
    require(re.fullmatch(r"[0-9a-f]{40}", release_revision) is not None, "invalid release revision")
    data = bundle.read_bytes()
    verify_bundle(io.BytesIO(data), target)
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    name = f"{ARTIFACT}-{target}-{release_revision}"
    try:
        deployment_id = existing_deployment(name, target, token, base_url)
    except urllib.error.URLError:
        if published_coordinate(data, target, repository_url):
            print(f"JAVA_CENTRAL_PUBLISH_RESULT version={target} state=published source=repository")
            return
        raise
    if deployment_id:
        wait_for_publication(deployment_id, target, token, base_url)
        return
    if published_coordinate(data, target, repository_url):
        print(f"JAVA_CENTRAL_PUBLISH_RESULT version={target} state=published source=repository")
        return
    boundary = "weavegate-" + uuid.uuid4().hex
    body = (f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="bundle"; filename="central-bundle.zip"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n").encode("ascii") + data + f"\r\n--{boundary}--\r\n".encode("ascii")
    headers = {"Authorization": f"Bearer {token}",
               "Content-Type": f"multipart/form-data; boundary={boundary}"}
    upload = urllib.request.Request(
        base_url + "/api/v1/publisher/upload?" + urllib.parse.urlencode({
            "name": name, "publishingType": "AUTOMATIC"}),
        data=body, headers=headers, method="POST")
    with urllib.request.urlopen(upload, timeout=60) as response:
        require(response.status == 201, f"Central upload returned HTTP {response.status}")
        deployment_id = response.read().decode("ascii").strip()
    try:
        uuid.UUID(deployment_id)
    except ValueError as error:
        raise ValueError("Central returned an invalid deployment ID") from error

    wait_for_publication(deployment_id, target, token, base_url)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    setter = commands.add_parser("set-version")
    setter.add_argument("version")
    setter.add_argument("--pom", type=Path, default=Path("sdk/java/pom.xml"))
    builder = commands.add_parser("build-bundle")
    builder.add_argument("version")
    builder.add_argument("--target", type=Path, default=Path("sdk/java/target"))
    builder.add_argument("--bundle", type=Path, default=Path("sdk/java/target/central-publishing/central-bundle.zip"))
    verifier = commands.add_parser("verify")
    verifier.add_argument("version")
    verifier.add_argument("--bundle", type=Path, default=Path("sdk/java/target/central-publishing/central-bundle.zip"))
    publisher = commands.add_parser("publish")
    publisher.add_argument("version")
    publisher.add_argument("--bundle", type=Path, default=Path("sdk/java/target/central-publishing/central-bundle.zip"))
    publisher.add_argument("--release-revision", required=True)
    args = parser.parse_args()
    try:
        if args.command == "set-version":
            set_version(args.pom, args.version)
        elif args.command == "build-bundle":
            build_bundle(args.version, args.target, args.bundle)
        elif args.command == "verify":
            verify_bundle(args.bundle, args.version)
            print(f"JAVA_CENTRAL_BUNDLE_RESULT version={args.version} artifacts=4 signatures=valid checksums=valid metadata=valid upload=skipped")
        else:
            username = os.environ.get("MAVEN_CENTRAL_USERNAME")
            password = os.environ.get("MAVEN_CENTRAL_PASSWORD")
            require(bool(username and password), "missing Central token credentials")
            publish_bundle(args.bundle, args.version, username, password, args.release_revision)
    except (ValueError, OSError, ET.ParseError, zipfile.BadZipFile,
            urllib.error.URLError, json.JSONDecodeError) as error:
        print(f"java-central: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
