#!/usr/bin/env python3
"""Build an installable plugin ZIP using a local IntelliJ-platform IDE SDK (no Gradle download)."""
import argparse
from pathlib import Path
import os
import xml.etree.ElementTree as ET
import shutil
import subprocess
import sys
import zipfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ide-home", required=True, help="IDE root containing lib/, e.g. PyCharm.app/Contents")
    parser.add_argument("--jdk-home", help="JDK 21+ containing bin/javac; defaults to the IDE's bundled JBR")
    args = parser.parse_args()
    project = Path(__file__).resolve().parent
    root = project.parent
    sdk = Path(args.ide_home).expanduser().resolve()
    if (sdk / "Contents").is_dir():
        sdk = sdk / "Contents"
    jdk = Path(args.jdk_home).expanduser().resolve() if args.jdk_home else sdk / "jbr"
    if (jdk / "Contents" / "Home").is_dir():
        jdk = jdk / "Contents" / "Home"
    compiler = jdk / "bin" / ("javac.exe" if os.name == "nt" else "javac")
    if not compiler.is_file():
        parser.error("未找到 javac，请通过 --jdk-home 指定 JDK 21 或更新版本。")
    output = project / "build"
    if output.exists():
        shutil.rmtree(output)
    classes = output / "classes"
    classes.mkdir(parents=True)
    jars = sorted((sdk / "lib").glob("*.jar"))
    if not jars:
        parser.error("IDE 路径下没有 lib/*.jar。")
    sources = sorted((project / "src" / "main" / "java").rglob("*.java"))
    subprocess.run([str(compiler), "--release", "21", "-encoding", "UTF-8", "-proc:none", "-classpath",
                    os.pathsep.join(map(str, jars)), "-d", str(classes), *map(str, sources)], check=True)
    plugin_jar = output / "multi-repo-sync.jar"
    with zipfile.ZipFile(plugin_jar, "w", zipfile.ZIP_DEFLATED) as archive:
        for directory in (classes, project / "src" / "main" / "resources"):
            for path in sorted(directory.rglob("*")):
                if path.is_file():
                    archive.write(path, str(path.relative_to(directory)).replace(os.sep, "/"))
        for filename in ("git_sync.py", "snapshot_sync.py", "ide_bridge.py"):
            archive.write(root / filename, "engine/" + filename)
        archive.write(root / "LICENSE", "META-INF/LICENSE")
    distribution = root / "dist"
    distribution.mkdir(exist_ok=True)
    version = ET.parse(project / "src" / "main" / "resources" / "META-INF" / "plugin.xml").getroot().findtext("version")
    target = distribution / f"multi-repo-sync-{version}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(plugin_jar, "multi-repo-sync/lib/multi-repo-sync.jar")
    print(target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
