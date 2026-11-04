package local.multirepo.sync;

import com.google.gson.*;
import com.intellij.diff.DiffContentFactory;
import com.intellij.diff.DiffManager;
import com.intellij.diff.DiffRequestFactory;
import com.intellij.diff.merge.MergeResult;
import com.intellij.diff.requests.SimpleDiffRequest;
import com.intellij.ide.util.PropertiesComponent;
import com.intellij.notification.*;
import com.intellij.openapi.Disposable;
import com.intellij.openapi.application.PathManager;
import com.intellij.openapi.editor.Document;
import com.intellij.openapi.editor.EditorFactory;
import com.intellij.openapi.fileEditor.FileDocumentManager;
import com.intellij.openapi.fileTypes.FileType;
import com.intellij.openapi.fileTypes.FileTypeManager;
import com.intellij.openapi.project.Project;
import com.intellij.openapi.vfs.VirtualFile;
import com.intellij.openapi.wm.*;
import java.awt.*;
import java.awt.event.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.time.LocalTime;
import java.util.List;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.Consumer;
import javax.swing.*;
import javax.swing.table.DefaultTableModel;

final class SyncPanel extends JPanel implements Disposable {
    private final Project project;
    private final ToolWindow toolWindow;
    private final JTextField python = new JTextField();
    private final JTextField configPath = new JTextField();
    private final JComboBox<String> repositories = new JComboBox<>();
    private final JTextField name = new JTextField(), path = new JTextField(), source = new JTextField();
    private final JTextField sourceBranch = new JTextField(), targetBranch = new JTextField(), logs = new JTextField();
    private final JTextArea preserved = new JTextArea(4, 24);
    private final JSpinner interval = new JSpinner(new SpinnerNumberModel(60, 10, 86400, 10));
    private final JLabel status = new JLabel("请加载配置，或填写后保存初始化配置。");
    private final JTextArea output = new JTextArea(6, 30);
    private final DefaultTableModel rows = new DefaultTableModel(new String[]{"文件", "处理方式", "原因"}, 0) {
        @Override public boolean isCellEditable(int row, int column) { return false; }
    };
    private final JTable table = new JTable(rows);
    private final JTabbedPane tabs = new JTabbedPane();
    private final JCheckBox watch = new JCheckBox("自动监测（首次同步/冲突需确认）");
    private final javax.swing.Timer timer;
    private final AtomicBoolean busy = new AtomicBoolean();
    private final ExecutorService worker = Executors.newSingleThreadExecutor(r -> {
        Thread thread = new Thread(r, "multi-repo-sync"); thread.setDaemon(true); return thread;
    });
    private volatile boolean disposed;
    private Bridge bridge;
    private JsonObject configuration, plan;
    private boolean loading;
    private boolean mergeOpen;
    private String lastNotice = "";
    private String revision;
    private final Map<String, String> notices = new HashMap<>();

    SyncPanel(Project project, ToolWindow toolWindow) {
        super(new BorderLayout(6, 6));
        this.project = project;
        this.toolWindow = toolWindow;
        PropertiesComponent properties = PropertiesComponent.getInstance(project);
        python.setText(properties.getValue("multi.sync.python", System.getProperty("os.name").startsWith("Windows") ? "py" : "python3"));
        configPath.setText(properties.getValue("multi.sync.config", Paths.get(PathManager.getConfigPath(), "multi-repo-sync", "config.json").toString()));
        JPanel connection = new JPanel(new GridLayout(0, 1, 3, 3));
        connection.add(fieldLine("Python 3.9+", python, false, true));
        connection.add(fieldLine("配置文件", configPath, false, true));
        JPanel select = new JPanel(new BorderLayout(4, 0));
        select.add(repositories, BorderLayout.CENTER);
        select.add(button("加载配置", () -> command("get", null, null, this::loaded)), BorderLayout.EAST);
        connection.add(select);
        add(connection, BorderLayout.NORTH);
        tabs.addTab("同步与差异", syncView());
        tabs.addTab("初始化 / 配置", settingsView());
        add(tabs, BorderLayout.CENTER);
        JPanel bottom = new JPanel(new BorderLayout());
        bottom.add(status, BorderLayout.NORTH);
        output.setEditable(false);
        output.setLineWrap(true);
        bottom.add(new JScrollPane(output), BorderLayout.CENTER);
        add(bottom, BorderLayout.SOUTH);
        timer = new javax.swing.Timer(60000, event -> automatic());
        timer.setRepeats(true);
        watch.addActionListener(event -> {
            if (watch.isSelected()) { timer.setDelay(((Number) interval.getValue()).intValue() * 1000); timer.start(); automatic(); }
            else timer.stop();
        });
        interval.addChangeListener(event -> timer.setDelay(((Number) interval.getValue()).intValue() * 1000));
        repositories.addActionListener(event -> { if (!loading) showRepository(); });
        emptyConfiguration();
        tabs.setSelectedIndex(1);
        if (Files.isRegularFile(Paths.get(configPath.getText())))
            command("get", null, null, this::loaded);
    }

