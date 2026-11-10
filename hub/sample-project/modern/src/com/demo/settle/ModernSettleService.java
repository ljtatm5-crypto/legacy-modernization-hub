package com.demo.settle;

import java.math.BigDecimal;
import java.math.RoundingMode;

public class ModernSettleService implements SettleApi {

    private static final BigDecimal REBATE_RATE = new BigDecimal("0.05");
    private static final BigDecimal HUNDRED = new BigDecimal("100");
    private static final int SCALE = 2;

    private SqlTrace lastTrace;

    @TxRequired(propagation = "REQUIRED")
    public SettleResult settle(SettleRequest request) throws SettleException {
        RecordingDao dao = new RecordingDao();
        lastTrace = dao.getTrace();
        dao.begin();
        try {
            BigDecimal amount = BigDecimal.valueOf(request.getAmount());
            if (amount.compareTo(BigDecimal.ZERO) <= 0) {
                throw new SettleException("INVALID_AMOUNT");
            }
            BigDecimal rebate = amount.multiply(REBATE_RATE)
                    .setScale(SCALE, RoundingMode.HALF_UP);
            BigDecimal total = amount.subtract(rebate)
                    .setScale(SCALE, RoundingMode.HALF_UP);
            String totalText = total.setScale(SCALE, RoundingMode.HALF_UP).toPlainString();
            String rebateText = rebate.setScale(SCALE, RoundingMode.HALF_UP).toPlainString();
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