# multiRepos_git_sync_tool

在电脑 2 上运行的小工具：电脑 1 push 到 GitHub 后，定时把指定分支的提交同步到电脑 2 的本地分支。

提供两种使用入口：

| 使用方式 | 启动入口 | 适用情况 |
| --- | --- | --- |
| **IDEA / PyCharm 插件（推荐）** | 在 IDE 中打开 `Multi Repo Sync` 工具窗口 | 图形化查看差异、处理冲突；支持没有共同 Git 历史的仓库 |
| Python 源码 / 命令行 | 运行 `git_sync.py` | 单次同步、后台轮询；`snapshot` 首次确认和冲突处理仍使用插件 |

插件面向 **PyCharm 2024.3.2**，同步脚本已包含在插件 ZIP 中，安装后无需另行启动 Python 脚本。电脑 2 仍需准备 **Python 3.9+、Git 2.29+**，并能通过系统 Git 访问 GitHub。Python 引擎使用标准库，没有第三方 Python 依赖。

采用轮询，例如每 60 秒检查一次。临时处理不创建分支，完成或丢弃后清理暂存文件；恢复点保存在本地专用引用中。工具单向拉取，完全不执行 push。

## 插件使用：安装、配置与同步

### 1. 安装插件

1. 下载 [multi-repo-sync-0.2.0.zip](dist/multi-repo-sync-0.2.0.zip)，复制到电脑 2。GitHub 文件页可点击下载按钮获取原始 ZIP，保持 ZIP 原样。
2. 在 PyCharm 中打开 `Settings / Preferences → Plugins → 齿轮菜单 → Install Plugin from Disk…`，选择这个插件 ZIP。
3. 按 IDE 提示重启，打开电脑 2 的业务项目。
4. 通过 `View → Tool Windows → Multi Repo Sync` 打开插件。

### 2. 在界面中初始化

插件顶部的 `Python 3.9+` 填系统可执行文件名称或解释器完整路径：Windows 可用 `py` 或 `python.exe` 的路径，macOS/Linux 可用 `python3` 或绝对路径。`配置文件` 可放在仓库外；已有配置时点击「加载配置」。

进入「初始化 / 配置」页，填写：

| 界面字段 | 示例 / 说明 |
| --- | --- |
| 配置名称 | `internal-app` |
| 电脑 2 仓库 | 已有本地 Git 工作区的根目录 |
| 来源 remote / URL | `github`、`origin` 或 `git@github.com:your-name/your-project.git` |
| GitHub 分支 | `main` |
| 本地目标分支 | `internal`；需要先在 IDE 中创建或切换到这个分支 |
| 保留内网路径 | 每行一项，例如 `config/internal/**`、`*.local.yml` |
| 日志存放目录 | 自选目录，如 `D:\logs\git-sync` 或 `/path/to/logs` |
| 监测间隔 | 默认 60 秒，最少 10 秒 |

点击「保存初始化 / 修改」。插件使用 `snapshot` 文件同步模式，不要求两边有共同 Git 历史。GitHub 认证复用本机 Git 的 SSH key / 凭据管理器，请事先完成连接认证。

### 3. 查看差异并完成首次同步

1. 进入「同步与差异」，点击「检查同步」，生成文件列表。此时业务代码尚未修改。
2. 选中文件，点击「查看差异」打开 IDE 双栏 Diff，或点击「三方合并」使用原生合并编辑器。
3. 根据需要选择「保留内网」「使用来源」「删除文件」，或保存手工合并结果。命中保留路径的文件会保留内网版本。
4. 所有待确认项处理完成后，点击「应用已确认计划」，确认更新和删除操作，工具才更新本地工作区并创建本地提交。

没有共同历史时，首次无法自动判断哪些差异属于内网代码，需要你逐项确认；来源独有文件默认新增。首次确认后会记录同步基线，后续比较能够保留内网修改。创建本地提交需要提前配置 Git `user.name` 和 `user.email`。

### 4. 开启自动监测

勾选「自动监测（首次同步/冲突需确认）」，会按配置轮询所有启用的仓库：无冲突的后续更新可自动应用，发生冲突或来源历史改写时通过 IDE 通知你处理。已经在界面检查或编辑的计划会等待你点击「应用已确认计划」。

取消勾选或关闭项目后监测停止；隐藏工具窗口不会停止监测。IDE 有未保存文档、仓库有未提交改动时会暂停同步。暂存计划成功应用或点击「丢弃暂存计划」后会清理，不会创建或推送临时分支。

