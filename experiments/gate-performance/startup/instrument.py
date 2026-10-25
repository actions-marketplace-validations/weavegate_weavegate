#!/usr/bin/env python3
"""Build a disposable, numeric-only startup probe from the current SDK Peer.

The production SDK is unchanged. Both arms must use the same probe JAR.
Use an unmodified JAR for whole-gate comparisons.
"""
import argparse
import io
import os
from pathlib import Path
import subprocess
import tempfile
import zipfile


def instrument(source):
    replacements = {
        "            host.initialize(config);":
        """            long initializeStart = System.nanoTime();
            host.initialize(config);
            long initializeEnd = System.nanoTime();""",
        "            validated = host.validateRegistration(config.commands(), config.points());":
        """            long registrationStart = System.nanoTime();
            validated = host.validateRegistration(config.commands(), config.points());
            long registrationEnd = System.nanoTime();""",
        "                host.probeDatabase();":
        """                long probeStart = System.nanoTime();
                host.probeDatabase();
                long probeEnd = System.nanoTime();
                String row = "{\\\"initialize_ns\\\":" + (initializeEnd - initializeStart)
                        + ",\\\"registration_ns\\\":" + (registrationEnd - registrationStart)
                        + ",\\\"probe_ns\\\":" + (probeEnd - probeStart) + "}\\n";
                java.nio.file.Files.writeString(
                        java.nio.file.Path.of(System.getenv("WEAVEGATE_STARTUP_PROBE")), row,
                        java.nio.file.StandardOpenOption.CREATE,
                        java.nio.file.StandardOpenOption.APPEND);""",
    }
    for before, after in replacements.items():
        if source.count(before) != 1:
            raise ValueError("startup source boundary changed")
        source = source.replace(before, after)
    return source


def replace_entries(original, replacements):
    result = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(original)) as src, zipfile.ZipFile(result, "w") as dst:
        if len(src.namelist()) != len(set(src.namelist())):
            raise ValueError("duplicate JAR entry")
        for entry in src.infolist():
            dst.writestr(entry, replacements.pop(entry.filename, src.read(entry)))
        for name, data in replacements.items():
            dst.writestr(name, data)
    return result.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jar", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3]
    source = root / "sdk/java/src/main/java/io/github/weavegate/sdk/Peer.java"
    with tempfile.TemporaryDirectory(prefix="weavegate-startup-probe-") as temp:
        temp = Path(temp)
        with zipfile.ZipFile(args.jar) as jar:
            libraries = [n for n in jar.namelist() if n.startswith("BOOT-INF/lib/") and n.endswith(".jar")]
            sdk, = [n for n in libraries if Path(n).name.startswith("weavegate-spring-")]
            paths = []
            for i, name in enumerate(libraries):
                path = temp / f"library-{i}.jar"
                path.write_bytes(jar.read(name))
                paths.append(str(path))
            peer = temp / "Peer.java"
            peer.write_text(instrument(source.read_text()))
            classes = temp / "classes"
            subprocess.run(["javac", "--release", "21", "-parameters", "-g", "-cp",
                            os.pathsep.join(paths), "-d", str(classes), str(peer)], check=True)
            replacements = {p.relative_to(classes).as_posix(): p.read_bytes()
                            for p in classes.rglob("*.class")}
            patched_sdk = replace_entries(jar.read(sdk), replacements)
        with args.output.open("xb") as output:
            output.write(replace_entries(args.jar.read_bytes(), {sdk: patched_sdk}))


if __name__ == "__main__":
    main()
