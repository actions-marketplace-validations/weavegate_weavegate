#!/usr/bin/env python3
"""Set a tag version, assemble and inspect a signed Central artifact set."""

import argparse
import hashlib
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path


GROUP = "io.github.weavegate"
ARTIFACT = "weavegate-spring"
NS = {"m": "http://maven.apache.org/POM/4.0.0"}
VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?")


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
    print(f"JAVA_CENTRAL_BUNDLE_RESULT version={target} artifacts=4 signatures=valid checksums=valid metadata=valid upload=skipped")


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
    args = parser.parse_args()
    try:
        if args.command == "set-version":
            set_version(args.pom, args.version)
        elif args.command == "build-bundle":
            build_bundle(args.version, args.target, args.bundle)
        else:
            verify_bundle(args.bundle, args.version)
    except (ValueError, OSError, ET.ParseError, zipfile.BadZipFile) as error:
        print(f"java-central: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
