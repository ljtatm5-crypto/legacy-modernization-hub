package com.demo.settle;

import java.math.BigDecimal;
import java.math.RoundingMode;

/**
 * 受控故障夹具 · 语义型：把金额先四舍五入到 2 位，再拿舍入后的值做零值判断。
 *
 * 这是模型真实犯过的错，且属于"最像对的一种错"：
 * 编译通过、契约测试通过、人工挑的 5 条用例也全部通过；
 * 只有算法生成的零邻域用例（0.001 / 0.004）能发现——
 * 存量对这两个金额返回 0.00，而这里会抛 INVALID_AMOUNT。
 */
public class ModernSettleService implements SettleApi {

    private SqlTrace lastTrace;

    @TxRequired(propagation = "REQUIRED")
    public SettleResult settle(SettleRequest request) throws SettleException {
        RecordingDao dao = new RecordingDao();
        lastTrace = dao.getTrace();
        dao.begin();
        try {
            BigDecimal amount = BigDecimal.valueOf(request.getAmount())
                    .setScale(2, RoundingMode.HALF_UP);
            if (amount.compareTo(BigDecimal.ZERO) <= 0) {
                throw new SettleException("INVALID_AMOUNT");
            }
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
