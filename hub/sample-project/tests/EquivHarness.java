package com.demo.settle;

/**
 * 行为等价性 Harness：同一组输入分别喂给新旧实现，
 * 逐条输出规范化结果、异常类型与 SQL 轨迹，由 Python 侧做差分判定。
 *
 * 用法：java -cp <classpath> com.demo.settle.EquivHarness <实现类名> [用例文件]
 *
 * 用例文件格式（纯文本，避免在 Java 侧引入 JSON 解析）：
 *   orderId|amount|quantity
 * 不传文件时使用内置的 5 条人工用例。
 */
public final class EquivHarness {

    private static final Object[][] CASES = new Object[][] {
            {"SO-2026-0881", 12800.00, 2},
            {"SO-2026-0882", 0.00, 1},
            {"SO-2026-0883", -50.00, 1},
            {"SO-2026-0884", 99.995, 1},
            {"SO-2026-0885", 1000000000.00, 3}
    };

    public static void main(String[] args) throws Exception {
        if (args.length < 1) {
            System.err.println("usage: EquivHarness <implementation class>");
            System.exit(2);
        }
        SettleApi api = (SettleApi) Class.forName(args[0]).getDeclaredConstructor().newInstance();
        Object[][] cases = args.length >= 2 ? loadCases(args[1]) : CASES;
        for (Object[] c : cases) {
            String orderId = (String) c[0];
            double amount = ((Number) c[1]).doubleValue();
            int quantity = ((Number) c[2]).intValue();
            SettleRequest request = new SettleRequest(orderId, amount, quantity);

            String output = "";
            String exception = "";
            try {
                SettleResult result = api.settle(request);
                output = result.toCanonical();
            } catch (SettleException e) {
                exception = e.getMessage();
            }

            StringBuilder sql = new StringBuilder();
            for (String statement : api.lastTrace().statements()) {
                if (sql.length() > 0) {
                    sql.append(";");
                }
                sql.append(statement);
            }
            System.out.println(toJson(orderId, amount, output, exception, sql.toString()));
        }
    }

    /** 读外部用例文件；解析失败的行走不了就跳过，不静默变成空集。 */
    private static Object[][] loadCases(String path) throws Exception {
        java.util.List<Object[]> rows = new java.util.ArrayList<Object[]>();
        for (String line : java.nio.file.Files.readAllLines(
                java.nio.file.Paths.get(path), java.nio.charset.StandardCharsets.UTF_8)) {
            String trimmed = line.trim();
            if (trimmed.isEmpty() || trimmed.startsWith("#")) {
                continue;
            }
            String[] parts = trimmed.split("\\|");
            if (parts.length != 3) {
                System.err.println("skip malformed case: " + trimmed);
                continue;
            }
            rows.add(new Object[] {parts[0], Double.parseDouble(parts[1]), Integer.parseInt(parts[2])});
        }
        if (rows.isEmpty()) {
            System.err.println("no cases loaded from " + path);
            System.exit(3);
        }
        return rows.toArray(new Object[0][]);
    }

    private static String toJson(String id, double amount, String output, String exception, String sql) {
        return "{\"case\":\"" + esc(id) + "\",\"amount\":" + amount
                + ",\"output\":\"" + esc(output)
                + "\",\"exception\":\"" + esc(exception)
                + "\",\"sql\":\"" + esc(sql) + "\"}";
    }

    private static String esc(String value) {
        return value.replace("\\", "\\\\")
                .replace("\"", "\\\"")
                .replace("\n", "\\n")
                .replace("\r", "\\r");
    }
}