    private JPanel syncView() {
        JPanel panel = new JPanel(new BorderLayout(4, 4));
        JPanel actions = new JPanel(new FlowLayout(FlowLayout.LEFT));
        actions.add(button("检查同步", () -> prepare(false)));
        actions.add(button("应用已确认计划", this::apply));
        actions.add(button("丢弃暂存计划", this::discard));
        panel.add(actions, BorderLayout.NORTH);
        table.setSelectionMode(ListSelectionModel.SINGLE_SELECTION);
        table.setAutoCreateRowSorter(true);
        table.setRowHeight(25);
        table.addMouseListener(new MouseAdapter() {
            @Override public void mouseClicked(MouseEvent event) { if (event.getClickCount() == 2) diff(); }
        });
        panel.add(new JScrollPane(table), BorderLayout.CENTER);
        JPanel choices = new JPanel(new FlowLayout(FlowLayout.LEFT));
        choices.add(button("查看差异", this::diff));
        choices.add(button("三方合并", this::merge));
        choices.add(button("保留内网", () -> choose("local", null)));
        choices.add(button("使用来源", () -> choose("source", null)));
        choices.add(button("删除文件", () -> choose("delete", null)));
        JPanel below = new JPanel(new GridLayout(0, 1));
        below.add(choices); below.add(watch);
        panel.add(below, BorderLayout.SOUTH);
        return panel;
    }

    private JComponent settingsView() {
        JPanel form = new JPanel(new GridBagLayout());
        int row = 0;
        row = setting(form, row, "配置名称", name);
        row = setting(form, row, "电脑 2 仓库", browse(path, true, false));
        row = setting(form, row, "来源 remote / URL", source);
        row = setting(form, row, "GitHub 分支", sourceBranch);
        row = setting(form, row, "本地目标分支", targetBranch);
        row = setting(form, row, "保留内网路径（每行一项）", new JScrollPane(preserved));
        row = setting(form, row, "日志存放目录", browse(logs, true, false));
        row = setting(form, row, "监测间隔（秒）", interval);
        JLabel explanation = new JLabel("<html>文件模式无需共同 Git 历史。首次差异逐项确认；后续三方比较。<br>保留示例：config/internal/** 或 *.local.yml。目标分支必须已检出。<br>不创建临时分支；完成/丢弃时清理暂存文件；从不执行 push。</html>");
        GridBagConstraints c = constraints(0, row++); c.gridwidth = 2;
        form.add(explanation, c);
        JPanel buttons = new JPanel(new FlowLayout(FlowLayout.LEFT));
        buttons.add(button("保存初始化 / 修改", this::save));
        buttons.add(button("添加仓库", this::addRepository));
        buttons.add(button("删除此配置项", this::removeRepository));
        c = constraints(0, row); c.gridwidth = 2;
        form.add(buttons, c);
        return new JScrollPane(form);
    }

    private int setting(JPanel form, int row, String label, JComponent value) {
        GridBagConstraints left = constraints(0, row); left.weightx = 0;
        form.add(new JLabel(label), left);
        GridBagConstraints right = constraints(1, row); right.weightx = 1;
        form.add(value, right);
        return row + 1;
    }
    private GridBagConstraints constraints(int column, int row) {
        GridBagConstraints c = new GridBagConstraints(); c.gridx = column; c.gridy = row;
        c.fill = GridBagConstraints.HORIZONTAL; c.anchor = GridBagConstraints.NORTHWEST;
        c.insets = new Insets(5, 5, 5, 5); return c;
    }
    private JButton button(String label, Runnable action) {
        JButton button = new JButton(label); button.addActionListener(event -> action.run()); return button;
    }
    private JPanel fieldLine(String label, JTextField value, boolean directory, boolean file) {
        JPanel line = new JPanel(new BorderLayout(4, 0)); line.add(new JLabel(label), BorderLayout.WEST);
        line.add(browse(value, directory, file), BorderLayout.CENTER); return line;
    }
    private JPanel browse(JTextField value, boolean directory, boolean file) {
        JPanel line = new JPanel(new BorderLayout(3, 0)); line.add(value, BorderLayout.CENTER);
        line.add(button("…", () -> {
            JFileChooser chooser = new JFileChooser();
            chooser.setFileSelectionMode(directory ? JFileChooser.DIRECTORIES_ONLY : JFileChooser.FILES_ONLY);
            if (chooser.showDialog(this, "选择") == JFileChooser.APPROVE_OPTION)
                value.setText(chooser.getSelectedFile().getAbsolutePath());
        }), BorderLayout.EAST); return line;
    }

