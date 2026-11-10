package com.demo.settle;

import java.util.ArrayList;
import java.util.List;

/** 记录一次调用产生的 SQL 与事务边界，供双跑差分逐条比对。 */
public class SqlTrace {
    private final List<String> statements = new ArrayList<String>();

    public void add(String sql) {
        statements.add(sql);
    }

    public List<String> statements() {
        return new ArrayList<String>(statements);
    }
}
