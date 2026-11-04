# PyCharm / IDEA 插件使用说明

安装包：[multi-repo-sync-0.2.0.zip](../dist/multi-repo-sync-0.2.0.zip)。目标最低版本为 **PyCharm / IDEA 2024.3（build 243，含 PyCharm 2024.3.2）**，编译字节码为 Java 21。同步引擎已随插件打包，电脑 2 仍需安装 **Python 3.9+ 和 Git 2.29+**。

## 安装与打开

1. 将 ZIP 复制到电脑 2，保持 ZIP 原样，无需解压。
2. 在 PyCharm 中打开 `Settings / Preferences → Plugins → 齿轮菜单 → Install Plugin from Disk…`，选择 ZIP。
3. 按 IDE 提示重启。打开业务项目，从 `View → Tool Windows → Multi Repo Sync` 进入工具窗口。
4. `Python` 填电脑 2 的解释器可执行文件路径。Windows 可用 `py` 或 `python.exe` 的完整路径；macOS/Linux 可用 `python3` 或绝对路径。不要在解释器路径后附加参数。

源码使用了 IntelliJ 平台公开的 [Tool Window](https://plugins.jetbrains.com/docs/intellij/tool-windows.html) 和 [Diff/Merge API](https://github.com/JetBrains/intellij-community/blob/master/platform/diff-api/src/com/intellij/diff/DiffRequestFactory.java)，无需把项目变成插件开发工程。

## 首次初始化

进入「初始化 / 配置」页填写：

| 配置 | 示例 / 含义 |
| --- | --- |
| 配置文件 | 可放在仓库之外；多个项目可加载同一文件管理多个仓库 |
| 配置名称 | `internal-app`，使用英文字母、数字、点、横线、下划线 |
| 电脑 2 仓库 | 本地已有 Git 工作区的根目录；可以与 GitHub 仓库没有共同历史 |
| 来源 remote / URL | `github` 或 `git@github.com:someone/project.git` |
| GitHub 分支 | `main` |
| 本地目标分支 | 如 `internal`，必须存在且当前已检出 |
| 保留内网路径 | 每行一项，例如 `config/internal/**`、`*.local.yml`，使用 `/` 分隔 |
| 日志存放目录 | 自选目录，例如 `D:\logs\git-sync` 或 `/path/to/logs` |
| 监测间隔 | 默认 60 秒，最少 10 秒 |

点击「保存初始化 / 修改」。如果已有配置，先点击「加载配置」，避免覆盖其他仓库的配置。配置被其他进程修改时，保存会拒绝覆盖，需重新加载。

`origin` 如果指向内网 Git 服务，可以额外添加名为 `github` 的 remote，也可以直接填写 GitHub URL，不需要改动现有 `origin`。工具复用系统 Git 的 SSH key / 凭据管理器；请事先用终端 `git ls-remote <来源> refs/heads/<分支>` 完成访问认证与 SSH 主机指纹确认。

## 首次同步：先确认哪些差异属于内网

点击「检查同步」。这里只下载来源快照并生成计划，不修改业务代码。

- 两边相同的文件不会列出。
- 来源独有的文件，默认选择新增。
- 两边内容不同、以及本地独有的文件，首次都需要你确认。
- 命中保留路径的文件始终保留电脑 2 的版本，包括该路径下来源独有的文件不会新增。

**没有共同历史，就没有可靠依据自动识别“内网专用代码”。** 首次必须由你选择；确认后记录的是“上次来源内容”，不会强行合并两条 Git 历史。

选中文件后：

- 「查看差异」：打开 IDE 原生双栏 Diff，左侧为内网，右侧为 GitHub。
- 「三方合并」：打开原生 Merge 编辑器，左侧内网、中间结果、右侧 GitHub；比较基线是上次确认的来源内容。首次没有共同基线时，基线为空，标题会提示。
- 「保留内网」：保留本地文件及内容。
- 「使用来源」：采用来源版本；如果来源已删除该文件，则表示删除本地文件。
- 「删除文件」：从同步结果中删除该文件。

选择和合并结果只保存在本地暂存计划中。所有待确认项完成后，点击「应用已确认计划」，确认更新/删除操作后才更新工作区并创建本地提交。需要提前配置 Git `user.name`、`user.email`。

文本合并支持普通 UTF-8 文本。二进制、非 UTF-8 文件、符号链接、子模块不能用该文本编辑器合并，应选择一方或删除。文本查看单文件最大 5 MiB。子模块工作区仍需按项目要求自行更新。

## 后续自动同步

勾选「自动监测」，轮询配置中的所有启用仓库：

- 只有来源修改：自动更新，包括来源删除的、内网未修改的文件。
- 只有内网修改：保留内网版本。
- 双方修改：尝试以“上次确认的来源内容”为基线进行文本三方合并；无冲突则更新，有冲突则等待界面确认。
- 首次同步、来源 force push、冲突：IDE 通知并暂停该仓库，其他仓库继续处理。
- 在界面中检查过或编辑过的计划会保持等待，监测不会抢先应用；仍需点击「应用已确认计划」。

保留内网版本不等于永远忽略该文件：来源以后又改动同一文件时，仍会进行比较。需要永久保护的文件应放入「保留内网路径」。规则是相对仓库根目录的简单通配符，不是完整 `.gitignore` 语法，不支持 `!` 反选。

IDE 有未保存文档时自动同步暂停；磁盘上有未提交、暂存或未跟踪文件时，同步也会暂停。建议使用专门的同步工作区，避免和日常编辑互相干扰。

取消勾选「自动监测」或关闭项目后监测停止，再次打开项目需重新勾选。隐藏工具窗口不会停止监测。无需 IDE 常驻时，可用同一配置运行命令行 `watch`。

## 暂存、恢复和远程边界

- **不创建临时分支。** 暂存计划位于 Git 公共目录的 `git-sync-tool/plans/`，可以跨 IDE 重启继续处理。
- 成功应用或点击「丢弃暂存计划」后，清理本次暂存文件。未完成的人工合并会保留，避免丢失编辑结果。
- 更新前保留一个滚动恢复引用：`refs/git-sync/<标识>/backup`，指向最近一次更新前的本地提交；后一次更新会替换它。
- 来源缓存、同步基线、恢复引用都在 `refs/git-sync/`，不属于分支。工具完全不执行 push，普通 push 与 `git push --all` 都不包含这些引用。不要对业务远程使用 `git push --mirror`，因为 mirror 会复制所有引用。
- 文件同步创建的正式提交在目标分支上，这是你可以正常审核和自行推送到内网服务的代码历史。
- 如果第一次采用新版本时已有旧工具留下的 `sync-backup/*` 备份分支，它们不会自动删除。确认无需旧备份后，可自行删除；新版本不再创建此类分支。

查看恢复引用：

```bash
git for-each-ref --format='%(refname) %(objectname:short)' refs/git-sync/
git show <以 /backup 结尾的完整引用>
```

需要恢复时先停止监测并检查本地改动，然后从该引用建立自己的恢复分支检查：

```bash
git switch -c recovery <完整 backup 引用>
```

如果本地提交在计划生成后发生变化，应用会拒绝执行；已编辑结果仍保存在暂存目录中。先备份需要的编辑结果，再丢弃旧计划并重新检查。

日志为所选目录中的 `<配置文件名去扩展名>.log`，每份约 2 MiB、保留 3 份轮转备份。配置文件不保存密码，URL 不要内嵌 token。

## 构建与验证范围

使用本机 IntelliJ 平台 IDE 安装目录作为 SDK，无需下载 Gradle：

```bash
python3 jetbrains-plugin/build_plugin.py --ide-home /path/to/PyCharm.app/Contents
```

SDK 的 JBR 若不含 `javac`，添加 `--jdk-home /path/to/jdk-21-or-newer`。产物在 `dist/`。当前产物使用本机 IntelliJ 平台 SDK 编译，Java 21 字节码、最低 build 243；Python 同步行为通过本地临时仓库测试。**未在用户的 PyCharm 2024.3.2 实例中实际安装并进行 GUI 验证**，安装后应先在测试仓库完成一次检查与合并，再接入业务仓库。

文件同步使用 Git 对象、临时索引和 `commit-tree` 生成本地单父提交，再以快进更新工作区，不引入来源的提交历史；它不执行常规 `git commit` 钩子，不自动运行项目测试，也不调用 `.gitattributes` 自定义合并驱动。项目过滤器和更新后的 Git `post-merge` 钩子仍按 Git 行为执行；有特殊提交规范时应先验证。
