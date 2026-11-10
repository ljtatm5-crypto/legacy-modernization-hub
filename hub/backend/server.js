/**
 * Legacy Modernization Hub · 后端 API（Node 原生 http，零依赖）。
 *
 * 启动：node hub/backend/server.js
 * 打开：http://127.0.0.1:8787
 *
 * 所有数据都来自 hub/artifacts 下的真实产物；写操作通过 CLI 真实执行。
 */
'use strict';

const http = require('http');
const fs = require('fs');
const path = require('path');
const { spawn, spawnSync } = require('child_process');

const HUB = path.resolve(__dirname, '..');
const ROOT = path.resolve(HUB, '..');
const ENGINE = path.join(HUB, 'engine');
const ARTIFACTS = path.join(HUB, 'artifacts');
const FRONTEND = path.join(ROOT, 'legacy-modernization-hub.html');
const PYTHON = process.env.PYTHON || 'python';
const PORT = Number(process.env.PORT || 8787);

let busy = false;
let benchRunning = false;
let lastCommand = '';
let lastExitCode = null;
let lastError = '';

function readJson(file, fallback) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch (_) {
    return fallback;
  }
}

function readText(file, fallback = '') {
  try {
    return fs.readFileSync(file, 'utf8');
  } catch (_) {
    return fallback;
  }
}

function artifact(relative) {
  return path.join(ARTIFACTS, relative);
}

function walk(dir, base = dir) {
  let result = [];
  if (!fs.existsSync(dir)) return result;
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.name.startsWith('.')) continue;   // .server.pid 这类运行态文件不算产物
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      result = result.concat(walk(full, base));
    } else {
      result.push({
        path: 'artifacts/' + path.relative(base, full).split(path.sep).join('/'),
        bytes: fs.statSync(full).size,
      });
    }
  }
  return result.sort((a, b) => a.path.localeCompare(b.path));
}

function send(res, code, payload, type = 'application/json; charset=utf-8') {
  res.writeHead(code, {
    'Content-Type': type,
    'Cache-Control': 'no-store',
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  });
  res.end(type.startsWith('application/json') ? JSON.stringify(payload) : payload);
}

function runCli(args) {
  if (busy) return false;
  busy = true;
  lastCommand = 'python cli.py ' + args.join(' ');
  lastError = '';
  const child = spawn(PYTHON, ['cli.py', ...args], {
    cwd: ENGINE,
    windowsHide: true,
  });
  let stderr = '';
  child.stdout.on('data', chunk => { /* CLI 自身已写 run.log */ });
  child.stderr.on('data', chunk => { stderr += chunk.toString(); });
  child.on('error', error => {
    lastError = String(error);
    busy = false;
  });
  child.on('close', code => {
    lastExitCode = code;
    if (code !== 0) lastError = stderr.slice(-2000);
    busy = false;
  });
  return true;
}

function statusPayload() {
  const state = readJson(artifact('state.json'), {
    steps: [], nodes: {}, gate: 'idle', gate_results: {}, rework: 0,
    plan_approved: false, finished: false, checkpoint: 'not-started',
  });
  const config = readJson(artifact('config.json'), { inject_defect: false });
  const metrics = readJson(artifact('report/efficiency-metrics.json'), {});
  const generation = readJson(artifact('generation.json'), {});
  return {
    busy, benchRunning, lastCommand, lastExitCode, lastError,
    state, config,
    measured: metrics.measured || null,
    baseline: metrics.baseline || null,
    generator: generation.adapter || state.generator || 'rule-based-template-v1',
    aiGenerated: !!generation.ai_generated,
    fallbackReason: generation.fallback_reason || '',
    serverTime: new Date().toISOString(),
  };
}

function workflowPayload() {
  const graph = readJson(artifact('workflow/graph.json'), null);
  const state = readJson(artifact('state.json'), {});
  return {
    graph,
    source: (state.workflow && state.workflow.source) || null,
    specPath: path.join(HUB, 'workflow.yaml'),
    specText: readText(path.join(HUB, 'workflow.yaml')),
    trace: (graph && graph.trace) || state.trace || [],
  };
}

