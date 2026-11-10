package com.demo.settle;

import java.math.BigDecimal;
import java.math.RoundingMode;

/**
 * 迁移后的目标实现：Spring Boot 3 / JDK 17 口径。
 * 由可插拔生成适配层按 MAP-001 / MAP-002 规则从存量实现变换而来。
 * 保留事务边界与 SQL 轨迹，金额改为定点数。
 */
public class ModernSettleService implements SettleApi {

    private SqlTrace lastTrace;

    @TxRequired(propagation = "REQUIRED")
    public SettleResult settle(SettleRequest request) throws SettleException {
        RecordingDao dao = new RecordingDao();
        lastTrace = dao.getTrace();
        dao.begin();
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
            dao.commit();
            return new SettleResult(
                    "ST-" + request.getOrderId().substring(3),
                    totalText,
                    "OK");
        } catch (SettleException e) {
            dao.rollback();
            throw e;
        }
    }

    public SqlTrace lastTrace() {
        return lastTrace;
    }
}
