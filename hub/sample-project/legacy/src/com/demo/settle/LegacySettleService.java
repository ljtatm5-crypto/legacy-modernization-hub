package com.demo.settle;

/**
 * 存量实现：Java 8 + 私有 ORM + 存储过程口径，事务边界由 XML 声明。
 *
 * RULE-021 结算单创建与库存扣减必须在同一事务边界内。
 * RULE-014 返利按实付金额 5% 计算，四舍五入保留 2 位小数。
 * RULE-027 重复回调按 orderNo 幂等，settleNo 由 orderNo 派生。
 *
 * 已知历史坑：金额用 double 做中间计算（PREC-003），迁移时须改为定点数。
 */
public class LegacySettleService implements SettleApi {

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
            double rebate = Math.round(request.getAmount() * 0.05 * 100.0) / 100.0;
            double total = request.getAmount() - rebate;
            dao.updateOrder(request.getOrderId(), total);
            dao.insertSettle(request.getOrderId(), total, rebate);
            dao.commit();
            return new SettleResult(
                    "ST-" + request.getOrderId().substring(3),
                    String.valueOf(total),
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
