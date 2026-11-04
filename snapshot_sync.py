"""Reviewed tree synchronization; does not require shared source/target history."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tempfile
from typing import Dict, Optional
import uuid

import git_sync as core


@dataclass(frozen=True)
class FileVersion:
    mode: str
    oid: str


def raw_git(repo, *args, timeout=120, data=None, extra_env=None, check=True):
    environment = core.git_env()
    environment.update(extra_env or {})
    try:
        result = subprocess.run(["git", "-C", str(repo), *args], input=data,
                                stdin=subprocess.DEVNULL if data is None else None,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=environment, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise core.GitError("Git 操作超时，请检查仓库状态后重试。") from exc
    if check and result.returncode:
        raise core.GitError(core.redact(result.stderr.decode("utf-8", "replace").strip()))
    return result


def valid_path(path):
    if not isinstance(path, str) or not path or "\x00" in path or "\\" in path:
        raise core.SyncError("不支持的 Git 文件路径。")
    parts = PurePosixPath(path).parts
    if path.startswith("/") or any(p in ("..", ".git") or p.lower() == ".git" for p in parts):
        raise core.SyncError(f"不安全的 Git 文件路径：{path!r}")
    return path


def tree(repo, ref, timeout):
    if not ref:
        return {}
    output = raw_git(repo, "ls-tree", "-rz", "--full-tree", ref, timeout=timeout).stdout
    files = {}
    for line in output.split(b"\x00"):
        if not line:
            continue
        meta, raw_path = line.split(b"\t", 1)
        try:
            path = valid_path(raw_path.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise core.SyncError("文件名不是 UTF-8，无法安全同步。") from exc
        mode, _, object_id = meta.decode("ascii").split()
        files[path] = FileVersion(mode, object_id)
    return files


def content(repo, version, timeout):
    if version is None:
        return b""
    if version.mode == "160000":
        return ("Submodule commit: " + version.oid + "\n").encode()
    return raw_git(repo, "cat-file", "blob", version.oid, timeout=timeout).stdout


def config_fingerprint(repo):
    return hashlib.sha256(json.dumps(repo.as_dict(), sort_keys=True).encode()).hexdigest()


def plan_directory(repo, timeout):
    common = Path(core.git(repo.path, "rev-parse", "--git-common-dir", timeout=timeout).stdout.strip())
    if not common.is_absolute():
        common = repo.path / common
    return common.resolve() / "git-sync-tool" / "plans" / repo.ref_prefix.rsplit("/", 1)[-1]


def save_plan(directory, plan):
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / "plan.json.tmp"
    temporary.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, directory / "plan.json")


def read_plan(repo, timeout):
    directory = plan_directory(repo, timeout)
    path = directory / "plan.json"
    if not path.exists():
        return None
    plan = json.loads(path.read_text(encoding="utf-8"))
    if plan.get("fingerprint") != config_fingerprint(repo):
        raise core.SyncError("配置已变更，请先丢弃旧同步计划，再重新检查。")
    return plan


def preserve(path, patterns):
    return any(fnmatch.fnmatchcase(path, pattern) or path.startswith(pattern.rstrip("/") + "/")
               for pattern in patterns)


def text_file(data):
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def mode_for(base, local, source):
    if local and source and local.mode == source.mode:
        return local.mode
    if base and local and local.mode == base.mode and source:
        return source.mode
    if base and source and source.mode == base.mode and local:
        return local.mode
    return None


def public_plan(repo, plan, timeout):
    directory = plan_directory(repo, timeout)
    result = dict(plan)
    result["directory"] = str(directory)
    result["pending_count"] = sum(entry["choice"] == "pending" for entry in plan["entries"])
    result["change_count"] = sum(entry["choice"] not in ("local", "preserve") for entry in plan["entries"])
    return result


def prepare_locked(repo, timeout, *, manual_review=False):
    head = core.preflight(repo, timeout)
    existing = read_plan(repo, timeout)
    if existing:
        if head not in (existing["head"], existing.get("candidate")):
            raise core.SyncError("计划创建后本地提交已变化，请丢弃旧计划并重新检查；已编辑结果仍在暂存目录。")
        if manual_review and not existing.get("manual_review"):
            existing["manual_review"] = True
            save_plan(plan_directory(repo, timeout), existing)
        return public_plan(repo, existing, timeout)
    directory = plan_directory(repo, timeout)
    if directory.exists():
        # A crash before plan.json was published: it contains no reviewed plan.
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    incoming_ref = repo.ref_prefix + "/incoming"
    core.git(repo.path, "fetch", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head",
             "--", repo.source, f"+refs/heads/{repo.source_branch}:{incoming_ref}", timeout=timeout)
    incoming = core.oid(repo.path, incoming_ref, timeout)
    baseline = core.oid(repo.path, repo.ref_prefix + "/baseline-source", timeout)
    source_files, local_files, base_files = (tree(repo.path, ref, timeout) for ref in (incoming, head, baseline))
    entries = []
    for path in sorted(set(source_files) | set(local_files) | set(base_files)):
        source, local, base = source_files.get(path), local_files.get(path), base_files.get(path)
        if preserve(path, repo.preserve_paths):
            if source != local:
                entries.append({"path": path, "choice": "preserve", "reason": "按保留路径保留内网版本",
                                "source": asdict(source) if source else None,
                                "local": asdict(local) if local else None, "base": asdict(base) if base else None})
            continue
        if source == local:
            continue
        file_id = hashlib.sha256(path.encode()).hexdigest()
        file_dir = directory / file_id
        file_dir.mkdir()
        versions = {"source": source, "local": local, "base": base}
        data = {key: content(repo.path, value, timeout) for key, value in versions.items()}
        for key, value in data.items():
            (file_dir / key).write_bytes(value)
        (file_dir / "result").write_bytes(data["local"])
        entry = {"path": path, "id": file_id, "choice": "pending", "reason": "需要人工确认",
                 "binary": any(not text_file(value) for value in data.values()),
                 "mergeable": bool(local and source and local.mode in ("100644", "100755")
                                   and source.mode in ("100644", "100755")
                                   and all(text_file(value) for value in data.values())),
                 **{key: asdict(value) if value else None for key, value in versions.items()}}
        if baseline is None:
            # No common baseline: additions are safe, other differences need explicit decisions.
            if local is None:
                entry.update(choice="source", reason="来源新增文件")
            else:
                entry["reason"] = "首次同步：确认内网差异" if source else "首次同步：本地独有文件，确认保留或删除"
        elif source == base:
            entry.update(choice="local", reason="来源未改变，保留内网修改")
        elif local == base:
            entry.update(choice="source", reason="仅来源发生变化" if source else "来源删除文件")
        elif entry["mergeable"] and base and mode_for(base, local, source):
            merged = raw_git(repo.path, "merge-file", "-p", str(file_dir / "local"),
                             str(file_dir / "base"), str(file_dir / "source"), timeout=timeout, check=False)
            if merged.returncode == 0:
                (file_dir / "result").write_bytes(merged.stdout)
                entry.update(choice="merged", mode=mode_for(base, local, source), reason="三方合并无冲突")
            elif merged.returncode > 127 or merged.returncode < 0:
                raise core.GitError("文件三方合并失败：" + merged.stderr.decode("utf-8", "replace"))
            else:
                entry["reason"] = "来源和内网修改发生冲突"
        entries.append(entry)
    rewritten = bool(baseline and not core.ancestor(repo.path, baseline, incoming, timeout))
    plan = {"version": 1, "id": uuid.uuid4().hex, "fingerprint": config_fingerprint(repo),
            "head": head, "source_oid": incoming, "baseline": baseline, "first_sync": baseline is None,
            "source_rewritten": rewritten, "manual_review": manual_review, "entries": entries}
    save_plan(directory, plan)
    return public_plan(repo, plan, timeout)


def prepare(repo, timeout=120):
    core.repository_root(repo.path, timeout)
    with core.repository_lock(repo.path, timeout):
        return prepare_locked(repo, timeout, manual_review=True)


def resolve(repo, path, choice, timeout=120, merged_content=None):
    with core.repository_lock(repo.path, timeout):
        plan = read_plan(repo, timeout)
        if plan is None:
            raise core.SyncError("没有待处理计划，请先检查同步。")
        if core.preflight(repo, timeout) != plan["head"]:
            raise core.SyncError("本地提交已变化，请丢弃旧计划并重新检查。")
        entry = next((item for item in plan["entries"] if item["path"] == path), None)
        if not entry or entry["choice"] == "preserve":
            raise core.SyncError("找不到可处理的文件，或文件命中了保留路径。")
        if choice not in ("source", "local", "merged", "delete"):
            raise core.SyncError("无效的处理方式。")
        if choice == "merged":
            if not entry.get("mergeable") or merged_content is None or not text_file(merged_content):
                raise core.SyncError("只有普通 UTF-8 文本文件可以使用文本合并结果。")
            if any(line.startswith((b"<<<<<<< ", b">>>>>>> ")) for line in merged_content.splitlines()):
                raise core.SyncError("合并结果仍有冲突标记，请先解决。")
            (plan_directory(repo, timeout) / entry["id"] / "result").write_bytes(merged_content)
            entry["mode"] = (entry.get("local") or entry["source"])["mode"]
        entry["choice"] = choice
        entry["reason"] = "用户已确认"
        plan["manual_review"] = True
        plan.pop("candidate", None)
        save_plan(plan_directory(repo, timeout), plan)
        return public_plan(repo, plan, timeout)


def discard(repo, timeout=120):
    with core.repository_lock(repo.path, timeout):
        directory = plan_directory(repo, timeout)
        if directory.exists():
            shutil.rmtree(directory)


def apply_locked(repo, timeout, *, reviewed=False):
    plan = read_plan(repo, timeout)
    if plan is None:
        raise core.SyncError("没有待应用计划。")
    if (plan["first_sync"] or plan["source_rewritten"]) and not reviewed:
        raise core.SyncError("首次同步或来源历史改写，需要在插件界面确认。")
    if any(entry["choice"] == "pending" for entry in plan["entries"]):
        raise core.SyncError("仍有未处理文件，请先逐一确认。")
    head = core.preflight(repo, timeout)
    if head not in (plan["head"], plan.get("candidate")):
        raise core.SyncError("本地提交已变化，请先丢弃旧计划并重新检查。")
    current_baseline = core.oid(repo.path, repo.ref_prefix + "/baseline-source", timeout)
    if (current_baseline == plan["source_oid"]
            and core.oid(repo.path, repo.ref_prefix + "/baseline-local", timeout) == head
            and head == plan.get("candidate", plan["head"])):
        shutil.rmtree(plan_directory(repo, timeout))
        return core.Outcome(repo.name, "UP_TO_DATE", "上次同步已完成，遗留暂存计划已清理。")
    if current_baseline != plan["baseline"]:
        raise core.SyncError("同步基线已变化，请重新检查。")
    directory = plan_directory(repo, timeout)
    result = tree(repo.path, plan["head"], timeout)
    for entry in plan["entries"]:
        path = valid_path(entry["path"])
        choice = entry["choice"]
        if choice in ("local", "preserve"):
            continue
        if choice == "source":
            version = entry["source"]
            if version:
                result[path] = FileVersion(**version)
            else:
                result.pop(path, None)
        elif choice == "delete":
            result.pop(path, None)
        elif choice == "merged":
            data = (directory / entry["id"] / "result").read_bytes()
            object_id = raw_git(repo.path, "hash-object", "-w", "--stdin", data=data,
                                timeout=timeout).stdout.decode().strip()
            result[path] = FileVersion(entry["mode"], object_id)
        else:
            raise core.SyncError("计划含有无效的处理方式。")
    # Avoid ambiguous directory/file choices and case collisions before touching the worktree.
    insensitive = core.git(repo.path, "config", "--bool", "--get", "core.ignorecase", check=False,
                           timeout=timeout).stdout.strip() == "true" or os.name == "nt"
    normalized = {}
    for path in result:
        key = path.casefold() if insensitive else path
        if key in normalized and normalized[key] != path:
            raise core.SyncError(f"文件路径存在大小写碰撞：{path} / {normalized[key]}")
        normalized[key] = path
    for path in normalized:
        for parent in PurePosixPath(path).parents:
            if str(parent) in normalized:
                raise core.SyncError(f"文件与目录重名：{path}；请调整相关文件的保留/删除选择。")
    core.protect_ignored_files(repo, list(result), timeout)
    with tempfile.TemporaryDirectory(prefix="git-sync-index-") as temporary:
        environment = {"GIT_INDEX_FILE": str(Path(temporary) / "index")}
        raw_git(repo.path, "read-tree", "--empty", timeout=timeout, extra_env=environment)
        index_data = b"".join(f"{version.mode} {version.oid}\t{path}".encode("utf-8") + b"\x00"
                              for path, version in sorted(result.items()))
        raw_git(repo.path, "update-index", "-z", "--index-info", data=index_data,
                timeout=timeout, extra_env=environment)
        result_tree = raw_git(repo.path, "write-tree", timeout=timeout, extra_env=environment).stdout.decode().strip()
    original_tree = core.git(repo.path, "rev-parse", plan["head"] + "^{tree}", timeout=timeout).stdout.strip()
    candidate = plan.get("candidate")
    if result_tree != original_tree:
        if candidate is None:
            candidate = raw_git(repo.path, "commit-tree", result_tree, "-p", plan["head"],
                                "-m", f"Sync {repo.source_branch} ({plan['source_oid'][:12]})",
                                timeout=timeout).stdout.decode().strip()
            plan["candidate"] = candidate
            save_plan(directory, plan)
        if head == plan["head"]:
            if core.preflight(repo, timeout) != head:
                raise core.SyncError("应用前本地提交已变化。")
            core.protect_ignored_files(repo, list(result), timeout)
            backup = core.backup_branch(repo, head, timeout)
            operation = core.git(repo.path, *core.merge_args(repo), "--ff-only", candidate,
                                 timeout=timeout, check=False)
            if operation.returncode:
                raise core.GitError(core.redact(operation.stderr.strip() or operation.stdout.strip())
                                    + f"\n本地恢复引用：{backup}；计划已保留。")
        if core.oid(repo.path, "HEAD", timeout) != candidate:
            raise core.SyncError("应用后 HEAD 不符合预期，请检查 Git hooks 和本地状态；计划已保留。")
    else:
        candidate = plan["head"]
    # One ref transaction makes baseline recovery atomic. The plan retains candidate if interrupted.
    old = plan["baseline"] or "0" * len(plan["source_oid"])
    commands = (f"start\nupdate {repo.ref_prefix}/baseline-source {plan['source_oid']} {old}\n"
                f"update {repo.ref_prefix}/baseline-local {candidate}\nprepare\ncommit\n").encode()
    raw_git(repo.path, "update-ref", "--stdin", data=commands, timeout=timeout)
    shutil.rmtree(directory)
    status = "UPDATED" if result_tree != original_tree else "UP_TO_DATE"
    return core.Outcome(repo.name, status, "文件同步完成；暂存计划已清理，没有创建或推送临时分支。")


def apply(repo, timeout=120):
    with core.repository_lock(repo.path, timeout):
        return apply_locked(repo, timeout, reviewed=True)


def sync_snapshot_locked(repo, timeout, *, dry_run=False):
    plan = prepare_locked(repo, timeout)
    if plan["first_sync"] or plan["source_rewritten"] or plan["pending_count"] or plan.get("manual_review"):
        return core.Outcome(repo.name, "REVIEW_REQUIRED",
                            f"待确认 {plan['pending_count']} 个文件，请在 Multi Repo Sync 插件中查看。")
    if dry_run:
        return core.Outcome(repo.name, "DRY_RUN", f"预计处理 {plan['change_count']} 个文件，本地工作区未修改。")
    return apply_locked(repo, timeout)
