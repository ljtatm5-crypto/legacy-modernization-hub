package com.demo.settle;

import java.lang.annotation.ElementType;
import java.lang.annotation.Retention;
import java.lang.annotation.RetentionPolicy;
import java.lang.annotation.Target;

/**
 * 事务边界标记。存量系统由 applicationContext-tx.xml 的 <tx:advice> 声明等价语义，
 * 迁移到 Spring Boot 3 后必须保留 REQUIRED 传播行为。
 * 对应业务规则 RULE-021，语义巡检规则 TX-001。
 */
@Retention(RetentionPolicy.RUNTIME)
@Target(ElementType.METHOD)
public @interface TxRequired {
    String propagation() default "REQUIRED";
}