    private void emptyConfiguration() {
        configuration = new JsonObject();
        configuration.addProperty("version", 1);
        configuration.addProperty("interval_seconds", 60);
        configuration.addProperty("git_timeout_seconds", 120);
        configuration.addProperty("log_directory", Paths.get(configPath.getText()).toAbsolutePath().getParent().resolve("logs").toString());
        JsonArray repos = new JsonArray(); repos.add(newRepository("my-project")); configuration.add("repositories", repos);
        refreshRepositories("my-project");
    }
    private JsonObject newRepository(String label) {
        JsonObject repo = new JsonObject();
        repo.addProperty("name", label); repo.addProperty("path", project.getBasePath() == null ? "" : project.getBasePath());
        repo.addProperty("source", "origin"); repo.addProperty("source_branch", "main"); repo.addProperty("target_branch", "main");
        repo.addProperty("strategy", "snapshot"); repo.addProperty("enabled", true); repo.add("preserve_paths", new JsonArray());
        return repo;
    }
    private void loaded(JsonObject response) {
        revision = str(response, "revision", null);
        if (response.has("config") && response.get("config").isJsonObject()) {
            configuration = response.getAsJsonObject("config"); refreshRepositories(null); log("配置已加载。");
        } else { emptyConfiguration(); tabs.setSelectedIndex(1); log("尚无配置，请填写后保存。"); }
    }
    private void refreshRepositories(String selected) {
        loading = true; repositories.removeAllItems();
        for (JsonElement value : configuration.getAsJsonArray("repositories")) repositories.addItem(str(value.getAsJsonObject(), "name", ""));
        if (selected != null) repositories.setSelectedItem(selected);
        loading = false;
        interval.setValue(configuration.has("interval_seconds") ? configuration.get("interval_seconds").getAsInt() : 60);
        logs.setText(str(configuration, "log_directory", Paths.get(configPath.getText()).toAbsolutePath().getParent().resolve("logs").toString()));
        showRepository();
    }
    private JsonObject currentRepository() {
        String selected = (String) repositories.getSelectedItem();
        for (JsonElement value : configuration.getAsJsonArray("repositories"))
            if (Objects.equals(selected, str(value.getAsJsonObject(), "name", ""))) return value.getAsJsonObject();
        return null;
    }
    private void showRepository() {
        JsonObject repo = currentRepository(); if (repo == null) return;
        name.setText(str(repo, "name", "")); path.setText(str(repo, "path", "")); source.setText(str(repo, "source", "origin"));
        sourceBranch.setText(str(repo, "source_branch", "main")); targetBranch.setText(str(repo, "target_branch", "main"));
        List<String> patterns = new ArrayList<>();
        if (repo.has("preserve_paths")) for (JsonElement value : repo.getAsJsonArray("preserve_paths")) patterns.add(value.getAsString());
        preserved.setText(String.join("\n", patterns)); plan = null; rows.setRowCount(0);
    }
    private void save() {
        if (plan != null) { log("请先丢弃或应用当前计划，再修改配置。"); return; }
        JsonObject repo = currentRepository(); if (repo == null) return;
        JsonObject updated = configuration.deepCopy();
        for (JsonElement value : updated.getAsJsonArray("repositories")) {
            JsonObject item = value.getAsJsonObject();
            if (str(item, "name", "").equals(str(repo, "name", ""))) {
                item.addProperty("name", name.getText().strip()); item.addProperty("path", path.getText().strip());
                item.addProperty("source", source.getText().strip()); item.addProperty("source_branch", sourceBranch.getText().strip());
                item.addProperty("target_branch", targetBranch.getText().strip()); item.addProperty("strategy", "snapshot");
                JsonArray patterns = new JsonArray();
                for (String pattern : preserved.getText().split("\\R")) if (!pattern.isBlank()) patterns.add(pattern.strip());
                item.add("preserve_paths", patterns); break;
            }
        }
        updated.addProperty("interval_seconds", ((Number) interval.getValue()).intValue());
        updated.addProperty("log_directory", logs.getText().strip());
        updated.addProperty("_expected_revision", revision);
        String newName = name.getText().strip();
        command("save", null, updated, result -> {
            configuration = result.getAsJsonObject("config"); refreshRepositories(newName); tabs.setSelectedIndex(0);
            revision = str(result, "revision", null);
            log("配置已保存。点击「检查同步」查看首次差异。");
        });
    }
    private void addRepository() {
        int index = configuration.getAsJsonArray("repositories").size() + 1;
        String label;
        Set<String> existing = new HashSet<>();
        for (JsonElement value : configuration.getAsJsonArray("repositories")) existing.add(str(value.getAsJsonObject(), "name", ""));
        do { label = "project-" + index++; } while (existing.contains(label));
        configuration.getAsJsonArray("repositories").add(newRepository(label)); refreshRepositories(label); tabs.setSelectedIndex(1);
    }
    private void removeRepository() {
        JsonArray repos = configuration.getAsJsonArray("repositories");
        if (repos.size() == 1) { log("至少保留一个配置项。"); return; }
        JsonObject selected = currentRepository(); repos.remove(selected); refreshRepositories(null);
        log("已从界面移除配置项；点击保存后生效，不删除仓库文件。");
    }

