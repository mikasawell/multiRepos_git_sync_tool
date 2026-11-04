#!/usr/bin/env python3
"""Pull-only synchronization for existing Git worktrees. Python 3.9+, Git 2.29+."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Dict, List, Optional, Sequence, Tuple
import unicodedata


LOG = logging.getLogger("git-sync")
LOG.addHandler(logging.NullHandler())
GOOD_STATUSES = {"UP_TO_DATE", "UPDATED", "AHEAD", "DRY_RUN"}
LABELS = {
    "UP_TO_DATE": "已同步", "UPDATED": "已更新", "AHEAD": "本地领先",
    "DRY_RUN": "预览", "BLOCKED": "已暂停", "CONFLICT": "有冲突", "ERROR": "失败",
    "REVIEW_REQUIRED": "等待界面确认",
}


class SyncError(Exception):
    pass


class ConfigError(SyncError):
    pass


class GitError(SyncError):
    pass


def redact(text: str) -> str:
    text = re.sub(r"(https?://)[^/\s@]+@", r"\1***@", text)
    return re.sub(r"(?i)([?&](?:access_token|token|password)=)[^&\s]+", r"\1***", text)


def git_env() -> Dict[str, str]:
    env = os.environ.copy()
    # A caller launched from a Git hook must not redirect operations to another repo/index.
    for key in (
        "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_NAMESPACE",
        "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_PREFIX",
    ):
        env.pop(key, None)
    env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never", GIT_PAGER="cat",
               GIT_MERGE_AUTOEDIT="no")
    return env


def git(path: Path, *args: str, timeout: int = 120,
        check: bool = True) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace", env=git_env(), timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise GitError("找不到 git，请先安装 Git 并加入 PATH。") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError("Git 操作超时；请检查网络和仓库状态后再试。") from exc
    except OSError as exc:
        raise GitError(redact(str(exc))) from exc
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise GitError(redact(detail or "Git 操作失败。"))
    return result


def check_git() -> None:
    output = git(Path.cwd(), "--version").stdout
    match = re.search(r"(\d+)\.(\d+)", output)
    if not match or tuple(map(int, match.groups())) < (2, 29):
        raise ConfigError("需要 Git 2.29 或更新版本。")


def validate_branch(branch: object, field: str) -> str:
    if (not isinstance(branch, str) or not branch or branch.startswith("-") or branch == "HEAD"
            or "\x00" in branch):
        raise ConfigError(f"{field} 不是有效的分支名。")
    if git(Path.cwd(), "check-ref-format", f"refs/heads/{branch}", check=False).returncode:
        raise ConfigError(f"{field} 不是有效的分支名：{branch!r}")
    return branch


@dataclass(frozen=True)
class Repository:
    name: str
    path: Path
    source: str
    source_branch: str
    target_branch: str
    strategy: str = "ff-only"
    enabled: bool = True
    preserve_paths: Tuple[str, ...] = ()

    @property
    def ref_prefix(self) -> str:
        identity = json.dumps([self.source, self.source_branch, self.target_branch], ensure_ascii=True)
        digest = hashlib.sha256(identity.encode()).hexdigest()[:24]
        return f"refs/git-sync/{digest}"

    def as_dict(self) -> dict:
        return {"name": self.name, "path": str(self.path), "source": self.source,
                "source_branch": self.source_branch, "target_branch": self.target_branch,
                "strategy": self.strategy, "enabled": self.enabled,
                "preserve_paths": list(self.preserve_paths)}


@dataclass(frozen=True)
class Configuration:
    repositories: List[Repository]
    interval_seconds: int = 60
    git_timeout_seconds: int = 120
    log_directory: Optional[Path] = None

    def as_dict(self) -> dict:
        return {"version": 1, "interval_seconds": self.interval_seconds,
                "git_timeout_seconds": self.git_timeout_seconds,
                "log_directory": str(self.log_directory) if self.log_directory is not None else None,
                "repositories": [repo.as_dict() for repo in self.repositories]}


def positive_int(value: object, field: str, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise ConfigError(f"{field} 必须是 >= {minimum} 的整数。")
    return value


def parse_repository(data: object, config_dir: Path) -> Repository:
    if not isinstance(data, dict):
        raise ConfigError("repositories 的每一项必须是对象。")
    name = data.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
        raise ConfigError("name 请使用 1–64 位英文字母、数字、点、横线或下划线，并以字母或数字开头。")
    raw_path = data.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip() or "\x00" in raw_path:
        raise ConfigError(f"{name}: path 必须是仓库路径。")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = config_dir / path
    source = data.get("source", "origin")
    if (not isinstance(source, str) or not source.strip() or source.startswith("-")
            or any(c in source for c in "\x00\r\n")):
        raise ConfigError(f"{name}: source 必须是 remote 名称或 Git URL。")
    strategy = data.get("strategy", "ff-only")
    if strategy not in ("ff-only", "merge", "snapshot"):
        raise ConfigError(f"{name}: strategy 只能是 ff-only、merge 或 snapshot。")
    enabled = data.get("enabled", True)
    if type(enabled) is not bool:
        raise ConfigError(f"{name}: enabled 必须是 true 或 false。")
    preserve = data.get("preserve_paths", [])
    if not isinstance(preserve, list) or any(not isinstance(p, str) or not p.strip()
                                            or "\x00" in p or "\\" in p
                                            or ".." in p.split("/") or p.startswith("/")
                                            for p in preserve):
        raise ConfigError(f"{name}: preserve_paths 必须是相对仓库根目录的路径/通配符数组，使用 / 分隔。")
    return Repository(name, path.resolve(), source,
                      validate_branch(data.get("source_branch"), "source_branch"),
                      validate_branch(data.get("target_branch"), "target_branch"), strategy, enabled,
                      tuple(p.strip() for p in preserve))


def load_config(path: Path) -> Configuration:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"配置不存在：{path}\n先运行 python3 git_sync.py init") from exc
    except (OSError, ValueError) as exc:
        raise ConfigError(f"无法读取配置：{exc}") from exc
    return parse_config(data, path)


def parse_config(data: object, path: Path) -> Configuration:
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
        raise ConfigError("配置 version 必须为 1。")
    raw_repos = data.get("repositories")
    if not isinstance(raw_repos, list) or not raw_repos:
        raise ConfigError("repositories 必须是非空数组。")
    repos = [parse_repository(item, path.parent) for item in raw_repos]
    if len({repo.name for repo in repos}) != len(repos):
        raise ConfigError("仓库 name 不能重复。")
    if len({os.path.normcase(str(repo.path)) for repo in repos}) != len(repos):
        raise ConfigError("同一个本地工作区只能配置一次。")
    raw_logs = data.get("log_directory")
    log_directory = None
    if raw_logs is not None:
        if not isinstance(raw_logs, str) or not raw_logs.strip() or "\x00" in raw_logs:
            raise ConfigError("log_directory 必须是目录路径或 null。")
        log_directory = Path(raw_logs).expanduser()
        if not log_directory.is_absolute():
            log_directory = path.parent / log_directory
        log_directory = log_directory.resolve()
    return Configuration(repos,
                         positive_int(data.get("interval_seconds", 60), "interval_seconds", 10),
                         positive_int(data.get("git_timeout_seconds", 120), "git_timeout_seconds", 1),
                         log_directory)


def write_config(path: Path, config: Configuration) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".git-sync-config-", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(config.as_dict(), handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def repository_root(path: Path, timeout: int = 120) -> Path:
    if not path.is_dir():
        raise SyncError(f"仓库目录不存在：{path}")
    result = git(path, "rev-parse", "--show-toplevel", timeout=timeout)
    root = Path(result.stdout.strip()).resolve()
    if root != path.resolve():
        raise SyncError(f"path 必须指向工作区根目录：{root}")
    return root


def git_path(path: Path, item: str, timeout: int) -> Path:
    result = Path(git(path, "rev-parse", "--git-path", item, timeout=timeout).stdout.strip())
    return result if result.is_absolute() else path / result


@contextmanager
def repository_lock(path: Path, timeout: int = 120):
    common = Path(git(path, "rev-parse", "--git-common-dir", timeout=timeout).stdout.strip())
    if not common.is_absolute():
        common = path / common
    with (common / "git-sync-tool.lock").open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise SyncError("另一个同步进程正在操作这个仓库，请稍后再试。") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def oid(path: Path, ref: str, timeout: int) -> Optional[str]:
    result = git(path, "rev-parse", "--verify", "--quiet", ref, timeout=timeout, check=False)
    if result.returncode == 1:
        return None
    if result.returncode:
        raise GitError(redact(result.stderr.strip() or "无法读取 Git 引用。"))
    return result.stdout.strip()


def ancestor(path: Path, older: str, newer: str, timeout: int) -> bool:
    result = git(path, "merge-base", "--is-ancestor", older, newer, timeout=timeout, check=False)
    if result.returncode not in (0, 1):
        raise GitError(redact(result.stderr.strip() or "无法判断提交关系。"))
    return result.returncode == 0


def current_branch(path: Path, timeout: int) -> Optional[str]:
    result = git(path, "symbolic-ref", "--quiet", "--short", "HEAD", timeout=timeout, check=False)
    if result.returncode == 1:
        return None
    if result.returncode:
        raise GitError(redact(result.stderr.strip() or "无法读取当前分支。"))
    return result.stdout.strip()


def preflight(repo: Repository, timeout: int) -> str:
    branch = current_branch(repo.path, timeout)
    if branch != repo.target_branch:
        raise SyncError(f"当前分支是 {branch or 'detached HEAD'}，需要你先切换到 {repo.target_branch}。")
    for operation in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge",
                      "rebase-apply", "sequencer", "BISECT_LOG"):
        if git_path(repo.path, operation, timeout).exists():
            raise SyncError(f"存在未完成的 Git 操作（{operation}），请先完成或撤销。")
    entries = git(repo.path, "ls-files", "-v", "-z", timeout=timeout).stdout.split("\x00")
    if any(entry and (entry[0].islower() or entry.startswith("S ")) for entry in entries):
        raise SyncError("索引含 assume-unchanged / skip-worktree 标记（或稀疏检出），"
                        "无法可靠检查本地修改；请先自行处理这些标记。")
    if git(repo.path, "status", "--porcelain=v1", "-z", "--untracked-files=all",
           "--ignore-submodules=none", timeout=timeout).stdout:
        raise SyncError("工作区有未提交、暂存或未跟踪的文件，请先自行处理；工具不会自动 stash。")
    head = oid(repo.path, "HEAD", timeout)
    if head is None:
        raise SyncError("本地分支还没有提交，请先 clone/初始化代码仓。")
    # History classification is unsafe with missing ancestors.
    if git(repo.path, "rev-parse", "--is-shallow-repository", timeout=timeout).stdout.strip() == "true":
        raise SyncError("这是浅克隆，请先自行 git fetch --unshallow，再启用同步。")
    return head


def merge_args(repo: Repository) -> List[str]:
    # Ignore branch-specific mergeOptions (which might contain -X theirs/ours or autostash).
    return ["-c", f"branch.{repo.target_branch}.mergeOptions=", "-c", "merge.autoStash=false",
            "merge", "--strategy=recursive", "--no-edit", "--no-autostash", "--no-overwrite-ignore"]


@dataclass(frozen=True)
class MergePreview:
    conflicts: List[str]
    paths: List[str]


def preview_merge(repo: Repository, head: str, incoming: str, timeout: int) -> MergePreview:
    """Use a disposable detached worktree, without changing the user's worktree/index."""
    with tempfile.TemporaryDirectory(prefix="git-sync-preview-") as temporary:
        base = Path(temporary)
        worktree = base / "worktree"
        hooks = base / "empty-hooks"
        hooks.mkdir()
        options = ["-c", f"core.hooksPath={hooks}", "-c", "user.name=Git Sync Preview",
                   "-c", "user.email=preview@localhost", "-c", "commit.gpgSign=false",
                   "-c", "rerere.enabled=false"]
        registered = False
        try:
            # Mark before creation: even a timed-out checkout may have registered the worktree.
            registered = True
            git(repo.path, *options, "worktree", "add", "--detach", "--", str(worktree), head,
                timeout=timeout)
            result = git(worktree, *options, *merge_args(repo), "--no-commit", "--no-ff",
                         "--no-gpg-sign", incoming, timeout=timeout, check=False)
            conflicts = git(worktree, "diff", "--name-only", "--diff-filter=U", "-z",
                            timeout=timeout).stdout
            paths = [item for item in git(worktree, "ls-files", "-z", timeout=timeout).stdout.split("\x00")
                     if item]
            if conflicts:
                return MergePreview([item for item in conflicts.split("\x00") if item], paths)
            if result.returncode:
                raise GitError(redact((result.stderr or result.stdout).strip()))
            return MergePreview([], paths)
        finally:
            if registered:
                cleanup = git(repo.path, "-c", f"core.hooksPath={hooks}", "worktree", "remove",
                              "--force", "--", str(worktree), timeout=timeout, check=False)
                if cleanup.returncode:
                    # Do not prune unrelated worktree records. Tell the user about this one.
                    LOG.warning("临时工作区清理失败：%s", redact(cleanup.stderr.strip()))


