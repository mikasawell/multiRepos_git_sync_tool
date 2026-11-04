package local.multirepo.sync;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.intellij.openapi.application.PathManager;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.*;

final class Bridge {
    private final Path engine;

    Bridge() throws IOException {
        engine = Paths.get(PathManager.getSystemPath(), "multi-repo-sync", "engine-0.2.0");
        Files.createDirectories(engine);
        for (String name : List.of("git_sync.py", "snapshot_sync.py", "ide_bridge.py")) {
            try (InputStream stream = Bridge.class.getResourceAsStream("/engine/" + name)) {
                if (stream == null) throw new IOException("插件缺少同步引擎：" + name);
                Files.copy(stream, engine.resolve(name), StandardCopyOption.REPLACE_EXISTING);
            }
        }
    }

    JsonObject call(String python, String config, String operation, String repo, JsonObject request) throws Exception {
        List<String> command = new ArrayList<>();
        command.add(python);
        if (Paths.get(python).getFileName().toString().matches("(?i)py(?:\\.exe)?")) command.add("-3");
        command.addAll(List.of(engine.resolve("ide_bridge.py").toString(), "--config", config, operation));
        if (repo != null) command.addAll(List.of("--repo", repo));
        Process process = new ProcessBuilder(command).directory(engine.toFile()).start();
        ExecutorService readers = Executors.newFixedThreadPool(2);
        try {
            Future<String> stdout = readers.submit(() -> new String(process.getInputStream().readAllBytes(), StandardCharsets.UTF_8));
            Future<String> stderr = readers.submit(() -> new String(process.getErrorStream().readAllBytes(), StandardCharsets.UTF_8));
            try (OutputStream stream = process.getOutputStream()) {
                if (request != null) stream.write(request.toString().getBytes(StandardCharsets.UTF_8));
            }
            if (!process.waitFor(20, TimeUnit.MINUTES)) {
                process.descendants().forEach(ProcessHandle::destroy);
                process.destroyForcibly();
                throw new IOException("同步操作超时，请检查仓库状态。暂存计划会保留。");
            }
            String output = stdout.get(10, TimeUnit.SECONDS).strip();
            String errors = stderr.get(10, TimeUnit.SECONDS).strip();
            JsonObject result;
            try { result = JsonParser.parseString(output).getAsJsonObject(); }
            catch (Exception invalid) { throw new IOException("引擎没有返回有效结果：" + output + "\n" + errors); }
            if (!result.has("ok") || !result.get("ok").getAsBoolean())
                throw new IOException(result.has("error") ? result.get("error").getAsString() : errors);
            return result;
        } finally {
            if (process.isAlive()) {
                process.descendants().forEach(ProcessHandle::destroy);
                process.destroyForcibly();
            }
            readers.shutdownNow();
        }
    }
}