function efficiencyPayload() {
  const ab = readJson(artifact('w1/ab-report.json'), null);
  const bench = readJson(artifact('w1/bench.json'), null);
  const human = readJson(artifact('w1/arm-human.json'), null);
  const platform = readJson(artifact('w1/arm-platform.json'), null);
  return {
    ab, bench, human, platform,
    protocol: readText(artifact('w1/protocol.md')),
    benchRunning,
  };
}

function reportPayload() {
  return {
    gate: readJson(artifact('equiv/gate-report.json'), null),
    firstAttempt: readJson(artifact('equiv/gate-first-attempt.json'), null),
    diffFirst: readJson(artifact('equiv/dual-run-diff-first-attempt.json'), null),
    diffFinal: readJson(artifact('equiv/dual-run-diff.json'), null),
    findings: readJson(artifact('equiv/semantic-findings.json'), []),
    recheck: readJson(artifact('equiv/recheck-report.json'), null),
    build: readJson(artifact('equiv/build.json'), null),
    coverage: readJson(artifact('test/coverage-report.json'), null),
    patch: readText(artifact('patch/tx-annotation-fix.diff')),
    interception: readText(artifact('equiv/interception-case.md')),
    generation: readJson(artifact('generation.json'), null),
    metrics: readJson(artifact('report/efficiency-metrics.json'), null),
    delta: readJson(artifact('report/delta-summary.json'), null),
  };
}

function logsPayload() {
  const raw = readText(artifact('run.log'));
  const lines = raw.split(/\r?\n/).filter(Boolean).slice(-240);
  return {
    count: lines.length,
    lines: lines.map(line => {
      const match = line.match(/^\[([^\]]+)\]\s+\[([^\]]+)\]\s+(.*)$/);
      return match
        ? { time: match[1], level: match[2], message: match[3] }
        : { time: '', level: 'INFO', message: line };
    }),
  };
}

function planPayload() {
  return {
    risk: readJson(artifact('plan/risk-register.json'), null),
    markdown: readText(artifact('plan/migration-plan.md')),
    rules: readJson(path.join(HUB, 'sample-project', 'rules', 'domain-settle.json'), null),
    mapping: readText(path.join(HUB, 'sample-project', 'mapping', 'legacy-to-modern.yaml')),
  };
}

