/* Legacy Modernization Hub · 前端数据层
 * 所有展示内容来自后端 /api/* 的真实产物，页面不内置任何模拟状态机。
 * 三块核心视图：执行拓扑（DAG）、闸门前置证据、效率三层证明。
 */
(function () {
  'use strict';

  var $ = function (selector, root) { return (root || document).querySelector(selector); };
  var $$ = function (selector, root) { return Array.prototype.slice.call((root || document).querySelectorAll(selector)); };

  var ICON = {
    ok: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="m5 12 4.5 4.5L19 7"/></svg>',
    err: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/></svg>',
    info: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/></svg>',
    warn: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 3 2 20h20z"/><path d="M12 9v5M12 17h.01"/></svg>',
    file: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>'
  };

  var GATES = [
    { key: 'compile', name: '编译', desc: 'javac 四阶段' },
    { key: 'unit', name: '契约测试', desc: '纯 JDK 断言' },
    { key: 'char', name: '特征测试', desc: '锁定当前行为' },
    { key: 'dual', name: '双跑差分', desc: '输出 / 异常 / SQL' },
    { key: 'ast', name: 'AST 语义巡检', desc: 'Compiler Tree API' }
  ];

  var KPI = [
    { key: 'build_stages', unit: ' 阶段' },
    { key: 'characterization_cases', unit: ' 个' },
    { key: 'assertions_passed', unit: ' 条' },
    { key: 'dual_run_diffs', unit: ' 处' },
    { key: 'rework', unit: ' 次' }
  ];

  var lastSignature = '';
  var offlineNotified = false;
  var vetoes = new Set();
  var configPending = false;
  var adapterPending = false;
  var chainCache = null;
  var dagObserver = null;
  var proofExport = null;
  var modalCache = {};

  function esc(value) {
    return String(value === null || value === undefined ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function fmtMs(ms) {
    if (!ms && ms !== 0) { return '—'; }
    return ms >= 1000 ? (ms / 1000).toFixed(1) + ' s' : ms + ' ms';
  }

  function toast(type, title, desc) {
    var el = document.createElement('div');
    el.className = 'toast ' + type;
    el.innerHTML = '<span class="t-ico">' + ICON[type === 'ok' ? 'ok' : type === 'err' ? 'err' : type === 'warn' ? 'warn' : 'info'] +
      '</span><div><b>' + esc(title) + '</b><span>' + esc(desc) + '</span></div>';
    $('#toasts').appendChild(el);
    setTimeout(function () {
      el.style.opacity = '0'; el.style.transform = 'translateX(14px)'; el.style.transition = '.25s';
      setTimeout(function () { el.remove(); }, 260);
    }, 3400);
  }

  function getJSON(url) {
    return fetch(url, { cache: 'no-store' }).then(function (response) {
      if (!response.ok) { throw new Error('HTTP ' + response.status + ' ' + url); }
      return response.json();
    });
  }

  function post(url, payload) {
    return fetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload || {})
    }).then(function (response) { return response.json(); });
  }

  function openModal(title, body) {
    $('#modalTitle').textContent = title;
    $('#modalBody').textContent = body || '（空）';
    $('#modal').hidden = false;
  }

  /* ---------------- 执行拓扑 ---------------- */

  function watchDagSize() {
    if (dagObserver || typeof ResizeObserver === 'undefined') { return; }
    var wrap = $('#dagWrap');
    if (!wrap) { return; }
    dagObserver = new ResizeObserver(function () {
      var steps = (chainCache && chainCache.nodes) || [];
      if (steps.length) { drawEdges(steps); }
    });
    dagObserver.observe(wrap);
  }

  function nodeHtml(step) {
    var attempts = step.attempts > 1 ? '<span class="re">×' + step.attempts + '</span>' : '';
    return '<div class="dag-node" data-s="' + esc(step.status) + '" data-node="' + esc(step.node_id) + '">' +
      '<div class="dn-id">' + esc(step.id) + '</div>' +
      '<div class="dn-title">' + esc(step.name) + '</div>' +
      '<div class="dn-meta"><span>' + fmtMs(step.duration_ms) + '</span>' + attempts +
      '<span>' + (step.artifacts || []).length + ' 产物</span></div></div>';
  }

  function drawEdges(steps) {
    var wrap = $('#dagWrap'), svg = $('#dagEdges');
    if (!wrap || !svg) { return; }
    var box = wrap.getBoundingClientRect();
    var width = Math.max(box.width, wrap.scrollWidth);
    var height = Math.max(box.height, wrap.scrollHeight);
    // 卡片在未激活的视图里时 display:none，量到的全是 0。
    // 这时不要画（否则会留下一堆 0 坐标的路径），等 ResizeObserver 在显示时重画。
    if (!width || !height) {
      svg.innerHTML = '';
      return;
    }
    svg.setAttribute('viewBox', '0 0 ' + width + ' ' + height);
    svg.setAttribute('width', width);
    svg.setAttribute('height', height);

    var nodes = {};
    $$('.dag-node', wrap).forEach(function (el) {
      var rect = el.getBoundingClientRect();
      nodes[el.getAttribute('data-node')] = {
        x: rect.left - box.left + wrap.scrollLeft,
        y: rect.top - box.top + wrap.scrollTop,
        w: rect.width, h: rect.height
      };
    });

    var paths = [];
    steps.forEach(function (step) {
      (step.depends_on || []).forEach(function (dep) {
        var from = nodes[dep], to = nodes[step.node_id];
        if (!from || !to) { return; }
        var x1 = from.x + from.w / 2, y1 = from.y + from.h;
        var x2 = to.x + to.w / 2, y2 = to.y;
        var mid = (y1 + y2) / 2;
        var color = step.status === 'done' ? '#9fd8c0' : (step.status === 'running' ? '#9dbcf5' : '#c9d4e3');
        paths.push('<path d="M' + x1 + ' ' + y1 + ' C' + x1 + ' ' + mid + ',' + x2 + ' ' + mid + ',' + x2 + ' ' + y2 +
          '" fill="none" stroke="' + color + '" stroke-width="1.5" marker-end="url(#dagArrow)"/>');
        paths.push('<circle cx="' + x1 + '" cy="' + y1 + '" r="2.4" fill="' + color + '"/>');
      });
    });
    svg.innerHTML = '<defs><marker id="dagArrow" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto">' +
      '<path d="M0 0 L7 3.5 L0 7 z" fill="#c9d4e3"/></marker></defs>' + paths.join('');
  }

  function renderDag(status, wf) {
    var steps = status.state.steps || [];
    if (!steps.length) {
      $('#dagRows').innerHTML = '<div class="empty" style="padding:26px">运行链路后生成执行拓扑</div>';
      $('#dagEdges').innerHTML = '';
      return;
    }
    var byLayer = {};
    steps.forEach(function (step) { (byLayer[step.layer] = byLayer[step.layer] || []).push(step); });
    var layers = Object.keys(byLayer).map(Number).sort(function (a, b) { return a - b; });
    $('#dagRows').innerHTML = layers.map(function (layer) {
      return '<div class="dag-row"><span class="dr-l">L' + layer + '</span>' +
        '<div class="dr-body">' + byLayer[layer].map(nodeHtml).join('') + '</div></div>';
    }).join('');

    var graph = (wf && wf.graph) || {};
    var total = steps.reduce(function (sum, step) { return sum + (step.duration_ms || 0); }, 0);
    var done = steps.filter(function (step) { return step.status === 'done'; }).length;
    $('#chainSummary').textContent = done + ' / ' + steps.length + ' 节点 · 累计 ' + fmtMs(total);

    var wide = Math.max.apply(null, layers.map(function (layer) { return byLayer[layer].length; }));
    var parallel = layers.filter(function (layer) { return byLayer[layer].length > 1; }).length;
    var chips = [
      { label: '来源', value: (status.state.workflow && status.state.workflow.source && status.state.workflow.source.kind === 'file')
          ? 'hub/workflow.yaml' : '内置 DAG' },
      { label: '节点', value: steps.length + ' 个' },
      { label: '边', value: ((wf && wf.graph && wf.graph.edges) ? wf.graph.edges.length : steps.reduce(function (n, s) { return n + (s.depends_on || []).length; }, 0)) + ' 条' },
      { label: '层', value: layers.length + ' 层' },
      { label: '并发', value: (status.state.workflow && status.state.workflow.concurrency) || 3 },
      { label: '最宽层', value: wide + ' 节点' },
      { label: '并行层', value: parallel + ' 层' }
    ];
    if (graph.critical_ms) {
      chips.push({ label: '关键路径', value: fmtMs(graph.critical_ms), hl: true });
      chips.push({ label: '串行需', value: fmtMs(graph.serial_ms) });
    }
    $('#dagMeta').innerHTML = chips.map(function (chip) {
      return '<span class="dag-chip' + (chip.hl ? ' hl' : '') + '">' + esc(chip.label) +
        ' <b>' + esc(chip.value) + '</b></span>';
    }).join('');

    $('#chainMeta').textContent = status.state.finished
      ? '轨迹含 ' + (status.state.trace || []).length + ' 条事件 · 点击导出可回放'
      : '悬停节点查看依赖；失败回边会在此重绘';

    chainCache = {
      exported_at: new Date().toISOString(),
      module: 'OrderSettleService',
      generator: status.generator,
      ai_generated: !!status.aiGenerated,
      workflow: status.state.workflow || {},
      nodes: steps,
      trace: status.state.trace || [],
      edges: (wf && wf.graph && wf.graph.edges) || [],
      critical_path: (graph.critical_path || []),
      gate: status.state.gate,
      gate_results: status.state.gate_results,
      rework: status.state.rework,
      measured: status.measured,
      artifact_files: (status.artifactCount || 0)
    };

    steps.forEach(function (step) {
      var el = $('.dag-node[data-node="' + step.node_id + '"]');
      if (el && step.depends_on && step.depends_on.length) {
        el.setAttribute('title', step.id + ' ← ' + step.depends_on.join(', '));
      }
    });
    watchDagSize();
    requestAnimationFrame(function () { drawEdges(steps); });
  }

  /* ---------------- 左栏节点列表 ---------------- */

  function renderSkills(status) {
    var steps = status.state.steps || [];
    $('#skillList').innerHTML = steps.map(function (step, index) {
      var state = step.status || 'pending';
      var flag = state === 'done' ? '<span class="skill-flag s-flag-done">完成</span>' :
        state === 'running' ? '<span class="skill-flag s-flag-run">运行中</span>' :
        state === 'fail' || state === 'failed' ? '<span class="skill-flag s-flag-fail">拦下</span>' :
        state === 'waiting' ? '<span class="skill-flag s-flag-wait">待签核</span>' : '';
      var meta = fmtMs(step.duration_ms) + ' · ' + (step.artifacts || []).length + ' 产物' +
        (step.attempts > 1 ? ' · 第 ' + step.attempts + ' 次' : '') +
        (step.depends_on && step.depends_on.length ? ' · 依赖 ' + step.depends_on.length : '');
      return '<div class="skill" data-s="' + state + '" data-i="' + index + '">' +
        '<div class="skill-n">' + (state === 'done' ? '✓' : (state === 'fail' || state === 'failed') ? '!' : String(index + 1).padStart(2, '0')) + '</div>' +
        '<div class="skill-main"><div class="skill-name">' + esc(step.id) + '</div>' +
        '<div class="skill-desc"><b class="s-title">' + esc(step.name) + '</b> · L' + step.layer + '</div>' +
        '<div class="skill-meta"><span>' + esc(meta) + '</span></div></div>' + flag + '</div>';
    }).join('');
    var done = steps.filter(function (step) { return step.status === 'done'; }).length;
    $('#chainProgress').textContent = done + ' / ' + (steps.length || 9);
  }

  function renderControls(status) {
    ['#runBtn', '#topRunBtn', '#resumeBtn'].forEach(function (selector) { $(selector).disabled = status.busy; });
    $('#topRunLabel').textContent = status.busy ? '链路执行中…' :
      (status.state.finished ? '重新运行全链路' : '运行全链路');
    var approved = status.state.plan_approved;
    var waiting = (status.state.nodes && status.state.nodes['migrate-module'] || {}).status === 'waiting';
    var strip = $('#stripHITL');
    strip.className = 'pill ' + (approved ? 'pill-ok' : 'pill-warn');
    strip.innerHTML = '<i class="dot"></i>人在回路：' +
      (approved ? '计划已签核' : waiting ? '等待计划签核' : 'Plan 审批制');

    var wf = status.state.workflow || {};
    var source = wf.source && wf.source.kind === 'file' ? 'workflow.yaml' : '内置 DAG';
    $('#stripWf').innerHTML = '<i class="dot"></i>工作流：' + esc(source) + ' · ' +
      (wf.node_count || (status.state.steps || []).length) + ' 节点';
    $('#stripWf').className = 'pill ' + (wf.source && wf.source.kind === 'file' ? 'pill-info' : 'pill-neutral');
  }

  /* ---------------- KPI 与缺陷发现分布 ---------------- */

  function renderKPI(status) {
    var measured = status.measured;
    $$('.kpi-val').forEach(function (el, index) {
      var config = KPI[index];
      if (!config) { return; }
      var value = measured ? measured[config.key] : '—';
      el.innerHTML = esc(value) + '<span class="unit">' + config.unit + '</span>';
    });
  }

  function renderDetect(report, eff) {
    var box = $('#detectBox');
    var first = report.firstAttempt;
    var title = '<div class="detect-title">首轮缺陷在各检查层的发现数</div>';
    var names = ['编译', '契约测试', '特征测试', '双跑差分', 'AST 语义巡检'];

    if (!first) {
      var armA = (eff && eff.ab && eff.ab.arm_a) || null;
      var baselineFail = (armA && armA.kind === 'baseline') ? armA : null;
      var bsamples = (baselineFail && baselineFail.record && baselineFail.record.samples) || [];
      var failFirst = bsamples.filter(function (x) { return x.first_compile_ok === false; }).length;
      box.innerHTML = title +
        names.map(function (name) {
          return '<div class="detect-row"><div class="detect-name">' + name + '</div>' +
            '<div class="detect-track"><div class="detect-fill ok" style="width:100%"></div></div>' +
            '<div class="detect-val" style="color:var(--ok)">通过</div></div>';
        }).join('') +
        '<div style="font-size:11.5px;color:var(--muted);margin-top:7px">' +
        '本轮首轮即五项全绿，无缺陷可统计。' +
        (baselineFail
          ? '对照：A 基线臂在同条件下 ' + failFirst + ' / ' + bsamples.length +
            ' 次<b style="color:var(--danger)">首轮编译失败</b>，本轮 0 次（见效率证明）。'
          : '') +
        '</div>';
      return;
    }
    var findings = first.findings || [];
    var rows = [
      { name: '编译', count: 0 },
      { name: '契约测试', count: (first.unit && first.unit.modern ? first.unit.modern.failed : 0) + (first.unit && first.unit.legacy ? first.unit.legacy.failed : 0) },
      { name: '特征测试', count: 0 },
      { name: '双跑差分', count: (first.diff && first.diff.diff_count) || 0 },
      { name: 'AST 语义巡检', count: findings.filter(function (item) { return item.risk === 'high'; }).length }
    ];
    var max = Math.max.apply(null, rows.map(function (row) { return row.count; })) || 1;
    box.innerHTML = title + rows.map(function (row) {
      var pct = Math.max(row.count ? 9 : 0, row.count / max * 100);
      return '<div class="detect-row"><div class="detect-name">' + row.name + '</div>' +
        '<div class="detect-track"><div class="detect-fill ' + (row.count ? 'hit' : 'zero') +
        '" style="width:' + pct + '%"></div></div>' +
        '<div class="detect-val">' + row.count + ' 处</div></div>';
    }).join('') +
      '<div style="font-size:11.5px;color:var(--muted);margin-top:7px">' +
      '编译与契约测试 <b style="color:var(--danger)">0 命中</b>：这正是静默破坏最危险的地方——只有双跑差分与语义巡检能发现。' +
      '</div>';
  }

  /* ---------------- 闸门 / 差分 / 巡检 ---------------- */

  function renderGate(status, report) {
    var results = status.state.gate_results || {};
    if (!status.state.gate || status.state.gate === 'idle') { results = {}; }
    $('#pipeline').innerHTML = GATES.map(function (gate, index) {
      var value = results[gate.key];
      var state = value === 'pass' ? 'pass' : value === 'fail' ? 'fail' : status.busy ? 'run' : 'idle';
      var mark = value === 'pass' ? '✓' : value === 'fail' ? '!' : '·';
      return '<div class="gate-item" data-s="' + state + '">' +
        '<div class="gi-top"><span class="gi-idx">GATE-0' + (index + 1) + '</span><span class="gi-status">' + mark + '</span></div>' +
        '<div class="gi-name">' + gate.name + '</div><div class="gi-desc">' + gate.desc + '</div></div>';
    }).join('');

    var v = $('#verdict'), text = $('#verdictTxt');
    var gate = status.state.gate;
    var rework = status.state.rework || 0;
    if (gate === 'fail') {
      v.className = 'verdict v-fail'; text.textContent = '等价性不通过 · 闸门阻断合入';
    } else if (gate === 'pass' && report.firstAttempt && report.firstAttempt.verdict === 'fail') {
      v.className = 'verdict v-pass';
      text.textContent = '首轮被拦下 → 自动补丁 → 复检通过（' + rework + ' 次返工）';
    } else if (gate === 'pass') {
      v.className = 'verdict v-pass'; text.textContent = '行为等价性通过 · 五项检查全绿';
    } else if (status.busy) {
      v.className = 'verdict v-run'; text.textContent = '正在执行真编译、双跑差分与语义巡检…';
    } else {
      v.className = 'verdict v-idle'; text.textContent = '闸门待命 · 运行 /equiv-check 后出具结论';
    }
    var svg = v.querySelector('svg');
    if (svg) { svg.outerHTML = ICON[gate === 'fail' ? 'err' : gate === 'pass' ? 'ok' : 'info']; }
  }

  function short(value) {
    var text = String(value === null || value === undefined ? '' : value);
    if (!text) { return '（空）'; }
    return text.length > 76 ? text.slice(0, 76) + '…' : text;
  }

  function renderDiff(report) {
    var diff = report.diffFirst && report.diffFirst.diffs && report.diffFirst.diffs.length
      ? report.diffFirst : report.diffFinal;
    var box = $('#diffBox');
    if (!diff || !diff.legacy) {
      box.innerHTML = '<div class="empty">运行 /equiv-check 后生成真实双跑差分报告</div>';
      return;
    }
    var rows = diff.legacy.map(function (left, index) {
      var right = (diff.modern || [])[index] || {};
      var dimensions = ['output', 'exception', 'sql'].map(function (dimension) {
        var same = left[dimension] === right[dimension];
        var cell = same ? 'good' : 'bad';
        return '<td class="' + cell + '" title="' + esc(left[dimension] || '') + '">' + esc(short(left[dimension])) + '</td>' +
          '<td class="' + cell + '" title="' + esc(right[dimension] || '') + '">' + esc(short(right[dimension])) + '</td>';
      });
      return '<tr><td rowspan="3"><code>' + esc(left.case) + '</code></td>' +
        '<td>输出</td>' + dimensions[0] + '</tr>' +
        '<tr><td>异常</td>' + dimensions[1] + '</tr>' +
        '<tr><td>SQL 轨迹</td>' + dimensions[2] + '</tr>';
    }).join('');
    box.innerHTML = '<table class="tbl"><thead><tr><th style="width:118px">用例</th><th style="width:70px">维度</th>' +
      '<th>存量实现（Java 8）</th><th>目标实现（JDK 17）</th></tr></thead><tbody>' + rows + '</tbody></table>' +
      '<div style="margin-top:9px;font-size:11.5px;color:' + (diff.diff_count ? 'var(--danger)' : 'var(--ok)') + '">' +
      '真实比对 ' + diff.cases + ' 个人工用例 · 差异 ' + (diff.diff_count || 0) + ' 处' +
      (report.diffFirst ? ' · 此处展示首轮拦截时的快照' : '') + '</div>' +
      genLine(diff);
  }

  function genLine(diff) {
    var gen = diff.generated;
    if (!gen || !gen.cases) { return ''; }
    var missed = (!diff.diff_count || diff.diff_count === gen.diff_count) && gen.diff_count > 0;
    return '<div class="gen-line">' +
      '<span class="gen-tag">用例生成</span>' +
      '全组合 <b>' + gen.full_space + '</b> → 算法生成 <b>' + gen.cases + '</b> 例' +
      '（' + esc(gen.strategy || '') + '，两两覆盖 <b>' + Math.round((gen.coverage_t2 || 0) * 100) + '%</b>，' +
      '规模降 <b>' + Math.round((gen.reduction || 0) * 100) + '%</b>）· ' +
      '算法发现差异 <b style="color:' + (gen.diff_count ? 'var(--danger)' : 'var(--ok)') + '">' +
      gen.diff_count + '</b> 处' +
      (missed ? '<span class="gen-warn">人工用例漏检</span>' : '') +
      '</div>';
  }

  function renderInspect(report) {
    var findings = (report.firstAttempt && report.firstAttempt.findings && report.firstAttempt.findings.length)
      ? report.firstAttempt.findings : (report.findings || []);
    var box = $('#inspectBox');
    if (!findings.length) {
      box.innerHTML = '<div class="empty" style="padding:22px">语义巡检无高风险命中 · 规则集：TX-001 / EX-007 / PREC-003 / CONC-005 / TZ-002</div>';
      return;
    }
    box.innerHTML = '<table class="tbl"><thead><tr><th style="width:82px">规则 ID</th><th>证据</th>' +
      '<th style="width:62px">风险</th><th style="width:190px">文件 / 行号</th><th style="width:60px">结果</th></tr></thead><tbody>' +
      findings.map(function (item) {
        return '<tr style="background:var(--danger-soft)"><td><code>' + esc(item.rule) + '</code></td>' +
          '<td>' + esc(item.evidence) + '</td><td class="risk-h">高</td>' +
          '<td><code>' + esc(item.file) + ':' + item.line + '</code></td>' +
          '<td><span class="pill" style="background:var(--danger-soft);color:var(--danger);border-color:#f0c7c5">命中</span></td></tr>';
      }).join('') + '</tbody></table>' +
      '<div class="note" style="border-radius:0 0 10px 10px">由 JDK Compiler Tree API 真实解析语法树得出；SQL-004 由双跑差分提供。</div>';
  }

  function renderPlan(status, plan) {
    var box = $('#planBox');
    var items = (plan.risk && plan.risk.items) || [];
    var approved = status.state.plan_approved;
    var planNode = (status.state.nodes && status.state.nodes['plan-migration']) || {};
    $('#planState').textContent = approved ? '已签核' :
      (planNode.status === 'waiting' || (status.state.nodes && status.state.nodes['migrate-module'] || {}).status === 'waiting' ? '待签核' : '待生成');
    if (!items.length) {
      box.innerHTML = '<div class="empty">运行 /plan-migration 后生成可审批计划与风险清单</div>';
      return;
    }
    var html = approved
      ? '<div class="plan-note" style="background:var(--ok-soft);border-color:#c9ead9;color:#0b7a55">' + ICON.ok +
        '<div>计划已人工签核通过，/migrate-module 已获得执行授权。</div></div>'
      : '<div class="plan-note">' + ICON.warn + '<div><b>人在回路关键决策点</b>：勾选条目否决后提交，可让编排层重规划；签核通过前不会生成代码。</div></div>';
    html += items.map(function (item) {
      if (item.vetoed) { vetoes.add(item.id); }
      var vetoed = vetoes.has(item.id);
      return '<div class="plan-item ' + (vetoed ? (item.adjusted ? 'adjusted' : 'vetoed') : '') + '">' +
        '<div class="plan-check"><input type="checkbox" data-veto="' + esc(item.id) + '" ' +
        (vetoed ? 'checked' : '') + ' ' + (approved ? 'disabled' : '') + ' title="否决此条并重规划"></div>' +
        '<div class="plan-body"><div class="plan-title">' + esc(item.adjusted || item.title) + '</div>' +
        '<div class="plan-meta"><span>' + esc(item.id) + '</span><span>风险 <b class="' +
        (item.risk === 'high' ? 'risk-h' : item.risk === 'medium' ? 'risk-m' : 'risk-l') + '">' +
        esc(item.risk) + '</b></span></div>' +
        (item.adjusted ? '<div class="fix">已按否决重规划：执行顺序后移，并补充双跑差分与人工签核项。</div>' : '') +
        '</div></div>';
    }).join('');
    html += '<div class="plan-actions"><button class="btn btn-sm" id="vetoBtn" ' + (approved ? 'disabled' : '') +
      '>提交否决 · 重规划</button><button class="btn btn-sm btn-primary" id="approveBtn" ' + (approved ? 'disabled' : '') +
      '>' + (approved ? '已签核通过' : '签核通过 · 继续生成') + '</button>' +
      '<span class="hint">' + items.length + ' 条计划 · ' +
      items.filter(function (item) { return item.risk === 'high'; }).length + ' 条高风险</span></div>';
    box.innerHTML = html;
  }

  function renderLogs(logs) {
    var box = $('#logBox');
    box.innerHTML = (logs.lines || []).map(function (line) {
      return '<div class="log-line lv-' + esc(line.level) + '"><span class="log-t">' + esc(line.time) +
        '</span><span class="log-l">[' + esc(line.level) + ']</span><span class="log-msg">' +
        esc(line.message) + '</span></div>';
    }).join('');
    box.scrollTop = box.scrollHeight;
    $('#logCount').textContent = (logs.count || 0) + ' 条';
  }

  function renderArtifacts(data) {
    var files = data.files || [];
    $('#artCount').textContent = files.length + ' 个文件';
    $('#artBox').innerHTML = files.length
      ? files.map(function (file) {
          return '<div class="art-row"><span class="art-ico">' + ICON.file + '</span>' +
            '<span class="art-name" title="' + esc(file.path) + '">' + esc(file.path) + '</span>' +
            '<span class="art-size">' + file.bytes + ' B</span></div>';
        }).join('')
      : '<div class="empty">工作流运行后产物逐条落盘</div>';
  }

  function renderEvidence(report, eff) {
    var files = [
      ['首次拦截快照', 'equiv/gate-first-attempt.json', !!report.firstAttempt],
      ['首个差分快照', 'equiv/dual-run-diff-first-attempt.json', !!report.diffFirst],
      ['拦截案例', 'equiv/interception-case.md', !!report.interception],
      ['自动补丁', 'patch/tx-annotation-fix.diff', !!report.patch],
      ['复检报告', 'equiv/recheck-report.json', !!report.recheck],
      ['实测指标', 'report/efficiency-metrics.json', !!report.metrics],
      ['A/B 报告', 'w1/ab-report.json', !!(eff && eff.ab)],
      ['实验协议', 'w1/protocol.md', !!(eff && eff.protocol)]
    ];
    $('#sigBox').innerHTML = files.map(function (item) {
      var available = item[2];
      return '<div class="sig"><div class="sig-body"><div class="sig-title">' + item[0] + '</div>' +
        '<div class="sig-meta">' + item[1] + '</div>' +
        (available ? '<div class="sig-btns"><a class="btn btn-sm" style="text-decoration:none" target="_blank" ' +
          'href="/api/file?path=' + encodeURIComponent(item[1]) + '">查看</a></div>' : '') +
        '</div><span class="sig-state ' + (available ? 'st-ok' : 'st-wait') + '">' +
        (available ? '已生成' : '待运行') + '</span></div>';
    }).join('');
  }

  function renderFooter(status) {
    $('#footCheckpoint').textContent = status.state.checkpoint || '未生成';
    var fallback = status.fallbackReason || '';
    $('#footToken').textContent = (status.generator || 'rule-based-template-v1') + (fallback ? '（回退）' : '');
    $('#footToken').title = fallback || '按 artifacts/config.json 选择生成适配层';
    $('#footAI').textContent = status.aiGenerated ? '是 · 模型生成' : '否 · 规则驱动';
    $('#footRework').textContent = status.state.rework || 0;
    if (!configPending) {
      var mode = status.config.defect_mode || (status.config.inject_defect ? 'structural' : 'off');
      $('#defectModeSel').value = mode;
    }
    if (!adapterPending && $('#modelSel')) {
      $('#modelSel').value = (status.config.model_adapter === 'llm-api-v1') ? 'llm' : 'rule';
    }
  }

  function renderHero(status, report) {
    var state = status.state;
    var migrate = (state.nodes && state.nodes['migrate-module']) || {};
    var first = report.firstAttempt;
    var graph = (report.delta && report.delta.workflow) || {};
    var phase, title, desc, action = '', hint = '';

    if (status.busy) {
      phase = '执行中'; title = '正在真实执行工作流';
      desc = 'javac 编译、契约测试、双跑差分与 AST 语义巡检依次运行，产物逐条落盘。';
    } else if (migrate.status === 'waiting') {
      phase = '等待人工决策'; title = '迁移计划待签核';
      desc = '未签核不会生成代码。可在计划卡片否决条目触发重规划，再签核继续。';
      action = '签核通过 · 继续生成'; hint = '继续生成、编译与等价性验证';
    } else if (state.gate === 'fail') {
      phase = '闸门阻断'; title = '行为等价性不通过';
      desc = '已自动出补丁并回到生成节点，等待复检。';
    } else if (state.gate === 'pass' && first && first.verdict === 'fail') {
      phase = '链路已完成'; title = '缺陷被拦截，闭环修复完成';
      desc = '首轮 ' + first.diff.diff_count + ' 处差分 · ' + (state.rework || 0) +
        ' 次返工 · 触发工作流回边后复检通过。';
      action = '重新运行全链路'; hint = '可切换「注入缺陷」对比';
    } else if (state.gate === 'pass') {
      phase = '链路已完成'; title = '行为等价性通过';
      desc = '五项检查全绿 · 节点产物已落盘，可按节点回放。';
      action = '重新运行全链路'; hint = '切换「注入缺陷」可看拦截闭环';
    } else {
      phase = '就绪'; title = '准备执行真实链路';
      desc = 'DAG 将真实编译 Java 工程、生成目标源码并执行等价性闸门。';
      action = '运行全链路'; hint = '9 节点 · 3 并发 · 产物逐条落盘';
    }

    $('#heroPhase').textContent = phase;
    $('#heroTitle').textContent = title;
    $('#heroDesc').textContent = desc;
    var button = $('#heroActionBtn');
    button.style.display = action ? '' : 'none';
    button.textContent = action || '';
    $('#heroActionHint').textContent = hint;

    var ms = graph.critical_ms || 0;
    $('#heroMs').textContent = ms ? (ms / 1000).toFixed(1) : '—';
    $('#heroMetricFoot').textContent = ms
      ? (graph.nodes || 9) + ' 节点 · ' + (graph.concurrency || 3) + ' 并发 · 关键路径'
      : '9 节点 · 3 并发 · 含真编译';
  }

  /* ---------------- 效率证明 ---------------- */

  function fmtMetric(metric, value) {
    if (value === null || value === undefined) { return '—'; }
    if (metric.key === 'deliver_ms') { return Number(value).toFixed(2); }
    if (metric.key === 'first_pass') { return Math.round(Number(value) * 100) + '%'; }
    return String(Math.round(Number(value) * 100) / 100);
  }

  function fmtDelta(metric) {
    if (metric.verdict !== 'measured') { return '—'; }
    if (metric.key === 'first_pass') {
      return Math.round(metric.arm_a * 100) + '% → ' + Math.round(metric.arm_b * 100) + '%';
    }
    if (metric.delta_pct === null || metric.delta_pct === undefined) { return '—'; }
    var good = metric.delta_pct >= 0;
    var verb = metric.key === 'deliver_ms' ? (good ? '缩短 ' : '增加 ') : (good ? '减少 ' : '增加 ');
    return verb + Math.abs(metric.delta_pct) + '%';
  }

  function metricRow(metric, armLabel) {
    var verdict = metric.verdict === 'measured'
      ? '<span class="pill pill-ok" style="font-size:10.5px">已配对</span>'
      : metric.verdict === 'invalid-pairing'
        ? '<span class="pill" style="font-size:10.5px;background:var(--danger-soft);color:var(--danger);border-color:#f0c7c5">不可配对</span>'
        : '<span class="pill pill-warn" style="font-size:10.5px">待实测</span>';
    return '<tr><td>' + esc(metric.label) + '<div style="font-size:10.5px;color:var(--faint)">' +
      esc(metric.note || '') + '</div></td>' +
      '<td class="mono">' + esc(fmtMetric(metric, metric.arm_a)) + '</td>' +
      '<td class="mono">' + esc(fmtMetric(metric, metric.arm_b)) + '</td>' +
      '<td class="mono">' + esc(fmtDelta(metric)) + '</td>' +
      '<td>' + verdict + '</td></tr>';
  }

  function renderProof(status, eff) {
    var ab = (eff && eff.ab) || null;
    var bench = (eff && eff.bench) || {};
    var armA = (ab && ab.arm_a) || null;
    var armB = (ab && ab.arm_b) || (eff && eff.platform) || null;
    var ready = !!(armA && armA.ready);
    var kind = (ab && ab.arm_a_kind) || 'none';
    var armLabel = kind === 'human' ? 'A 人工臂' : kind === 'baseline' ? 'A 基线臂' : 'A 臂';

    var head;
    if (ab && ab.pairing_error) {
      head = '<div class="proof-head bad"><div class="ph-ico">' + ICON.err + '</div>' +
        '<div class="ph-main"><div class="ph-t">两臂不同源，本轮数字不可配对</div>' +
        '<div class="ph-d">' + esc(ab.pairing_error) + '</div></div>' +
        '<div class="ph-num" style="color:var(--danger)">无效</div></div>';
    } else if (ready && ab.headline && ab.headline.available) {
      var delta = null;
      (ab.metrics || []).forEach(function (m) { if (m.key === 'deliver_ms') { delta = m.delta_pct; } });
      var title = kind === 'human'
        ? '两臂已实测：人工实现 vs 平台链路'
        : '两臂已实测：同一模型、同样两次机会，只差编排';
      head = '<div class="proof-head ok"><div class="ph-ico">' + ICON.ok + '</div>' +
        '<div class="ph-main"><div class="ph-t">' + esc(title) + '</div>' +
        '<div class="ph-d">' + esc(ab.headline.text) + ' · ' + esc(ab.acceptance) + '</div></div>' +
        '<div class="ph-num">' + (delta === null ? '—' : Math.abs(delta) + '%') + '</div></div>';
    } else {
      var min = armB ? (armB.deliver_ms / 60000) : 0;
      head = '<div class="proof-head"><div class="ph-ico">' + ICON.warn + '</div>' +
        '<div class="ph-main"><div class="ph-t">A 臂还没实测，差值暂不宣称</div>' +
        '<div class="ph-d">平台臂已实测：交付 ' + (min ? min.toFixed(2) : '—') + ' min · ' +
        (armB ? armB.manual_actions : '—') + ' 次人工动作 · ' +
        (armB ? armB.artifacts : '—') + ' 份可审计产物</div></div>' +
        '<div class="ph-num" style="color:var(--warn)">待测</div></div>';
    }

    var table = '';
    if (ab && ab.metrics) {
      table = '<table class="tbl"><thead><tr><th>指标</th><th style="width:92px">' + esc(armLabel) +
        '</th><th style="width:92px">B 平台臂</th><th style="width:96px">差值</th><th style="width:74px">状态</th>' +
        '</tr></thead><tbody>' + ab.metrics.map(function (m) { return metricRow(m, armLabel); }).join('') +
        '</tbody></table>';
    }

    var benchHtml = '';
    if (bench.on || bench.off) {
      benchHtml = ['off', 'on'].map(function (key) {
        var item = bench[key];
        if (!item) { return ''; }
        return '<div class="bench-box"><div class="bb-h"><span>' +
          (key === 'on' ? '注入缺陷 · ' : '无缺陷 · ') + item.runs + ' 次连跑</span>' +
          '<span>' + (item.defect ? '闸门应拦截' : '应一次通过') + '</span></div>' +
          '<div class="bb-v">' + (item.critical_ms.mean / 1000).toFixed(2) + '<small>s ± ' +
          (item.critical_ms.stdev / 1000).toFixed(2) + '</small></div>' +
          '<div class="bb-f">' + esc(item.adapter || '—') +
          (item.ai_generated ? ' · AI 生成' : '') +
          ' · 一次通过率 ' + Math.round(item.first_pass_rate * 100) + '% · 拦截率 ' +
          Math.round(item.interception_rate * 100) + '% · 返工均值 ' + item.rework_mean + '</div></div>';
      }).join('');
      if (bench.off && bench.on && bench.off.adapter !== bench.on.adapter) {
        benchHtml += '<div class="bench-note">注入缺陷固定使用规则夹具以保证可复现，该批次不含模型调用；两组耗时不可直接横比。</div>';
      }
    } else {
      benchHtml = '<div class="bench-box" style="grid-column:1/-1"><div class="bb-h"><span>基准测试</span></div>' +
        '<div class="bb-v" style="font-size:14px;color:var(--muted)">尚未运行</div>' +
        '<div class="bb-f">点击「跑基准 · 3 次」得到耗时分布与一次通过率</div></div>';
    }

    // 随包会带上一次实测的 A/B 报告，标明测量时间，避免被当成"刚刚跑出来的"
    var measuredAt = (ab && ab.arm_b && ab.arm_b.started_at) ? String(ab.arm_b.started_at).slice(5, 16).replace('T', ' ') : '';
    $('#proofBadge').textContent = (ab && ab.pairing_error)
      ? '配对无效 · 两臂适配器不同'
      : ready
        ? (kind === 'human' ? '两臂已实测 · 含人工臂' : '两臂已实测 · 基线 vs 平台') +
          (measuredAt ? ' · 测于 ' + measuredAt : '')
        : '平台侧已实测 · A 臂待测';
    $('#proofBadge').title = measuredAt
      ? '本表数据来自 ' + measuredAt + ' 的一次运行；重跑链路会刷新为本次结果'
      : '';
    $('#proofHead').innerHTML = head;
    $('#proofTable').innerHTML = table;
    $('#proofBench').innerHTML = benchHtml;
    $('#proofHint').textContent = status.benchRunning ? '基准运行中…' : '基准会重置并连跑链路，约 20 秒';
    $('#benchBtn').disabled = status.benchRunning || status.busy;
    $('#benchBtn').textContent = status.benchRunning ? '基准运行中…' : '跑基准 · 3 次';

    if (ab && ab.pairing_error) {
      $('#proofNote').innerHTML =
        '<b>为什么不算：</b>基线臂是用模型跑的，平台臂这一轮用的是规则适配器。' +
        '换掉生成器再比，比出来的差值衡量的是「两个不同生成器」，而不是「有没有编排」。' +
        '处理办法：把右上角生成适配层切回 <code>llm-api-v1</code> 后重跑链路，或重跑 <code>efficiency.py baseline</code>。';
      proofExport = { exported_at: new Date().toISOString(), ab: ab, bench: bench };
      return;
    }

    var aNote = kind === 'human'
      ? 'A 臂为真人打点记录（<code>efficiency.py human-start</code>），可对外宣称人效。'
      : kind === 'baseline'
        ? 'A 臂为<b>基线实验</b>：同一模型、同样最多两次机会，但只给 javac 报错、不给接口面与语义闸门。'
        : 'A 臂未测：跑 <code>efficiency.py baseline</code> 或 <code>human-start</code>。';
    $('#proofNote').innerHTML =
      '<b>两臂口径：</b>' + aNote +
      'B 臂拿到的是代码图谱抽出的真实接口面 + 硬性约束 + 五项闸门，失败走回边。' +
      (armB && armB.approval_gap_ms
        ? '<br><b>人工耗时单独披露：</b>B 臂本轮签核等待 ' + (armB.approval_gap_ms / 1000).toFixed(1) +
          ' s' + (armB.approval_mode === 'auto' ? '（自动跑批，真人复核会更久）' : '') +
          '，未计入上表的机器耗时对比。'
        : '') +
      '<br><b>不混着说：</b>基线臂证明的是「编排带来的提升」，不是「人效提升」；' +
      '要宣称人效，仍需真人臂记录，<code>human-stop</code> 之后本表会自动切到人工臂。' +
      '文档基线（80 人时 → 6.5 人时）始终 <b>measured=false</b>，只作参照。';

    proofExport = ab ? { exported_at: new Date().toISOString(), ab: ab, bench: bench } : null;
  }

  /* ---------------- refresh ---------------- */

  function refresh() {
    return Promise.all([
      getJSON('/api/status'), getJSON('/api/logs'), getJSON('/api/artifacts'),
      getJSON('/api/report'), getJSON('/api/plan'), getJSON('/api/workflow'),
      getJSON('/api/efficiency')
    ]).then(function (values) {
      var status = values[0], logs = values[1], artifacts = values[2], report = values[3],
        plan = values[4], wf = values[5], eff = values[6];
      offlineNotified = false;
      status.artifactCount = artifacts.files.length;
      var lastLog = logs.lines && logs.lines.length ? logs.lines[logs.lines.length - 1].message : '';
      var signature = JSON.stringify([
        status.state.steps, status.busy, status.state.gate, status.state.rework,
        logs.count, lastLog, artifacts.files.length, status.state.plan_approved,
        status.config.inject_defect, status.config.model_adapter, status.lastCommand,
        report.gate && report.gate.checked_at,
        report.firstAttempt && report.firstAttempt.checked_at,
        report.delta && report.delta.finished_at,
        plan.risk && plan.risk.replanned,
        status.benchRunning,
        eff.bench && (eff.bench.on || {}).generated_at,
        eff.ab && eff.ab.generated_at,
        wf.graph && (wf.graph.critical_ms || 0)
      ]);
      if (signature !== lastSignature) {
        lastSignature = signature;
        renderHero(status, report);
        renderDag(status, wf);
        renderProof(status, eff);
        renderSkills(status);
        renderControls(status);
        renderKPI(status);
        renderDetect(report, eff);
        renderGate(status, report);
        renderDiff(report);
        renderInspect(report);
        renderPlan(status, plan);
        renderLogs(logs);
        renderArtifacts(artifacts);
        renderEvidence(report, eff);
        renderFooter(status);
        modalCache = { spec: wf.specText, protocol: eff.protocol };
      }
    }).catch(function (error) {
      if (!offlineNotified) {
        offlineNotified = true;
        toast('warn', '后端未连接', '请启动 node hub/backend/server.js 后打开 http://127.0.0.1:8787');
        $('#verdictTxt').textContent = '后端未连接 · ' + error.message;
      }
    });
  }

  /* ---------------- events ---------------- */

  function command(url, payload, successTitle, successDesc) {
    return post(url, payload).then(function (result) {
      if (result.started === false) {
        toast('warn', '命令未执行', result.message || '另一条命令正在运行');
      } else {
        toast('info', successTitle, successDesc);
      }
      setTimeout(refresh, 300);
    }).catch(function (error) { toast('err', '请求失败', error.message); });
  }

  $('#heroActionBtn').addEventListener('click', function () {
    var label = $('#heroActionBtn').textContent;
    if (label.indexOf('签核') >= 0) {
      command('/api/approve', {}, '计划已签核', '链路继续：真实生成、编译与等价性验证');
    } else {
      command('/api/run', { restart: true }, '全链路已启动', '9 节点 DAG 真实执行，产物逐条落盘');
    }
  });

  $('#exportChainBtn').addEventListener('click', function () {
    if (!chainCache) { toast('warn', '暂无链路数据', '请先运行一次全链路'); return; }
    var blob = new Blob([JSON.stringify(chainCache, null, 2)], { type: 'application/json' });
    var url = URL.createObjectURL(blob);
    var link = document.createElement('a');
    link.href = url;
    link.download = 'workflow-trace.json';
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
    toast('ok', '轨迹已导出', 'workflow-trace.json 含节点耗时、依赖边与闸门结果');
  });

  $('#viewSpecBtn').addEventListener('click', function () {
    openModal('hub/workflow.yaml · 编排定义（改这里即可调整拓扑）',
      modalCache.spec || '尚未读取到工作流定义');
  });

  $('#protocolBtn').addEventListener('click', function () {
    openModal('W1 人工对照实验协议', modalCache.protocol || '协议未生成，运行 python engine/efficiency.py protocol');
  });

  $('#benchBtn').addEventListener('click', function () {
    command('/api/bench', { runs: 3, defect: true }, '基准测试已启动',
      '连跑 3 次并注入缺陷，约 20 秒后出分布');
  });

  $('#exportProofBtn').addEventListener('click', function () {
    if (!proofExport) { toast('warn', '暂无 A/B 数据', '先运行链路或基准测试'); return; }
    var blob = new Blob([JSON.stringify(proofExport, null, 2)], { type: 'application/json' });
    var url = URL.createObjectURL(blob);
    var link = document.createElement('a');
    link.href = url;
    link.download = 'efficiency-ab.json';
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
    toast('ok', '已导出', 'efficiency-ab.json 含两臂明细、配对结果与判读规则');
  });

  $('#modalClose').addEventListener('click', function () { $('#modal').hidden = true; });
  $('#modal').addEventListener('click', function (event) {
    if (event.target.id === 'modal') { $('#modal').hidden = true; }
  });
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') { $('#modal').hidden = true; }
  });

  $('#modelSel').addEventListener('change', function (event) {
    adapterPending = true;
    var value = event.target.value;
    command('/api/adapter', { adapter: value }, '生成适配层已切换',
      value === 'llm' ? '若密钥未配置，运行时会回退规则实现并在产物里写明原因'
        : '恢复确定性规则变换')
      .then(function () { setTimeout(function () { adapterPending = false; refresh(); }, 150); });
  });

  $('#runBtn').addEventListener('click', function () { command('/api/run', { restart: true }, '全链路已启动', '9 节点 DAG 真实执行'); });
  $('#topRunBtn').addEventListener('click', function () { command('/api/run', { restart: true }, '全链路已启动', '9 节点 DAG 真实执行'); });
  $('#resumeBtn').addEventListener('click', function () { command('/api/run', { restart: false }, '从断点续跑', '复用已落盘节点产物，从第一个未完成节点继续'); });
  $('#resetBtn').addEventListener('click', function () { vetoes.clear(); command('/api/reset', {}, '会话已重置', '产物与日志已清空，工程上下文保留'); });
  $('#resetBtn2').addEventListener('click', function () { vetoes.clear(); command('/api/reset', {}, '会话已重置', '产物与日志已清空，工程上下文保留'); });

  $('#defectModeSel').addEventListener('change', function (event) {
    configPending = true;
    var mode = event.target.value;
    var notes = {
      off: '恢复完整实现，闸门应首轮全绿',
      structural: '丢掉 @TxRequired 与 begin/commit：编译与单测都不报错，只有闸门能拦',
      semantic: '金额先舍入再判零：人工 5 条用例全过，算法用例能发现'
    };
    command('/api/config', { defect_mode: mode }, '故障注入已更新', notes[mode] || '')
      .then(function () {
        setTimeout(function () { configPending = false; refresh(); }, 120);
      });
  });

  $('#planBox').addEventListener('change', function (event) {
    var id = event.target.getAttribute('data-veto');
    if (!id) { return; }
    if (event.target.checked) { vetoes.add(id); } else { vetoes.delete(id); }
  });

  $('#planBox').addEventListener('click', function (event) {
    var button = event.target.closest('button');
    if (!button) { return; }
    if (button.id === 'vetoBtn') {
      if (!vetoes.size) { toast('warn', '请先勾选条目', '勾选任意计划条目以触发重规划'); return; }
      command('/api/veto', { ids: Array.from(vetoes) }, '已提交否决', '编排层将重规划并更新风险清单');
    } else if (button.id === 'approveBtn') {
      command('/api/approve', {}, '计划已签核', '链路继续：真实生成、编译与等价性验证');
    }
  });

  $('#skillList').addEventListener('click', function (event) {
    var item = event.target.closest('.skill');
    if (!item) { return; }
    toast('info', '单节点重跑', '请通过「从断点续跑」按真实产物状态继续，不跳过前置节点');
  });

  $('#assetTabs').addEventListener('click', function (event) {
    var tab = event.target.closest('.tab');
    if (!tab) { return; }
    $$('.tab').forEach(function (element) {
      element.classList.remove('active'); element.setAttribute('aria-selected', 'false');
    });
    $$('.pane').forEach(function (element) { element.classList.remove('active'); });
    tab.classList.add('active');
    tab.setAttribute('aria-selected', 'true');
    $('#' + tab.getAttribute('data-pane')).classList.add('active');
  });

  window.addEventListener('hashchange', function () {
    var steps = (chainCache && chainCache.nodes) || [];
    if (steps.length) { setTimeout(function () { drawEdges(steps); }, 60); }
  });

  window.addEventListener('resize', function () {
    var steps = (chainCache && chainCache.nodes) || [];
    if (steps.length) { drawEdges(steps); }
  });

  watchDagSize();
  refresh();
  setInterval(refresh, 900);
})();
