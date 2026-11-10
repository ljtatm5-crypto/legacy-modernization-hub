package com.demo.settle;

/**
 * 存量请求对象。amount 使用 double，是 PREC-003 记录的历史坑：
 * 迁移映射表 MAP-002 要求改为 BigDecimal(18,2)。
 */
public class SettleRequest {
    private final String orderId;
    private final double amount;
    private final int quantity;

    public SettleRequest(String orderId, double amount, int quantity) {
        this.orderId = orderId;
        this.amount = amount;
        this.quantity = quantity;
    }

    public String getOrderId() {
        return orderId;
    }

    public double getAmount() {
        return amount;
    }

    public int getQuantity() {
        return quantity;
    }
}
