package com.demo.settle;

/**
 * 存量契约测试：不依赖 JUnit，纯 JDK 运行。
 * 关键点：事务边界丢失不会让这些断言失败，因此能真实复现
 * 「编译通过 + 单测通过，但行为已被静默破坏」的场景。
 */
public final class SettleContractTest {

    private static int passed = 0;
    private static int failed = 0;

    private static void expect(boolean condition, String name) {
        if (condition) {
            passed++;
        } else {
            failed++;
            System.err.println("FAIL " + name);
        }
    }

    public static void main(String[] args) throws Exception {
        if (args.length < 1) {
            System.err.println("usage: SettleContractTest <implementation class>");
            System.exit(2);
        }
        String impl = args[0];
        SettleApi api = (SettleApi) Class.forName(impl).getDeclaredConstructor().newInstance();

        SettleResult normal = api.settle(new SettleRequest("SO-2026-0881", 12800.00, 2));
        expect("OK".equals(normal.getStatus()), "normal_status");
        expect("ST-2026-0881|12160.00|OK".equals(normal.toCanonical()), "normal_canonical");

        boolean invalidThrown = false;
        try {
            api.settle(new SettleRequest("SO-2026-0882", 0.00, 1));
        } catch (SettleException e) {
            invalidThrown = "INVALID_AMOUNT".equals(e.getMessage());
        }
        expect(invalidThrown, "invalid_amount");

        SettleResult large = api.settle(new SettleRequest("SO-2026-0885", 1000000000.00, 3));
        expect(large.toCanonical().startsWith("ST-2026-0885|950000000.00"), "large_amount");

        SettleResult precision = api.settle(new SettleRequest("SO-2026-0884", 99.995, 1));
        expect("ST-2026-0884|95.00|OK".equals(precision.toCanonical()), "precision_boundary");

        System.out.println("{\"impl\":\"" + impl + "\",\"passed\":" + passed
                + ",\"failed\":" + failed + "}");
        if (failed > 0) {
            System.exit(1);
        }
    }
}