更多边界、恢复方法、构建方法见 [插件完整使用说明](docs/jetbrains-plugin.md)。插件已完成编译打包和引擎测试，尚未在用户的 PyCharm 2024.3.2 实例中进行 GUI 验证。

```text
电脑 1：开发 → commit → push → GitHub 来源分支 main
                                    ↓ fetch（轮询）
电脑 2：已有的本地仓库 → 目标分支 main / internal
                         ↓
                安全快进，或预检查后合并
                遇到冲突暂停，由你决定
```

## 源码 / 命令行使用

以下 `python3 git_sync.py ...` 命令用于直接从源码启动同步引擎。使用插件时，在上面的工具窗口完成操作即可。源码与插件可通过同一配置文件配合同步；命令行使用 `--config` 指定插件界面所选的配置路径。

工具目录建议与业务仓库分开放置。以下命令均在工具目录执行；Windows 可将 `python3` 换成 `py -3`，路径按实际情况修改。多行命令使用 Bash 语法，在 PowerShell 中请合成一行执行。

### 1. 准备电脑 2 的仓库和 GitHub 访问权限

如果还没有本地仓库，先自行 clone，例如：

```bash
git clone git@github.com:your-name/your-project.git /path/to/project
```

如果已有内网仓库，`origin` 指向内网 Git 服务，可以额外添加 GitHub 来源，保留原有 remote：

```bash
git -C /path/to/internal-project remote add github git@github.com:your-name/your-project.git
```

工具复用系统 Git 的 SSH key / 凭据管理器。请先在终端验证连接并处理 SSH 主机指纹确认：

```bash
git -C /path/to/internal-project ls-remote github refs/heads/main
```

HTTPS 地址也支持。不要把 PAT、密码写进 URL 或配置文件。监测期间不会弹出 Git 终端凭据输入；请提前完成认证。

### 2. 初始化配置

交互式向导会询问本地路径、来源 remote、来源分支、目标分支、同步策略和日志目录。新初始化默认 `snapshot`，首次确认需使用插件；纯命令行兼容模式可显式选 `ff-only` / `merge`：

```bash
python3 git_sync.py init
```

也可以一次指定全部参数。假设 GitHub 分支为 `main`，电脑 2 的目标分支为 `internal`：

```bash
python3 git_sync.py init \
  --name my-project \
  --repo /path/to/internal-project \
  --source github \
  --source-branch main \
  --target-branch internal \
  --strategy ff-only
```

本地目标分支必须已经存在，并由你先切换到该分支：

```bash
git -C /path/to/internal-project switch internal
```

工具会检查当前分支，**不会自动切分支**。如果还需要在电脑 2 的同一仓库里日常开发，建议用 `git worktree add /path/to/sync-workspace internal` 单独建立同步工作区，把配置指向该路径，减少互相打扰。Git 不允许同一个分支同时被两个工作区检出，请按现有仓库状态安排。

### 3. 预览并同步

```bash
# 只检查本地状态，不联网
python3 git_sync.py status

# 联网 fetch 并检查同步计划，不修改本地分支、工作区或暂存区
python3 git_sync.py sync --dry-run

# 执行一次同步
python3 git_sync.py sync

# 持续运行，默认每 60 秒检查一次，Ctrl+C 退出
python3 git_sync.py watch
```

预览仍会下载 Git 对象、更新工具自己的来源引用；合并预检查会短暂创建临时工作区。它不会更新目标分支，也不会创建备份分支。

## 内网版本存在专用修改时

| 策略 | 行为 | 适用情况 |
| --- | --- | --- |
| `snapshot`（新初始化默认） | 按文件快照同步，首次界面确认；后续使用来源基线三方比较 | 无共同历史，或需要图形界面保留内网差异 |
| `ff-only` | 仅快进更新；本地和 GitHub 各有提交时暂停 | 保持严格镜像，或每次分歧都亲自确认 |
| `merge` | 快进优先；分歧时先在临时工作区试合并，无冲突才创建合并提交 | 内网分支有需要长期保留的独立提交 |

内网专用修改应先自行 commit 到目标分支。`merge` 模式保留双方历史和 Git 能无冲突合并的改动，不会自动 push 合并提交。创建合并提交需要事先配置好自己的 `user.name`、`user.email`，以及项目要求的提交签名环境。

