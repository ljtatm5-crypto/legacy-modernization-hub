package com.demo.settle;

import java.math.BigDecimal;
import java.math.RoundingMode;

/**
 * 差分比对使用规范化输出：金额统一到 2 位小数，
 * 避免 "12160.0" 与 "12160.00" 这类字面差异干扰语义等价判断。
 */
public class SettleResult {
    private final String settleNo;
    private final String amountText;
    private final String status;

    public SettleResult(String settleNo, String amountText, String status) {
        this.settleNo = settleNo;
        this.amountText = amountText;
        this.status = status;
    }

    public String getSettleNo() {
        return settleNo;
    }

    public String getStatus() {
        return status;
    }

    public String toCanonical() {
        BigDecimal normalized = new BigDecimal(amountText).setScale(2, RoundingMode.HALF_UP);
        return settleNo + "|" + normalized.toPlainString() + "|" + status;
    }
}
