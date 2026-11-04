package local.multirepo.sync;

import java.io.IOException;
import java.nio.file.*;
import java.util.*;

/** Runs only the interpreter entered by the user. No discovery or fallback. */
final class PythonCommand {
    static String normalize(String configured) {
        String value = configured == null ? "" : configured.strip();
        if (value.length() >= 2 && ((value.startsWith("\"") && value.endsWith("\""))
                || (value.startsWith("'") && value.endsWith("'"))))
            value = value.substring(1, value.length() - 1);
        return value;
    }

    static List<String> command(String configured, List<String> arguments) throws IOException {
        String executable = normalize(configured);
        if (executable.isBlank()) throw new IOException("请在第一行 Python 中填写解释器完整路径。");
        List<String> command = new ArrayList<>();
        command.add(executable);
        String filename = executable.replace('\\', '/');
        filename = filename.substring(filename.lastIndexOf('/') + 1);
        if (filename.matches("(?i)py(?:\\.exe)?")) command.add("-3");
        command.addAll(arguments);
        return command;
    }

    static Process start(String configured, List<String> arguments, Path directory) throws IOException {
        String executable = normalize(configured);
        List<String> command = command(executable, arguments);
        try {
            if (Files.isDirectory(Paths.get(executable)))
                throw new IOException("请选择 Python 可执行文件，例如 python.exe；当前填写的是目录。");
        } catch (InvalidPathException invalid) {
            throw new IOException("Python 路径无效，请只填写可执行文件路径，不要附加命令参数。", invalid);
        }
        ProcessBuilder builder = new ProcessBuilder(command).directory(directory.toFile());
        // Python's Windows pipe defaults may otherwise corrupt Chinese JSON in both directions.
        builder.environment().put("PYTHONIOENCODING", "utf-8");
        builder.environment().put("PYTHONUTF8", "1");
        builder.environment().put("PYTHONUNBUFFERED", "1");
        try { return builder.start(); }
        catch (IOException failure) {
            throw new IOException("无法启动你填写的 Python：" + executable + "\n"
                    + "请在第一行选择本机 python.exe 的完整路径。填写 py 时，需要本机已有 Python Launcher。\n"
                    + "第二行「配置文件」填写同步工具的 config.json 路径。\n"
                    + failure.getMessage(), failure);
        }
    }
}
