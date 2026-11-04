package local.multirepo.sync;

import com.google.gson.*;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.TimeUnit;

/** Native process/JSON regressions for the manually entered interpreter. */
public final class ManualPythonTest {
    private static int passed;
    private interface Check { void run() throws Exception; }
    private static void test(String name, Check check) throws Exception {
        check.run(); passed++; System.out.println("PASS " + name);
    }
    private static void require(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }
    private static String failure(Check check) throws Exception {
        try { check.run(); }
        catch (IOException error) { return error.getMessage(); }
        throw new AssertionError("Expected a readable interpreter error");
    }
    public static void main(String[] args) throws Exception {
        String python = args[0];
        Path temporary = Paths.get(args[1]);
        test("quoted manual paths retain spaces", () -> {
            String path = "C:\\Program Files\\Python312\\python.exe";
            require(PythonCommand.command("\"" + path + "\"", List.of("script.py")).equals(List.of(path, "script.py")), "quoted path split or changed");
        });
        test("Windows launcher receives only its expected -3 flag", () -> {
            require(PythonCommand.command("C:\\Windows\\py.exe", List.of("script.py"))
                    .equals(List.of("C:\\Windows\\py.exe", "-3", "script.py")), "launcher flag missing");
            require(PythonCommand.command("C:\\Python312\\python.exe", List.of("script.py")).size() == 2, "python.exe received launcher flag");
        });
        test("blank interpreter is rejected without auto detection", () -> {
            require(failure(() -> PythonCommand.command("", List.of())).contains("第一行"), "blank interpreter silently accepted");
        });
        test("manual arguments are not split or run through a shell", () -> {
            require(PythonCommand.command("python.exe & injected", List.of()).size() == 1, "input interpreted as shell commands");
        });
        test("missing interpreter reports both setting meanings without fallback", () -> {
            String missing = temporary.resolve("missing-python-executable").toString();
            String error = failure(() -> PythonCommand.start(missing, List.of(), temporary));
            require(error.contains(missing) && error.contains("完整路径") && error.contains("config.json"), "missing program error not actionable");
        });
        test("directory selected instead of executable is explained", () -> {
            require(failure(() -> PythonCommand.start(temporary.toString(), List.of(), temporary)).contains("目录"), "directory mistake not explained");
        });
        test("manual Python pipe supports Chinese and UTF-8 environment", () -> {
            Process process = PythonCommand.start(python, List.of("-c", "import os; print('内网配置'); print(os.environ['PYTHONIOENCODING'])"), temporary);
            process.getOutputStream().close();
            try {
                require(process.waitFor(10, TimeUnit.SECONDS), "Python timeout");
                String output = new String(process.getInputStream().readAllBytes(), StandardCharsets.UTF_8);
                require(process.exitValue() == 0 && output.contains("内网配置") && output.contains("utf-8"), "UTF-8 pipe corrupted");
            } finally { if (process.isAlive()) process.destroyForcibly(); }
        });
        test("native bridge round trips Chinese JSON and paths with spaces", () -> {
            Path config = temporary.resolve("配置 with spaces").resolve("config.json");
            Bridge bridge = new Bridge(temporary.resolve("引擎 with spaces"));
            JsonObject payload = new JsonObject(); payload.addProperty("version", 1);
            payload.addProperty("log_directory", temporary.resolve("中文 logs").toString());
            JsonObject repo = new JsonObject(); repo.addProperty("name", "demo");
            repo.addProperty("path", temporary.resolve("内网 project").toString()); repo.addProperty("source", "origin");
            repo.addProperty("source_branch", "开发"); repo.addProperty("target_branch", "内网"); repo.addProperty("strategy", "snapshot");
            JsonArray paths = new JsonArray(); paths.add("配置/内网/**"); repo.add("preserve_paths", paths);
            JsonArray repositories = new JsonArray(); repositories.add(repo); payload.add("repositories", repositories);
            require(bridge.call(python, config.toString(), "save", null, payload).get("ok").getAsBoolean(), "save failed");
            JsonObject loaded = bridge.call(python, config.toString(), "get", null, null);
            JsonObject actual = loaded.getAsJsonObject("config").getAsJsonArray("repositories").get(0).getAsJsonObject();
            require("内网".equals(actual.get("target_branch").getAsString())
                    && "配置/内网/**".equals(actual.getAsJsonArray("preserve_paths").get(0).getAsString()), "Chinese JSON corrupted");
        });
        System.out.println(passed + " manual interpreter/bridge tests passed");
    }
}