    private boolean unsaved() {
        JsonObject repo = currentRepository();
        if (repo == null) return true;
        try {
            Path root = Paths.get(str(repo, "path", "")).toAbsolutePath().normalize();
            for (Document document : FileDocumentManager.getInstance().getUnsavedDocuments()) {
                VirtualFile file = FileDocumentManager.getInstance().getFile(document);
                if (file != null && Paths.get(file.getPath()).toAbsolutePath().normalize().startsWith(root)) {
                    status.setText("同步工作区有尚未保存的 IDE 文档，请先保存并处理本地提交。"); return true;
                }
            }
        } catch (Exception invalid) { log("仓库路径无效，请检查配置。"); return true; }
        return false;
    }
    private void prepare(boolean automatic) {
        if (unsaved()) return;
        String selected = (String) repositories.getSelectedItem();
        command("prepare", selected, null, result -> { render(result.getAsJsonObject("plan")); tabs.setSelectedIndex(0); });
    }
    private void automatic() {
        // Don't write any monitored worktree while the IDE has an unsaved document.
        if (busy.get() || mergeOpen || FileDocumentManager.getInstance().getUnsavedDocuments().length > 0) return;
        command("auto-all", null, null, response -> {
          for (JsonElement element : response.getAsJsonArray("results")) {
            JsonObject result = element.getAsJsonObject();
            String repoName = str(result, "name", "");
            if (result.has("plan")) {
                JsonObject pendingPlan = result.getAsJsonObject("plan");
                if (repoName.equals(repositories.getSelectedItem())) render(pendingPlan);
                String notice = "review:" + str(pendingPlan, "id", "");
                if (!notice.equals(notices.get(repoName))) {
                    notifyUser("[" + repoName + "] 有同步差异等待确认，请打开 Multi Repo Sync。", NotificationType.WARNING);
                    notices.put(repoName, notice);
                }
            } else {
                JsonObject outcome = result.getAsJsonObject("outcome");
                String signature = str(outcome, "status", "") + str(outcome, "detail", "");
                status.setText(str(outcome, "detail", ""));
                if (!signature.equals(notices.get(repoName))) {
                    log("[" + repoName + "] " + str(outcome, "detail", ""));
                    if (Set.of("BLOCKED", "ERROR", "CONFLICT").contains(str(outcome, "status", "")))
                        notifyUser(str(outcome, "detail", ""), NotificationType.WARNING);
                    notices.put(repoName, signature);
                }
            }
          }
        });
    }
    private void apply() {
        if (plan == null || unsaved()) { if (plan == null) log("请先检查同步。"); return; }
        if (plan.get("pending_count").getAsInt() != 0) { log("仍有待确认文件，请先逐项选择。"); return; }
        String warning = flag(plan, "first_sync") ? "首次同步：没有共同基线，已按你的选择处理文件。\n" : "";
        if (flag(plan, "source_rewritten")) warning += "注意：来源历史发生了改写。\n";
        if (JOptionPane.showConfirmDialog(this, warning + "将应用列表中的更新和删除，并在本地提交。\n"
                + "保留规则命中的文件不变。不会推送，不创建临时分支。\n确认应用？", "应用同步计划", JOptionPane.YES_NO_OPTION) != JOptionPane.YES_OPTION) return;
        command("apply", (String) repositories.getSelectedItem(), null, result -> {
            plan = null; rows.setRowCount(0); log(str(result.getAsJsonObject("outcome"), "detail", "完成"));
            // Refresh asynchronously so an IDE view immediately reflects files changed by Git.
            com.intellij.openapi.vfs.LocalFileSystem.getInstance().refresh(true);
        });
    }
    private void discard() {
        if (JOptionPane.showConfirmDialog(this, "丢弃此仓库的暂存计划和合并编辑结果？\n本地代码不会改变。", "丢弃计划", JOptionPane.YES_NO_OPTION) != JOptionPane.YES_OPTION) return;
        command("discard", (String) repositories.getSelectedItem(), null, result -> { plan = null; rows.setRowCount(0); log("暂存计划已清理。"); });
    }
    private void render(JsonObject value) {
        plan = value; rows.setRowCount(0);
        for (JsonElement element : plan.getAsJsonArray("entries")) {
            JsonObject entry = element.getAsJsonObject();
            rows.addRow(new Object[]{str(entry, "path", ""), choiceLabel(str(entry, "choice", "")), str(entry, "reason", "")});
        }
        status.setText((flag(plan, "first_sync") ? "首次同步；" : "") + "来源 " + str(plan, "source_oid", "").substring(0, 12)
                + "；待确认 " + plan.get("pending_count").getAsInt() + " 个文件（不会修改工作区，直到应用）。");
    }
    private String choiceLabel(String choice) {
        return switch (choice) {
            case "pending" -> "待确认"; case "source" -> "来源版本（缺失则删除）";
            case "local" -> "保留内网"; case "merged" -> "合并结果";
            case "delete" -> "删除"; case "preserve" -> "保留路径"; default -> choice;
        };
    }
    private JsonObject selectedEntry() {
        if (plan == null || table.getSelectedRow() < 0) { log("请先选择一个文件。"); return null; }
        return plan.getAsJsonArray("entries").get(table.convertRowIndexToModel(table.getSelectedRow())).getAsJsonObject();
    }
    private byte[] bytes(JsonObject entry, String side) throws Exception {
        Path file = Paths.get(str(plan, "directory", ""), str(entry, "id", ""), side);
        if (Files.size(file) > 5 * 1024 * 1024) throw new IllegalArgumentException("文件超过 5 MiB，请选择保留内网或使用来源，或用项目自己的二进制工具检查。");
        return Files.readAllBytes(file);
    }
    private void diff() {
        JsonObject entry = selectedEntry(); if (entry == null) return;
        if (!entry.has("id")) { log("此文件命中保留路径，不会被同步。调整规则后请丢弃旧计划再检查。"); return; }
        if (flag(entry, "binary")) { log("二进制文件不能显示文本差异，请选择保留内网或使用来源。"); return; }
        try {
            FileType type = FileTypeManager.getInstance().getFileTypeByFileName(str(entry, "path", ""));
            var factory = DiffContentFactory.getInstance();
            var left = factory.create(project, new String(bytes(entry, "local"), StandardCharsets.UTF_8), type);
            var right = factory.create(project, new String(bytes(entry, "source"), StandardCharsets.UTF_8), type);
            DiffManager.getInstance().showDiff(project, new SimpleDiffRequest(str(entry, "path", ""), left, right, "电脑 2 / 内网", "GitHub / 来源"));
        } catch (Exception error) { log(error.getMessage()); }
    }
    private void merge() {
        if (busy.get() || mergeOpen) { log("请等待当前操作完成。"); return; }
        JsonObject entry = selectedEntry(); if (entry == null) return;
        if (!flag(entry, "mergeable")) { log("此项不能进行文本合并；请选择保留内网、使用来源或删除。"); return; }
        try {
            String file = str(entry, "path", "");
            String repo = (String) repositories.getSelectedItem();
            String planId = str(plan, "id", "");
            String initial = new String(bytes(entry, "result"), StandardCharsets.UTF_8);
            Document document = EditorFactory.getInstance().createDocument(initial);
            List<String> texts = List.of(new String(bytes(entry, "local"), StandardCharsets.UTF_8),
                    new String(bytes(entry, "base"), StandardCharsets.UTF_8), new String(bytes(entry, "source"), StandardCharsets.UTF_8));
            FileType type = FileTypeManager.getInstance().getFileTypeByFileName(file);
            var request = DiffRequestFactory.getInstance().createMergeRequest(project, type, document, texts,
                    file + (flag(plan, "first_sync") ? "（首次同步，无共同基线）" : ""),
                    List.of("内网版本", "上次确认的来源基线", "GitHub 来源"), result -> {
                        mergeOpen = false;
                        if (result == MergeResult.CANCEL || disposed) return;
                        if (plan == null || !repo.equals(repositories.getSelectedItem()) || !planId.equals(str(plan, "id", ""))) {
                            log("当前仓库/计划已变化，未保存此合并结果，请重新打开。"); return;
                        }
                        String choice = result == MergeResult.LEFT ? "local" : result == MergeResult.RIGHT ? "source" : "merged";
                        JsonObject data = new JsonObject(); data.addProperty("path", file); data.addProperty("choice", choice);
                        if (choice.equals("merged")) data.addProperty("content_base64", Base64.getEncoder().encodeToString(document.getText().getBytes(StandardCharsets.UTF_8)));
                        command("resolve", repo, data, response -> render(response.getAsJsonObject("plan")));
                    });
            mergeOpen = true;
            DiffManager.getInstance().showMerge(project, request);
        } catch (Exception error) { mergeOpen = false; log(error.getMessage()); }
    }
    private void choose(String choice, String ignored) {
        JsonObject entry = selectedEntry(); if (entry == null) return;
        JsonObject request = new JsonObject(); request.addProperty("path", str(entry, "path", "")); request.addProperty("choice", choice);
        command("resolve", (String) repositories.getSelectedItem(), request, response -> render(response.getAsJsonObject("plan")));
    }

