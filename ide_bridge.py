#!/usr/bin/env python3
"""JSON-only local subprocess protocol used by the JetBrains plugin."""
import argparse
import base64
import hashlib
from dataclasses import asdict
import json
from pathlib import Path
import sys

import git_sync as core
import snapshot_sync as snapshot


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("operation", choices=("get", "save", "prepare", "resolve", "apply", "discard", "auto", "auto-all"))
    parser.add_argument("--repo")
    args = parser.parse_args()
    path = Path(args.config).expanduser().resolve()
    try:
        if args.operation == "get":
            result = {"exists": path.exists(), "config": core.load_config(path).as_dict() if path.exists() else None,
                      "revision": hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None}
        elif args.operation == "save":
            core.check_git()
            request = json.load(sys.stdin)
            revision = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
            if revision != request.get("_expected_revision"):
                raise core.ConfigError("配置已存在或已被其他进程修改，请先重新加载配置，再保存。")
            config = core.parse_config(request, path)
            core.write_config(path, config)
            result = {"config": config.as_dict(), "revision": hashlib.sha256(path.read_bytes()).hexdigest()}
        elif args.operation == "auto-all":
            config = core.load_config(path)
            core.setup_logging(path, config.log_directory)
            results = []
            for repo in core.select_repositories(config, None):
                if repo.strategy != "snapshot":
                    outcome = core.Outcome(repo.name, "BLOCKED", "请先在界面保存为 snapshot 模式。")
                else:
                    outcome = core.sync_one(repo, config.git_timeout_seconds)
                item = {"name": repo.name, "outcome": asdict(outcome)}
                if outcome.status == "REVIEW_REQUIRED":
                    try:
                        item["plan"] = snapshot.prepare(repo, config.git_timeout_seconds)
                    except (core.SyncError, OSError) as exc:
                        outcome = core.Outcome(repo.name, "BLOCKED", core.redact(str(exc)))
                        item["outcome"] = asdict(outcome)
                results.append(item)
                core.LOG.info("[%s] %s: %s", repo.name, outcome.status, outcome.detail)
            result = {"results": results}
        else:
            core.check_git()
            config = core.load_config(path)
            repo = core.select_repositories(config, args.repo)
            if len(repo) != 1 or not args.repo:
                raise core.ConfigError("请选择一个同步仓库。")
            repo = repo[0]
            if repo.strategy != "snapshot":
                raise core.ConfigError("插件使用 snapshot 文件同步模式，请在配置中保存该模式。")
            core.setup_logging(path, config.log_directory)
            timeout = config.git_timeout_seconds
            if args.operation == "prepare":
                result = {"plan": snapshot.prepare(repo, timeout)}
            elif args.operation == "resolve":
                request = json.load(sys.stdin)
                merged = request.get("content_base64")
                content = base64.b64decode(merged, validate=True) if merged is not None else None
                result = {"plan": snapshot.resolve(repo, request["path"], request["choice"], timeout, content)}
            elif args.operation == "apply":
                outcome = snapshot.apply(repo, timeout)
                core.LOG.info("[%s] %s", repo.name, outcome.detail)
                result = {"outcome": asdict(outcome)}
            elif args.operation == "discard":
                snapshot.discard(repo, timeout)
                result = {"discarded": True}
            else:
                outcome = core.sync_one(repo, timeout)
                core.LOG.info("[%s] %s: %s", repo.name, outcome.status, outcome.detail)
                result = {"outcome": asdict(outcome)}
                if outcome.status == "REVIEW_REQUIRED":
                    result["plan"] = snapshot.prepare(repo, timeout)
            core.LOG.info("[%s] %s", repo.name, args.operation)
        print(json.dumps({"ok": True, **result}, ensure_ascii=False))
        return 0
    except (core.SyncError, OSError, ValueError, KeyError) as exc:
        error = core.redact(str(exc))
        core.LOG.warning("%s: %s", args.operation, error)
        print(json.dumps({"ok": False, "error": error}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