def protect_ignored_files(repo: Repository, tracked_paths: List[str], timeout: int) -> None:
    """Git merge strategies do not consistently honor --no-overwrite-ignore.

    Check the preview index as well as the source tree: directory renames can create
    result paths that don't occur in the source tree itself.
    """
    ignored = [item for item in git(repo.path, "ls-files", "--others", "--ignored",
                                   "--exclude-standard", "-z", timeout=timeout).stdout.split("\x00")
               if item]
    ignore_case = git(repo.path, "config", "--bool", "--get", "core.ignorecase",
                      timeout=timeout, check=False)
    if ignore_case.returncode not in (0, 1):
        raise GitError(redact(ignore_case.stderr.strip() or "无法读取 core.ignorecase。"))

    def normalize(path: str) -> str:
        if os.name == "nt":
            path = path.replace("\\", "/")
        path = unicodedata.normalize("NFC", path)
        return path.casefold() if os.name == "nt" or ignore_case.stdout.strip() == "true" else path

    tracked = {normalize(item) for item in tracked_paths}
    ignored_set = {normalize(item) for item in ignored}

    def prefixes(path: str):
        parts = path.replace("\\", "/").split("/") if os.name == "nt" else path.split("/")
        for length in range(1, len(parts) + 1):
            yield normalize("/".join(parts[:length]))

    collisions = {path for path in ignored if any(prefix in tracked for prefix in prefixes(path))}
    for path in tracked_paths:
        collisions.update(prefix for prefix in prefixes(path) if prefix in ignored_set)
    if collisions:
        raise SyncError("以下被忽略的本地文件可能被来源文件/目录覆盖，请先自行迁移或备份：\n"
                        + "\n".join(f"  {path}" for path in sorted(collisions)))