function body(req) {
  return new Promise(resolve => {
    let data = '';
    req.on('data', chunk => { data += chunk; });
    req.on('end', () => {
      try { resolve(data ? JSON.parse(data) : {}); } catch (_) { resolve({}); }
    });
  });
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  const route = url.pathname;

  if (req.method === 'OPTIONS') return send(res, 204, '');

  if (req.method === 'GET') {
    switch (route) {
      case '/':
      case '/index.html':
        return send(res, 200, readText(FRONTEND, '<h1>frontend not found</h1>'), 'text/html; charset=utf-8');
      case '/app.js':
        return send(res, 200, readText(path.join(__dirname, 'app.js')), 'application/javascript; charset=utf-8');
      case '/api/health':
        return send(res, 200, { ok: true, service: 'legacy-modernization-hub', port: PORT });
      case '/api/status':
        return send(res, 200, statusPayload());
      case '/api/logs':
        return send(res, 200, logsPayload());
      case '/api/artifacts': {
        const files = walk(ARTIFACTS);
        return send(res, 200, { count: files.length, files });
      }
      case '/api/report':
        return send(res, 200, reportPayload());
      case '/api/plan':
        return send(res, 200, planPayload());
      case '/api/workflow':
        return send(res, 200, workflowPayload());
      case '/api/efficiency':
        return send(res, 200, efficiencyPayload());
      case '/api/file': {
        const requested = url.searchParams.get('path') || '';
        const root = path.resolve(ARTIFACTS);
        const resolved = path.resolve(root, requested.replace(/^artifacts\//, ''));
        // 必须带路径分隔符比较，否则 ../artifacts2/x 这类同级前缀也能绕过
        if (resolved !== root && !resolved.startsWith(root + path.sep)) {
          return send(res, 403, { error: 'forbidden' });
        }
        return send(res, 200, readText(resolved), 'text/plain; charset=utf-8');
      }
      case '/api/source': {
        const which = url.searchParams.get('which') === 'legacy' ? 'legacy' : 'modern';
        const name = which === 'legacy' ? 'LegacySettleService.java' : 'ModernSettleService.java';
        const file = path.join(HUB, 'sample-project', which, 'src', 'com', 'demo', 'settle', name);
        return send(res, 200, { which, file: name, content: readText(file) });
      }
      default:
        return send(res, 404, { error: 'not found: ' + route });
    }
  }

  if (req.method === 'POST') {
    const payload = await body(req);
    let args = null;
    switch (route) {
      case '/api/run': {
        if (benchRunning) return send(res, 409, { started: false, message: '基准测试正在运行，稍后再跑链路' });
        const state = readJson(artifact('state.json'), {});
        // 链路已完成时没有断点可续，直接从头重跑，避免空跑
        const resume = payload.restart === false && !state.finished;
        args = resume
          ? ['run', ...(Number.isInteger(payload.from) ? ['--from', String(payload.from)] : [])]
          : ['run', '--restart'];
        break;
      }
      case '/api/approve':
        args = ['approve'];
        break;
      case '/api/veto': {
        const ids = Array.isArray(payload.ids) ? payload.ids.join(',') : '';
        args = ids ? ['veto', '--ids', ids] : null;
        if (!args) return send(res, 400, { error: 'ids required' });
        break;
      }
      case '/api/reset':
        args = ['reset'];
        break;
      case '/api/bench': {
        if (benchRunning) return send(res, 409, { started: false, message: '基准测试正在运行' });
        if (busy) return send(res, 409, { started: false, message: '链路正在执行，稍后再跑基准' });
        const runs = Number.isInteger(payload.runs) ? Math.min(9, Math.max(1, payload.runs)) : 3;
        const defect = payload.defect === false ? 'off' : 'on';
        benchRunning = true;
        lastCommand = 'python efficiency.py bench --runs ' + runs + ' --defect ' + defect;
        lastError = '';
        const child = spawn(PYTHON, ['efficiency.py', 'bench', '--runs', String(runs), '--defect', defect], {
          cwd: ENGINE, windowsHide: true,
        });
        let stderr = '';
        child.stdout.on('data', () => {});
        child.stderr.on('data', chunk => { stderr += chunk.toString(); });
        child.on('error', error => { lastError = String(error); benchRunning = false; });
        child.on('close', code => {
          lastExitCode = code;
          if (code !== 0) { lastError = stderr.slice(-2000); benchRunning = false; return; }
          // 基准跑完立刻重算配对，避免界面上的 A/B 表落后于刚测出的数据
          const pairing = spawn(PYTHON, ['efficiency.py', 'ab'], { cwd: ENGINE, windowsHide: true });
          pairing.on('close', () => { benchRunning = false; });
          pairing.on('error', error => { lastError = String(error); benchRunning = false; });
        });
        return send(res, 202, { started: true, command: lastCommand, runs, defect });
      }
      case '/api/adapter': {
        const wanted = payload.adapter === 'llm' ? 'llm-api-v1' : 'rule-based-template-v1';
        const config = readJson(artifact('config.json'), {});
        config.model_adapter = wanted;
        fs.writeFileSync(artifact('config.json'), JSON.stringify(config, null, 2), 'utf8');
        const stamp = new Date().toTimeString().slice(0, 8);
        fs.appendFileSync(artifact('run.log'),
          '[' + stamp + '] [INFO] 生成适配层切换 → ' + wanted + '\n', 'utf8');
        return send(res, 200, { started: true, command: 'adapter (direct write)', config });
      }
      case '/api/config': {
        // 配置写入是轻量操作，直接落盘，不与长任务抢 busy 锁，
        // 也避免轮询在 CLI 启动前读到旧值把开关弹回。
        const config = readJson(artifact('config.json'), { model_adapter: 'rule-based-template-v1' });
        const mode = payload.defect_mode
          || (payload.inject_defect ? 'structural' : 'off');
        config.defect_mode = ['off', 'structural', 'semantic'].includes(mode) ? mode : 'off';
        config.inject_defect = config.defect_mode !== 'off';
        fs.writeFileSync(artifact('config.json'), JSON.stringify(config, null, 2), 'utf8');
        const state = readJson(artifact('state.json'), null);
        if (state) {
          state.inject_defect = config.inject_defect;
          state.fixed = false;
          fs.writeFileSync(artifact('state.json'), JSON.stringify(state, null, 2), 'utf8');
        }
        const stamp = new Date().toTimeString().slice(0, 8);
        const level = config.inject_defect ? 'WARN' : 'INFO';
        const notes = {
          off: '故障注入关闭',
          structural: '故障注入开启（结构型）：生成适配层将漏掉事务映射',
          semantic: '故障注入开启（语义型）：金额先舍入再做零值判断',
        };
        const note = notes[config.defect_mode] || notes.off;
        fs.appendFileSync(artifact('run.log'), `[${stamp}] [${level}] ${note}\n`, 'utf8');
        return send(res, 200, { started: true, command: 'config (direct write)', config });
      }
      default:
        return send(res, 404, { error: 'not found: ' + route });
    }
    const started = runCli(args);
    return send(res, started ? 202 : 409, {
      started, command: lastCommand,
      message: started ? 'command accepted' : 'another command is running',
    });
  }

  return send(res, 405, { error: 'method not allowed' });
});

/**
 * 首次启动（没有 state.json）时先跑一次 cli.py reset。
 * 否则 /api/status 只能返回空数组，新装用户打开会看到左栏一片空白。
 */
function bootstrapState() {
  if (fs.existsSync(path.join(ARTIFACTS, 'state.json'))) return;
  console.log('  首次启动：正在按 workflow.yaml 初始化工作流状态…');
  const result = spawnSync(PYTHON, ['cli.py', 'reset'], {
    cwd: ENGINE, encoding: 'utf8', windowsHide: true,
  });
  if (result.error) {
    console.error('[!] 初始化失败（Python 是否在 PATH 中？）：' + result.error.message);
  } else if (result.status !== 0) {
    console.error('[!] cli.py reset 退出码 ' + result.status + '：' + String(result.stderr || '').slice(-300));
  } else {
    const fresh = readJson(path.join(ARTIFACTS, 'state.json'), {});
    console.log('  初始化完成：' + ((fresh.steps || []).length) + ' 个节点已就绪');
  }
}

server.on('error', error => {
  if (error.code === 'EADDRINUSE') {
    console.error('[X] 端口 ' + PORT + ' 已被占用。');
    console.error('    可能已经启动过一个实例，先运行 stop.bat / stop.sh，或换个端口：');
    console.error('    set PORT=8788 && node hub\\backend\\server.js');
    process.exit(1);
  }
  console.error('[X] 启动失败：' + error.message);
  process.exit(1);
});

bootstrapState();

server.listen(PORT, '127.0.0.1', () => {
  // 记录 pid，供 stop 脚本精确停止，避免 taskkill 误杀其他 node 进程
  try {
    fs.mkdirSync(ARTIFACTS, { recursive: true });
    fs.writeFileSync(path.join(ARTIFACTS, '.server.pid'), String(process.pid), 'utf8');
  } catch (_) { /* pid 文件写不了不影响服务 */ }
  console.log('');
  console.log('  Legacy Modernization Hub 已启动');
  console.log('  打开:      http://127.0.0.1:' + PORT);
  console.log('  产物目录:  ' + ARTIFACTS);
  console.log('  Python:    ' + PYTHON);
  console.log('  停止:      stop.bat（或 Ctrl+C）');
  console.log('');

  const url = 'http://127.0.0.1:' + PORT;
  if (process.platform === 'win32' && !process.env.HUB_NO_OPEN) {
    spawn('cmd', ['/c', 'start', '', url], { detached: true, stdio: 'ignore', windowsHide: true }).unref();
  }
});

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => {
    try { fs.unlinkSync(path.join(ARTIFACTS, '.server.pid')); } catch (_) { /* ignore */ }
    process.exit(0);
  });
}
