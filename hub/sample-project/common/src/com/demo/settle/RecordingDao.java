package com.demo.settle;

import java.math.BigDecimal;
import java.math.RoundingMode;

/**
 * 存量私有 ORM 的最小可复现替身。它不连数据库，但真实记录 SQL 顺序、
 * 事务边界与写入值，因此双跑差分能在离线环境下验证事务语义是否被破坏。
 */
public class RecordingDao {
    private final SqlTrace trace = new SqlTrace();

    public SqlTrace getTrace() {
        return trace;
    }

    private static String money(double value) {
        return BigDecimal.valueOf(value).setScale(2, RoundingMode.HALF_UP).toPlainString();
    }

    private static String money(String value) {
        return new BigDecimal(value).setScale(2, RoundingMode.HALF_UP).toPlainString();
    }

    public void begin() {
        trace.add("BEGIN");
    }

    public void commit() {
        trace.add("COMMIT");
    }

    public void rollback() {
        trace.add("ROLLBACK");
    }

    public void updateOrder(String orderId, double total) {
        trace.add("UPDATE t_order SET settle_amount=" + money(total)
                + " WHERE order_no='" + orderId + "'");
    }

    public void updateOrder(String orderId, String total) {
        trace.add("UPDATE t_order SET settle_amount=" + money(total)
                + " WHERE order_no='" + orderId + "'");
    }

    public void insertSettle(String orderId, double total, double rebate) {
        trace.add("INSERT INTO t_settle(order_no,total,rebate) VALUES('"
                + orderId + "'," + money(total) + "," + money(rebate) + ")");
    }

    public void insertSettle(String orderId, String total, String rebate) {
        trace.add("INSERT INTO t_settle(order_no,total,rebate) VALUES('"
                + orderId + "'," + money(total) + "," + money(rebate) + ")");
    }
}
