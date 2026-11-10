package com.demo.settle;

/** 新旧实现共同遵守的契约，双跑差分按此接口加载两个实现。 */
public interface SettleApi {
    SettleResult settle(SettleRequest request) throws SettleException;

    SqlTrace lastTrace();
}
