package com.demo.settle;

import java.math.BigDecimal;
import java.math.RoundingMode;

/**
 * 演示用缺陷版本：生成适配层在变换时漏掉了 MAP-001 事务映射。
 * 编译与存量单测都不会报错，只有双跑差分与 AST 语义巡检能发现。
 */
public class ModernSettleService implements SettleApi {

    private SqlTrace lastTrace;

    public SettleResult settle(SettleRequest request) throws SettleException {
        RecordingDao dao = new RecordingDao();
        lastTrace = dao.getTrace();
        try {
            if (request.getAmount() <= 0) {
                throw new SettleException("INVALID_AMOUNT");
            }
            BigDecimal amount = BigDecimal.valueOf(request.getAmount());
            BigDecimal rebate = amount.multiply(new BigDecimal("0.05"))
                    .setScale(2, RoundingMode.HALF_UP);
            BigDecimal total = amount.subtract(rebate).setScale(2, RoundingMode.HALF_UP);
            String totalText = total.toPlainString();
            String rebateText = rebate.toPlainString();
            dao.updateOrder(request.getOrderId(), totalText);
            dao.insertSettle(request.getOrderId(), totalText, rebateText);
            return new SettleResult(
                    "ST-" + request.getOrderId().substring(3),
                    totalText,
                    "OK");
        } catch (SettleException e) {
            throw e;
        }
    }

    public SqlTrace lastTrace() {
        return lastTrace;
    }
}
