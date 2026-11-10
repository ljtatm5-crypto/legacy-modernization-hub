import com.sun.source.tree.*;
import com.sun.source.util.JavacTask;
import com.sun.source.util.SourcePositions;
import com.sun.source.util.TreeScanner;
import com.sun.source.util.Trees;

import javax.tools.JavaCompiler;
import javax.tools.JavaFileObject;
import javax.tools.StandardJavaFileManager;
import javax.tools.ToolProvider;
import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import java.util.stream.Stream;

/**
 * 通用存量代码巡检器：对任意 Java 源码目录做 AST 级扫描。
 *
 * 与 SemanticScan 的区别：SemanticScan 面向 OrderSettle 模块的具体规则，
 * 这个面向「迁移到 JDK 17 时的通用风险」，因此可以拿真实开源项目当数据集跑。
 *
 * 用法：java -cp build LegacyScan <源码根目录> [最大文件数]
 */
public final class LegacyScan {

    private static int classes, methods, imports, filesParsed;
    private static final List<String[]> FINDINGS = new ArrayList<>();
    private static final Map<String, Integer> COUNTS = new LinkedHashMap<>();

    public static void main(String[] args) throws Exception {
        PrintStream out = new PrintStream(new FileOutputStream(FileDescriptor.out), true, "UTF-8");
        if (args.length < 1) {
            System.err.println("usage: LegacyScan <source-root> [max-files]");
            System.exit(2);
        }
        Path root = Paths.get(args[0]);
        int limit = args.length >= 2 ? Integer.parseInt(args[1]) : 4000;

        List<Path> sources = new ArrayList<>();
        try (Stream<Path> walk = Files.walk(root)) {
            walk.filter(path -> path.toString().endsWith(".java"))
                .filter(path -> !path.toString().contains("/test/"))
                .sorted()
                .limit(limit)
                .forEach(sources::add);
        }

        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        StandardJavaFileManager manager = compiler.getStandardFileManager(null, null, StandardCharsets.UTF_8);
        Iterable<? extends JavaFileObject> units = manager.getJavaFileObjectsFromFiles(
                sources.stream().map(Path::toFile).collect(java.util.stream.Collectors.toList()));
        JavacTask task = (JavacTask) compiler.getTask(
                null, manager, null, List.of("-proc:none"), null, units);

        Trees trees = Trees.instance(task);
        long began = System.currentTimeMillis();
        for (CompilationUnitTree unit : task.parse()) {
            filesParsed++;
            new Scan(unit, trees.getSourcePositions()).scan(unit, null);
        }
        long elapsed = System.currentTimeMillis() - began;

        COUNTS.put("files_scanned", sources.size());
        COUNTS.put("files_parsed", filesParsed);
        COUNTS.put("classes", classes);
        COUNTS.put("methods", methods);
        COUNTS.put("imports", imports);
        COUNTS.put("findings", FINDINGS.size());
        COUNTS.put("elapsed_ms", (int) elapsed);

        Map<String, Integer> byRule = new LinkedHashMap<>();
        for (String[] finding : FINDINGS) {
            byRule.merge(finding[0], 1, Integer::sum);
        }
        StringBuilder json = new StringBuilder();
        json.append("{\"root\":\"").append(esc(root.toString())).append("\"");
        json.append(",\"counts\":{");
        boolean first = true;
        for (Map.Entry<String, Integer> entry : COUNTS.entrySet()) {
            if (!first) json.append(",");
            json.append("\"").append(entry.getKey()).append("\":").append(entry.getValue());
            first = false;
        }
        json.append("},\"by_rule\":{");
        first = true;
        for (Map.Entry<String, Integer> entry : byRule.entrySet()) {
            if (!first) json.append(",");
            json.append("\"").append(entry.getKey()).append("\":").append(entry.getValue());
            first = false;
        }
        json.append("},\"samples\":[");
        int shown = 0;
        for (String[] finding : FINDINGS) {
            if (shown++ >= 30) break;
            if (shown > 1) json.append(",");
            json.append("{\"rule\":\"").append(finding[0]).append("\",\"file\":\"")
                .append(esc(finding[1])).append("\",\"line\":").append(finding[2])
                .append(",\"evidence\":\"").append(esc(finding[3])).append("\"}");
        }
        json.append("]}");
        out.println(json);
    }

    private static final class Scan extends TreeScanner<Void, Void> {
        private final CompilationUnitTree unit;
        private final SourcePositions positions;
        private final String file;

        Scan(CompilationUnitTree unit, SourcePositions positions) {
            this.unit = unit;
            this.positions = positions;
            this.file = Paths.get(unit.getSourceFile().toUri()).getFileName().toString();
        }

        private void hit(String rule, Tree node, String evidence) {
            long position = positions == null ? 0 : positions.getStartPosition(unit, node);
            long line = (unit.getLineMap() == null || position < 0)
                    ? 0 : unit.getLineMap().getLineNumber(position);
            FINDINGS.add(new String[] {rule, file, String.valueOf(line), evidence});
        }

        @Override
        public Void visitImport(ImportTree node, Void unused) {
            imports++;
            String name = node.getQualifiedIdentifier().toString();
            if (name.equals("java.util.Date")) {
                hit("TZ-002", node, "import java.util.Date");
            }
            if (name.equals("java.text.SimpleDateFormat")) {
                hit("TZ-003", node, "import java.text.SimpleDateFormat");
            }
            return null;
        }

        @Override
        public Void visitClass(ClassTree node, Void unused) {
            classes++;
            return super.visitClass(node, unused);
        }

        @Override
        public Void visitMethod(MethodTree node, Void unused) {
            methods++;
            if ("finalize".equals(node.getName().toString())) {
                hit("FIN-001", node, "finalize() 在 JDK 17 已废弃");
            }
            return super.visitMethod(node, unused);
        }

        @Override
        public Void visitNewClass(NewClassTree node, Void unused) {
            String type = node.getIdentifier().toString();
            if (type.endsWith("Date")) {
                hit("TZ-002", node, "new " + type + "()");
            } else if (type.endsWith("SimpleDateFormat")) {
                hit("TZ-003", node, "new SimpleDateFormat(...)");
            }
            return super.visitNewClass(node, unused);
        }

        @Override
        public Void visitCatch(CatchTree node, Void unused) {
            BlockTree body = node.getBlock();
            if (body.getStatements().isEmpty()) {
                hit("EX-007", node, "空 catch 块吞掉异常");
            }
            return super.visitCatch(node, unused);
        }

        @Override
        public Void visitMethodInvocation(MethodInvocationTree node, Void unused) {
            String text = node.getMethodSelect().toString();
            if (text.startsWith("System.out") || text.startsWith("System.err")) {
                hit("LOG-001", node, text + "(...) 直接写标准流");
            }
            return super.visitMethodInvocation(node, unused);
        }
    }

    private static String esc(String value) {
        return value.replace("\\", "\\\\").replace("\"", "\\\"");
    }
}
