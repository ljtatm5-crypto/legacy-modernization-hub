import com.sun.source.tree.AnnotationTree;
import com.sun.source.tree.CatchTree;
import com.sun.source.tree.CompilationUnitTree;
import com.sun.source.tree.ImportTree;
import com.sun.source.tree.MethodTree;
import com.sun.source.tree.VariableTree;
import com.sun.source.util.JavacTask;
import com.sun.source.util.SourcePositions;
import com.sun.source.util.TreeScanner;
import com.sun.source.util.Trees;

import javax.tools.DiagnosticCollector;
import javax.tools.JavaCompiler;
import javax.tools.JavaFileObject;
import javax.tools.StandardJavaFileManager;
import javax.tools.ToolProvider;
import java.io.File;
import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.PrintStream;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/**
 * 工程级语义巡检：直接用 JDK Compiler Tree API 解析 Java 语法树，
 * 不依赖第三方 parser，也不需要目标工程能编译通过。
 *
 * 用法：java SemanticScan <源文件1> [源文件2 ...]
 * 输出：JSON 数组，每项含 rule / risk / file / line / evidence。
 */
public final class SemanticScan {

    static final class Finding {
        final String rule;
        final String risk;
        final String file;
        final long line;
        final String evidence;

        Finding(String rule, String risk, String file, long line, String evidence) {
            this.rule = rule;
            this.risk = risk;
            this.file = file;
            this.line = line;
            this.evidence = evidence;
        }

        String toJson() {
            return "{\"rule\":\"" + esc(rule) + "\",\"risk\":\"" + esc(risk)
                    + "\",\"file\":\"" + esc(file) + "\",\"line\":" + line
                    + ",\"evidence\":\"" + esc(evidence) + "\"}";
        }
    }

    public static void main(String[] args) throws Exception {
        if (args.length == 0) {
            System.out.println("[]");
            return;
        }
        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        if (compiler == null) {
            System.err.println("JDK required (no system Java compiler found)");
            System.exit(2);
        }
        DiagnosticCollector<JavaFileObject> diagnostics = new DiagnosticCollector<JavaFileObject>();
        StandardJavaFileManager manager = compiler.getStandardFileManager(diagnostics, null, null);
        Iterable<? extends JavaFileObject> units =
                manager.getJavaFileObjectsFromStrings(Arrays.asList(args));
        JavacTask task = (JavacTask) compiler.getTask(
                null, manager, diagnostics, Arrays.asList("-proc:none"), null, units);
        Iterable<? extends CompilationUnitTree> parsed = task.parse();
        Trees trees = Trees.instance(task);
        final List<Finding> findings = new ArrayList<Finding>();

        for (final CompilationUnitTree unit : parsed) {
            final String fileName = new File(unit.getSourceFile().getName()).getName();
            final SourcePositions positions = trees.getSourcePositions();
            final LineMapProxy lines = new LineMapProxy(unit);

            new TreeScanner<Void, Void>() {
                private void add(String rule, String risk, com.sun.source.tree.Tree node, String evidence) {
                    long start = positions.getStartPosition(unit, node);
                    findings.add(new Finding(rule, risk, fileName,
                            start >= 0 ? lines.line(start) : 0, evidence));
                }

                @Override
                public Void visitMethod(MethodTree node, Void unused) {
                    if ("settle".equals(node.getName().toString())) {
                        boolean hasTx = false;
                        for (AnnotationTree annotation : node.getModifiers().getAnnotations()) {
                            String name = annotation.getAnnotationType().toString();
                            if (name.endsWith("TxRequired") || name.endsWith("Transactional")) {
                                hasTx = true;
                            }
                        }
                        if (!hasTx) {
                            add("TX-001", "high", node,
                                    "settle() 缺少事务边界声明，REQUIRED 传播行为可能丢失");
                        }
                    }
                    return super.visitMethod(node, unused);
                }

                @Override
                public Void visitCatch(CatchTree node, Void unused) {
                    if (node.getBlock().getStatements().isEmpty()) {
                        add("EX-007", "medium", node, "空 catch 块，异常被吞没");
                    }
                    return super.visitCatch(node, unused);
                }

                @Override
                public Void visitVariable(VariableTree node, Void unused) {
                    String type = node.getType().toString();
                    String name = node.getName().toString().toLowerCase();
                    if ("double".equals(type)
                            && (name.contains("amount") || name.contains("rebate") || name.contains("total"))) {
                        add("PREC-003", "high", node,
                                "金额变量仍使用 double：" + node.getName());
                    }
                    if (type.contains("HashMap") && (name.contains("cache") || name.contains("map"))) {
                        add("CONC-005", "medium", node,
                                "并发上下文使用非线程安全容器：" + type);
                    }
                    return super.visitVariable(node, unused);
                }

                @Override
                public Void visitImport(ImportTree node, Void unused) {
                    String qualified = node.getQualifiedIdentifier().toString();
                    if (qualified.equals("java.util.Date")
                            || qualified.equals("java.text.SimpleDateFormat")) {
                        add("TZ-002", "low", node,
                                "遗留日期 API，需确认 Asia/Shanghai 时区语义");
                    }
                    return super.visitImport(node, unused);
                }
            }.scan(unit, null);
        }

        StringBuilder out = new StringBuilder("[");
        for (int i = 0; i < findings.size(); i++) {
            if (i > 0) {
                out.append(",");
            }
            out.append(findings.get(i).toJson());
        }
        out.append("]");
        PrintStream utf8 = new PrintStream(new FileOutputStream(FileDescriptor.out), true, "UTF-8");
        utf8.println(out.toString());
        utf8.flush();
        manager.close();
    }

    /** 避免在匿名内部类里捕获可变的 line map 引用。 */
    static final class LineMapProxy {
        private final com.sun.source.tree.LineMap map;

        LineMapProxy(CompilationUnitTree unit) {
            this.map = unit.getLineMap();
        }

        long line(long position) {
            return map.getLineNumber(position);
        }
    }

    private static String esc(String value) {
        return value.replace("\\", "\\\\")
                .replace("\"", "\\\"")
                .replace("\n", "\\n");
    }
}