def backup_branch(repo: Repository, head: str, timeout: int) -> str:
    # A rolling recovery ref is deliberately outside refs/heads: push --all excludes it.
    ref = repo.ref_prefix + "/backup"
    git(repo.path, "update-ref", ref, head, timeout=timeout)
    return ref


@dataclass(frozen=True)
class Outcome:
    name: str
    status: str
    detail: str


def sync_one(repo: Repository, timeout: int = 120, *, interactive: bool = False,
             dry_run: bool = False, accept_rewritten: bool = False) -> Outcome:
    try:
        repository_root(repo.path, timeout)
        with repository_lock(repo.path, timeout):
            return sync_locked(repo, timeout, interactive, dry_run, accept_rewritten)
    except GitError as exc:
        return Outcome(repo.name, "ERROR", str(exc))
    except (SyncError, OSError) as exc:
        return Outcome(repo.name, "BLOCKED", redact(str(exc)))


def sync_locked(repo: Repository, timeout: int, interactive: bool,
                dry_run: bool, accept_rewritten: bool) -> Outcome:
    if repo.strategy == "snapshot":
        from snapshot_sync import sync_snapshot_locked
        return sync_snapshot_locked(repo, timeout, dry_run=dry_run)
    head = preflight(repo, timeout)
    incoming_ref = repo.ref_prefix + "/incoming"
    accepted_ref = repo.ref_prefix + "/accepted"
    previous = oid(repo.path, accepted_ref, timeout)
    git(repo.path, "fetch", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head",
        "--", repo.source, f"+refs/heads/{repo.source_branch}:{incoming_ref}", timeout=timeout)
    incoming = oid(repo.path, incoming_ref, timeout)
    if incoming is None:
        raise GitError("fetch 后未找到来源提交。")
    if previous and not ancestor(repo.path, previous, incoming, timeout) and not accept_rewritten:
        return Outcome(repo.name, "BLOCKED",
                       "来源分支历史被改写（可能 force push），已暂停。确认后仅对该仓库运行 "
                       f"sync --repo {repo.name} --accept-rewritten-source。")
    # This checkpoint is independent of the incoming ref, so rewrite detection survives retries.
    if not dry_run:
        git(repo.path, "update-ref", accepted_ref, incoming, timeout=timeout)
    if head == incoming:
        return Outcome(repo.name, "UP_TO_DATE", f"{repo.target_branch} @ {head[:12]}")
    if ancestor(repo.path, incoming, head, timeout):
        return Outcome(repo.name, "AHEAD", "本地已包含来源全部提交，并保留本地提交；无需更新。")
    fast_forward = ancestor(repo.path, head, incoming, timeout)
    tracked_paths = [item for item in git(repo.path, "ls-tree", "-r", "--name-only", "-z", incoming,
                                         timeout=timeout).stdout.split("\x00") if item]
    manual_merge = False
    if not fast_forward:
        related = git(repo.path, "merge-base", head, incoming, timeout=timeout, check=False)
        if related.returncode == 1:
            return Outcome(repo.name, "BLOCKED", "两条分支没有共同祖先，请确认是否配置了正确的仓库/分支。")
        if related.returncode:
            raise GitError(redact(related.stderr.strip() or "无法读取共同祖先。"))
        if repo.strategy == "ff-only" and not interactive:
            return Outcome(repo.name, "BLOCKED",
                           "本地与来源各有提交。运行 sync --interactive 逐次确认，"
                           "或将 strategy 配置为 merge，允许无冲突合并。")
        preview = preview_merge(repo, head, incoming, timeout)
        tracked_paths.extend(preview.paths)
        protect_ignored_files(repo, tracked_paths, timeout)
        conflicts = preview.conflicts
        if conflicts:
            detail = "预检查发现冲突，本地工作区未修改：\n" + "\n".join(f"  {path}" for path in conflicts)
            if dry_run or not interactive:
                return Outcome(repo.name, "CONFLICT", detail + "\n运行 sync --interactive 决定是否进入手工合并。")
            print(detail, flush=True)
            answer = input("在本地开始这次合并，由你手工解决并提交？[y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                return Outcome(repo.name, "CONFLICT", "已跳过，本地工作区未修改。")
            manual_merge = True
        elif repo.strategy == "ff-only" and interactive and not dry_run:
            answer = input(f"[{repo.name}] 无冲突，但需要创建合并提交。现在合并？[y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                return Outcome(repo.name, "BLOCKED", "已跳过，本地工作区未修改。")
    protect_ignored_files(repo, tracked_paths, timeout)
    if dry_run:
        action = "快进更新" if fast_forward else "创建无冲突合并提交"
        return Outcome(repo.name, "DRY_RUN", f"将{action}：{head[:12]} ← {incoming[:12]}；本地分支未修改。")
    if not fast_forward:
        # Preview has its own identity; actual commits must use the user's configured identity.
        git(repo.path, "var", "GIT_AUTHOR_IDENT", timeout=timeout)
        git(repo.path, "var", "GIT_COMMITTER_IDENT", timeout=timeout)
    if preflight(repo, timeout) != head:
        raise SyncError("检查期间本地 HEAD 已变化，本轮暂停，请重试。")
    protect_ignored_files(repo, tracked_paths, timeout)
    backup = backup_branch(repo, head, timeout)
    args = merge_args(repo) + (["--ff-only"] if fast_forward else ["--no-ff"])
    if manual_merge:
        args.append("--no-commit")
    try:
        result = git(repo.path, *args, incoming, timeout=timeout, check=False)
    except GitError as exc:
        raise GitError(f"{exc}\n更新前备份：{backup}；请检查 git status，存在合并状态时可 git merge --abort。") from exc
    if result.returncode or manual_merge:
        pending = git_path(repo.path, "MERGE_HEAD", timeout).exists()
        if pending:
            return Outcome(repo.name, "CONFLICT",
                           "已进入手工合并，自动同步暂停。解决后 git add <文件> && git commit；"
                           f"或 git merge --abort 撤销。\n更新前备份：{backup}")
        raise GitError(redact((result.stderr or result.stdout).strip()) + f"\n更新前备份：{backup}")
    after = oid(repo.path, "HEAD", timeout)
    detail = f"{head[:12]} → {(after or '?')[:12]}；更新前备份：{backup}"
    if (repo.path / ".gitmodules").exists():
        detail += "\n此仓库包含子模块，需按项目要求自行更新子模块工作区。"
    return Outcome(repo.name, "UPDATED", detail)


def select_repositories(config: Configuration, name: Optional[str]) -> List[Repository]:
    repos = [repo for repo in config.repositories if repo.enabled and (name is None or repo.name == name)]
    if not repos:
        raise ConfigError("没有匹配的启用仓库，请检查 --repo 和 enabled 配置。")
    return repos


def print_outcome(outcome: Outcome) -> None:
    text = redact(f"[{outcome.name}] {LABELS[outcome.status]}：{outcome.detail}")
    print(text, flush=True)
    LOG.log(logging.INFO if outcome.status in GOOD_STATUSES else logging.WARNING, text)


def run_sync(config: Configuration, name: Optional[str] = None, **kwargs) -> List[Outcome]:
    outcomes = []
    for repo in select_repositories(config, name):
        outcome = sync_one(repo, config.git_timeout_seconds, **kwargs)
        print_outcome(outcome)
        outcomes.append(outcome)
    return outcomes


def prompt(label: str, default: Optional[str] = None) -> str:
    if not sys.stdin.isatty():
        if default is None:
            raise ConfigError(f"非交互模式下必须通过参数指定{label}。")
        return default
    value = input(f"{label}" + (f" [{default}]" if default else "") + ": ").strip()
    if not value and default is None:
        raise ConfigError(f"{label}不能为空。")
    return value or default or ""


def configure(args: argparse.Namespace, path: Path) -> None:
    if args.command == "init" and path.exists():
        raise ConfigError(f"配置已存在：{path}；添加仓库请使用 add，修改请直接编辑配置。")
    config = load_config(path) if args.command == "add" else Configuration([])
    raw_path = args.repo or prompt("本地仓库根目录（--repo）")
    repo_path = Path(raw_path).expanduser().resolve()
    repository_root(repo_path)
    branch = current_branch(repo_path, 120)
    derived_name = re.sub(r"[^A-Za-z0-9._-]", "-", repo_path.name).strip("-._")[:64] or "repository"
    data = {
        "name": args.name or prompt("配置名称", derived_name),
        "path": str(repo_path),
        "source": args.source or prompt("GitHub 来源 remote 名称或 URL", "origin"),
        "source_branch": args.source_branch or prompt("GitHub 来源分支", branch or "main"),
        "target_branch": args.target_branch or prompt("本地目标分支", branch or "main"),
        "strategy": args.strategy or prompt("策略（snapshot / ff-only / merge）", "snapshot"),
        "preserve_paths": args.preserve or [],
    }
    repo = parse_repository(data, path.parent)
    if oid(repo.path, f"refs/heads/{repo.target_branch}", 120) is None:
        raise ConfigError(f"本地分支不存在：{repo.target_branch}。请先创建或检出该分支。")
    if any(existing.name == repo.name or existing.path == repo.path for existing in config.repositories):
        raise ConfigError("配置名称或本地仓库路径已存在。")
    log_directory = config.log_directory
    if args.command == "init" or args.log_dir:
        raw_logs = args.log_dir or prompt("日志存放目录", str(path.parent / ".git-sync"))
        log_directory = Path(raw_logs).expanduser().resolve()
    updated = Configuration(config.repositories + [repo], config.interval_seconds,
                            config.git_timeout_seconds, log_directory)
    write_config(path, updated)
    print(f"已保存配置：{path}\n先运行 sync --dry-run 查看同步计划，再运行 sync 或 watch。")


def show_status(config: Configuration, name: Optional[str]) -> int:
    failed = False
    for repo in select_repositories(config, name):
        try:
            repository_root(repo.path, config.git_timeout_seconds)
            head = preflight(repo, config.git_timeout_seconds)
            print(f"[{repo.name}] 就绪：{repo.path}\n"
                  f"  来源 {redact(repo.source)} / {repo.source_branch} → 本地 {repo.target_branch}\n"
                  f"  HEAD {head[:12]}；策略 {repo.strategy}（尚未联网检查）")
        except (SyncError, OSError) as exc:
            print_outcome(Outcome(repo.name, "BLOCKED", str(exc)))
            failed = True
    return int(failed)


def setup_logging(config_path: Path, log_directory: Optional[Path] = None) -> Path:
    directory = log_directory or config_path.parent / ".git-sync"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{config_path.stem}.log"
    handler = RotatingFileHandler(path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.setLevel(logging.INFO)
    LOG.addHandler(handler)
    return path


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="在电脑 2 安全同步 GitHub 提交；不自动 push/切分支/覆盖本地改动。")
    parser.add_argument("--config", default="sync-config.json", help="配置路径，默认 ./sync-config.json")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("init", "add"):
        sub = subparsers.add_parser(command, help="初始化配置" if command == "init" else "添加同步仓库")
        sub.add_argument("--repo", help="本地仓库根目录")
        sub.add_argument("--name", help="配置名称")
        sub.add_argument("--source", help="来源 remote（如 origin/github）或 Git URL")
        sub.add_argument("--source-branch", help="GitHub 来源分支")
        sub.add_argument("--target-branch", help="本地目标分支（必须已存在）")
        sub.add_argument("--strategy", choices=("ff-only", "merge", "snapshot"))
        sub.add_argument("--log-dir", help="日志存放目录")
        sub.add_argument("--preserve", action="append", help="保留路径/通配符，可重复，如 config/internal/**")
    sub = subparsers.add_parser("status", help="只检查本地仓库，不联网")
    sub.add_argument("--repo", help="只检查指定配置名称")
    sub = subparsers.add_parser("sync", help="同步一次")
    sub.add_argument("--repo", help="只同步指定配置名称")
    sub.add_argument("--interactive", action="store_true", help="分歧/冲突时逐次询问")
    sub.add_argument("--dry-run", action="store_true", help="fetch 并预检查，不修改本地分支/工作区")
    sub.add_argument("--accept-rewritten-source", action="store_true", help="确认来源历史改写，必须同时指定 --repo")
    sub = subparsers.add_parser("watch", help="定时同步，Ctrl+C 退出；冲突时保持暂停并继续监测")
    sub.add_argument("--repo", help="只同步指定配置名称")
    sub.add_argument("--interval", type=int, help="轮询间隔秒数（>= 10），默认使用配置")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    path = Path(args.config).expanduser().resolve()
    try:
        check_git()
        if args.command in ("init", "add"):
            configure(args, path)
            return 0
        config = load_config(path)
        if args.command == "status":
            return show_status(config, args.repo)
        if args.command == "sync":
            if args.accept_rewritten_source and not args.repo:
                raise ConfigError("--accept-rewritten-source 必须同时用 --repo 明确指定一个仓库。")
            if args.interactive and not sys.stdin.isatty():
                raise ConfigError("--interactive 需要在可输入的终端中运行。")
            setup_logging(path, config.log_directory)
            outcomes = run_sync(config, args.repo, interactive=args.interactive,
                                dry_run=args.dry_run, accept_rewritten=args.accept_rewritten_source)
            return int(any(outcome.status not in GOOD_STATUSES for outcome in outcomes))
        if args.interval is not None:
            positive_int(args.interval, "--interval", 10)
        log_path = setup_logging(path, config.log_directory)
        print(f"开始监测，Ctrl+C 退出。日志：{log_path}", flush=True)
        previous: Dict[str, Tuple[str, str]] = {}
        while True:
            config = load_config(path)
            for repo in select_repositories(config, args.repo):
                outcome = sync_one(repo, config.git_timeout_seconds)
                signature = (outcome.status, outcome.detail)
                if previous.get(repo.name) != signature:
                    print_outcome(outcome)
                previous[repo.name] = signature
            time.sleep(args.interval if args.interval is not None else config.interval_seconds)
    except (SyncError, OSError) as exc:
        print(f"错误：{redact(str(exc))}", file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print("\n已停止；如中断了 Git 操作，请用 git status 检查仓库状态。", file=sys.stderr)
        return 130 if args.command != "watch" else 0


if __name__ == "__main__":
    sys.exit(main())