    private void command(String operation, String repo, JsonObject request, Consumer<JsonObject> done) {
        if (disposed || !busy.compareAndSet(false, true)) return;
        String interpreter = python.getText().strip(), config = configPath.getText().strip();
        if (interpreter.isEmpty() || config.isEmpty()) { busy.set(false); log("请填写 Python 解释器和配置路径。"); return; }
        String selected = (String) repositories.getSelectedItem();
        PropertiesComponent.getInstance(project).setValue("multi.sync.python", interpreter);
        PropertiesComponent.getInstance(project).setValue("multi.sync.config", config);
        status.setText("正在执行 " + operation + " …");
        worker.submit(() -> {
            try {
                if (bridge == null) bridge = new Bridge();
                JsonObject response = bridge.call(interpreter, config, operation, repo, request);
                ui(() -> { busy.set(false);
                    if (repo != null && !Objects.equals(selected, repositories.getSelectedItem())) { log("操作已完成，切回对应仓库后检查结果。"); return; }
                    try { done.accept(response); } catch (Exception error) { log("界面处理失败：" + error.getMessage()); }
                });
            } catch (Exception error) {
                ui(() -> { busy.set(false); String message = error.getMessage() == null ? error.toString() : error.getMessage(); log(message);
                    if (watch.isSelected() && !message.equals(lastNotice)) { notifyUser(message, NotificationType.WARNING); lastNotice = message; }
                });
            }
        });
    }
    private void ui(Runnable runnable) {
        ToolWindowManager.getInstance(project).invokeLater(() -> { if (!disposed && !project.isDisposed()) runnable.run(); });
    }
    private void notifyUser(String message, NotificationType type) {
        NotificationGroupManager.getInstance().getNotificationGroup("Multi Repo Sync").createNotification(message, type).notify(project);
    }
    private void log(String message) {
        status.setText(message == null ? "" : message.replace('\n', ' '));
        output.append(LocalTime.now().withNano(0) + " " + message + "\n");
        if (output.getDocument().getLength() > 40000) output.setText(output.getText().substring(output.getText().length() - 25000));
        output.setCaretPosition(output.getDocument().getLength());
    }
    private static String str(JsonObject object, String key, String fallback) {
        return object.has(key) && !object.get(key).isJsonNull() ? object.get(key).getAsString() : fallback;
    }
    private static boolean flag(JsonObject object, String key) { return object.has(key) && object.get(key).getAsBoolean(); }
    @Override public void dispose() { disposed = true; timer.stop(); worker.shutdownNow(); }
}