初始化时可选择 `--strategy merge`；已有配置直接把对应项的 `strategy` 改为 `merge`。这代表你允许工具自动提交**经 Git 判断没有文本合并冲突**的合并。代码逻辑是否兼容仍应通过项目自身的测试确认。

例如本地已提交 `internal.ini` 的内网配置，GitHub 只更新了其他源文件，可以正常合并。如果两边修改了同一位置，工具会列出冲突文件并暂停，原工作区保持不变。

配置管理建议：

- 优先让项目从环境变量或被 `.gitignore` 忽略的本地覆盖文件读取机器配置，避免每次都修改被跟踪的公共配置。
- 已被 Git 跟踪的内网配置差异，应作为内网分支上的独立提交保留。不要仅用 `.gitignore` 期待保护已跟踪文件。
- 工具不提供“强制以来源为准”或按路径自动选择一方的功能。仓库自带的 `.gitattributes` 合并驱动仍按 Git 原有规则生效。
- `ff-only` / `merge` 需要共同 Git 历史；没有共同历史时，使用插件的 `snapshot` 模式，无须人工拼接提交历史。

## 遇到分歧或冲突

此节为 `ff-only` / `merge` 的终端操作。`snapshot` 模式在 [插件界面](docs/jetbrains-plugin.md) 中处理，不需要在终端查看冲突。

定时监测不会向你索取终端输入。它会输出暂停原因，其他仓库继续同步；后续会继续检查，问题解决后自动恢复。状态没有变化时不重复刷屏。

需要你决策时，在另一个终端运行（把名称替换为配置中的 `name`）：

```bash
python3 git_sync.py sync --repo my-project --interactive
```

- `ff-only` 模式下，两边各有提交但没有冲突：询问是否创建一次合并提交。
- 有冲突：先列出冲突文件，询问是否在本地进入手工合并。默认回答是 **否**，跳过后工作区保持不变。
- 回答 `y` 后，工具创建备份并开始合并，保留冲突现场，**不替你选择 ours/theirs，不自动提交冲突结果**。此仓库的后续自动同步会暂停。

之后在业务仓库中解决并提交：

```bash
cd /path/to/internal-project
git status
# 编辑冲突文件、检查结果
git add path/to/resolved-file
git commit
```

或者撤销本次手工合并：

```bash
git merge --abort
```

不要在未检查工作区时执行 `reset --hard`。工具不会执行该命令。

### 来源分支被 force push

如果已观察到的来源提交不再是新的来源提交的祖先，会持续暂停。确认改写原因后，可明确接受该仓库的新来源基线：

```bash
python3 git_sync.py sync --repo my-project --accept-rewritten-source
```

这个选项只是接受历史改写，之后仍遵守同步策略和冲突检查，**不会删除本地历史来强制对齐来源**。第一次同步前的来源历史改写无法通过历史观测检测，首次配置时应核对仓库和分支。

## 多仓库与配置文件

添加其他仓库：

```bash
python3 git_sync.py add
```

默认配置为当前目录的 `sync-config.json`，可参考 [sync-config.example.json](sync-config.example.json)。配置已加入本工具的 `.gitignore`。

```json
{
  "version": 1,
  "interval_seconds": 60,
  "git_timeout_seconds": 120,
  "repositories": [
    {
      "name": "my-project",
      "path": "/path/to/internal-project",
      "source": "github",
      "source_branch": "main",
      "target_branch": "internal",
      "strategy": "merge",
      "enabled": true
    }
  ]
}
```

| 字段 | 含义 |
| --- | --- |
| `name` | 唯一名称，用于 `--repo` 选择；使用英文字母、数字、点、横线或下划线 |
| `path` | 电脑 2 上已有的工作区根目录；相对路径相对于配置文件所在目录 |
| `source` | GitHub 来源 remote 名称或 Git URL，默认 `origin`；不会修改现有 remote 配置 |
| `source_branch` | GitHub 上要跟踪的分支，区分大小写 |
| `target_branch` | 电脑 2 上要更新的本地分支，必须存在且当前已检出 |
| `strategy` | `snapshot`、`ff-only` 或 `merge` |
| `preserve_paths` | `snapshot` 模式保留的内网路径/通配符数组，例如 `["config/internal/**"]` |
| `log_directory` | 顶层配置项，选择日志目录；`null` 使用配置旁的 `.git-sync/` |
| `enabled` | `false` 暂停此项；至少保留一个启用仓库才能运行 |
| `interval_seconds` | 每轮完成后等待的秒数，最小 10，默认 60 |
| `git_timeout_seconds` | 单条 Git 命令的超时秒数，默认 120 |

