package com.demo.settle;

/** 结算领域异常，异常类型与 code 均属于行为等价性的比对范围。 */
public class SettleException extends Exception {
    public SettleException(String code) {
        super(code);
    }
}
