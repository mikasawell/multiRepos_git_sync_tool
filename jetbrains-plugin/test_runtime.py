#!/usr/bin/env python3
"""Run manual interpreter and native Java/Python bridge regressions with a local IDE SDK."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ide-home", required=True)
    parser.add_argument("--jdk-home")
    args = parser.parse_args()
    project = Path(__file__).resolve().parent
    sdk = Path(args.ide_home).expanduser().resolve()
    if (sdk / "Contents").is_dir():
        sdk = sdk / "Contents"
    jdk = Path(args.jdk_home).expanduser().resolve() if args.jdk_home else sdk / "jbr"
    if (jdk / "Contents" / "Home").is_dir():
        jdk = jdk / "Contents" / "Home"
    suffix = ".exe" if os.name == "nt" else ""
    java, compiler = (jdk / "bin" / (name + suffix) for name in ("java", "javac"))
    jars = os.pathsep.join(str(path) for path in sorted((sdk / "lib").glob("*.jar")))
    package = Path("local/multirepo/sync")
    sources = [project / "src/main/java" / package / (name + ".java") for name in ("PythonCommand", "Bridge")]
    sources.append(project / "src/test/java" / package / "ManualPythonTest.java")
    with tempfile.TemporaryDirectory(prefix="git-sync-runtime-test-") as temporary:
        base = Path(temporary)
        classes = base / "classes"
        classes.mkdir()
        resources = classes / "engine"
        resources.mkdir()
        for name in ("git_sync.py", "snapshot_sync.py", "ide_bridge.py"):
            shutil.copyfile(project.parent / name, resources / name)
        subprocess.run([str(compiler), "--release", "21", "-encoding", "UTF-8", "-proc:none",
                        "-classpath", jars, "-d", str(classes), *map(str, sources)], check=True)
        subprocess.run([str(java), "-classpath", str(classes) + os.pathsep + jars,
                        "local.multirepo.sync.ManualPythonTest", sys.executable, str(base)], check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