指定配置路径时，`--config` 放在子命令前：

```bash
python3 git_sync.py --config /path/to/my-sync.json watch --interval 30
python3 git_sync.py --config /path/to/my-sync.json sync --repo my-project
```

`watch` 每轮重读配置。仓库逐个同步，某个仓库失败不会阻断其他仓库；网络失败下一轮重试。多个同步进程对同一仓库采用系统文件锁，进程退出后自动释放。这个锁用于协调本工具；日常编辑、IDE 和其他 Git 工具仍需避免同时修改同步工作区。

## 备份、日志与长期运行

每次实际更新目标分支前，保存指向原 HEAD 的滚动本地恢复引用（不创建分支）：

```text
refs/git-sync/<标识>/backup
```

查看与检查备份：

```bash
git -C /path/to/internal-project for-each-ref --format='%(refname)' refs/git-sync/
git -C /path/to/internal-project log --oneline <以 /backup 结尾的完整引用>
git -C /path/to/internal-project diff <完整 backup 引用> HEAD
```

需要恢复时，先暂停监测并检查本地改动，再从备份建立恢复分支检查结果：

```bash
git -C /path/to/internal-project switch -c recovery-branch <完整 backup 引用>
```

恢复引用保护的是**已提交的 Git 历史**，不是磁盘备份；每次更新替换上一个恢复点。工具不执行 push，这些引用不属于分支，普通 push 和 `push --all` 不会推送；`push --mirror` 会复制全部引用，请勿用于业务远程。不改变业务分支 upstream，也不写入 `FETCH_HEAD`。旧版留下的 `sync-backup/*` 分支需要你确认后自行删除，新版不再创建。

初始化可通过 `--log-dir /path/to/logs` 或交互向导选择目录。日志为 `<日志目录>/<配置文件名去掉扩展名>.log`；未配置时使用配置旁的 `.git-sync/`。单个日志约 2 MiB，最多保留 3 份轮转备份。

macOS / Linux 上，确认正常后可让进程在后台运行：

```bash
nohup python3 -u /absolute/path/to/git_sync.py \
  --config /absolute/path/to/sync-config.json watch \
  > /absolute/path/to/git-sync-console.log 2>&1 &
```

这不会自动开机启动。需要开机运行时，可将同一条 `watch` 命令接入系统的 launchd、systemd 或 Windows 任务计划程序，使用绝对路径，并确保该账户能访问 GitHub。休眠、关机或 GitHub 不可达期间无法同步，恢复后会拉取积累的提交。

单次 `sync` 退出码：`0` 全部成功/已同步/本地领先，`1` 至少一个仓库冲突、暂停或失败，`2` 配置或启动错误，`130` 用户中断。`status` 不联网，不能证明代码已与 GitHub 一致；用 `sync --dry-run` 检查远端。

## 保护边界与注意事项

- 未提交文件、暂存内容、未跟踪文件、未完成的 merge/rebase/cherry-pick/revert/bisect 会阻止同步。工具不会自动 stash、clean、reset、rebase，也不会强推或切分支。
- 被忽略的本地文件若与来源新增的受跟踪文件重名，合并会拒绝覆盖，需你先处理。
- 浅克隆会暂停，请先自行 `git fetch --unshallow`。来源分支删除或改名会报错，需更新配置。
- 带有 `assume-unchanged` / `skip-worktree` 标记或稀疏检出的工作区会暂停，防止被隐藏的本地配置改动遭覆盖；建议使用完整、专用的同步工作区。
- 子模块仅同步主仓库记录的 gitlink，不自动递归拉取、更新子模块工作区；Git LFS 沿用本机 Git/LFS 的过滤器和认证配置。
- 传统 `merge` 模式沿用 Git hooks、提交签名、`.gitattributes` 等项目规则。`snapshot` 的临时索引/commit-tree 方式不执行常规 commit 钩子或自定义合并驱动；具体边界见插件说明。失败时检查本地状态，暂存计划会保留。
- 内容不同不一定是 Git 冲突；`merge` 模式自动整合 Git 判断无冲突的改动。工具不运行项目构建或测试，也不承诺自动解决业务逻辑冲突。

## 开发验证

```bash
python3 -m unittest discover -s tests -v
```

测试使用临时目录中的本地裸仓库模拟 GitHub 和两台电脑，无须网络，不操作真实业务仓库。
