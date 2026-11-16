/**
 * 会话续跑候选语义的回归测试（跑在 node 上）。
 *
 * 测的是**插件源码里的真函数**，不是抄一遍的副本：
 * 从 index.js 里按名字把 lastTurnEnd / closedWhileRunning / pick 三个函数抠出来，
 * 放进一个注入了桩（cfg / state / guard / archivedIds / defaultWorkspacePath / samePath / log）
 * 的沙箱里执行。插件改了，这些断言跟着变；复制粘贴的实现改了，它测不出来。
 *
 * 跑法：node tests/test_resume_semantics.mjs
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
// ZF_PLUGIN_PATH 可以指向任意一份插件源码：回归时用它对着**旧版本**跑一遍，
// 确认这些断言真的能抓住旧缺陷（而不是"新代码自己给自己发奖状"）。
const pluginPath = process.env.ZF_PLUGIN_PATH
  ? path.resolve(process.env.ZF_PLUGIN_PATH)
  : path.resolve(here, '..', 'modules', 'payload', 'dsh-resume-sessions', 'index.js');
const src = fs.readFileSync(pluginPath, 'utf8');
console.log(`插件源码：${pluginPath}`);

const PASS = [];
const FAIL = [];

function check(name, cond, extra = '') {
  (cond ? PASS : FAIL).push(name);
  console.log(`  ${cond ? 'OK  ' : 'FAIL'} ${name}${cond || !extra ? '' : `  [${extra}]`}`);
}

/** 从源码里抠出一个函数声明（按大括号配平）。 */
function extractFunction(name) {
  const marker = `function ${name}(`;
  const idx = src.indexOf(marker);
  if (idx < 0) throw new Error(`函数没找到：${name}`);
  const braceStart = src.indexOf('{', idx);
  let depth = 0;
  let i = braceStart;
  for (; i < src.length; i += 1) {
    const ch = src[i];
    if (ch === '{') depth += 1;
    else if (ch === '}') {
      depth -= 1;
      if (depth === 0) { i += 1; break; }
    }
  }
  return src.slice(idx, i);
}

const cfg = {
  workspacePath: '',
  maxAgeHours: 48,
  maxSessions: 2,
  runningWindowMin: 15,
  includeGoalActive: true,
  verbose: true,
};

const samePath = (a, b) => {
  if (typeof a !== 'string' || a === '' || typeof b !== 'string' || b === '') return false;
  const norm = (p) => String(p).replace(/[\\/]+$/, '').toLowerCase();
  return norm(a) === norm(b);
};

const factory = new Function(
  'cfg', 'state', 'archivedIds', 'defaultWorkspacePath', 'guard', 'samePath', 'log',
  [
    extractFunction('lastTurnEnd'),
    extractFunction('closedWhileRunning'),
    extractFunction('pick'),
    'return { lastTurnEnd, closedWhileRunning, pick };',
  ].join('\n'),
);
const api = factory(cfg, {}, () => new Set(), () => '/ws', { crashes: 0 }, samePath, () => {});

const MIN = 60_000;
const HOUR = 60 * MIN;
const now = 1_800_000_000_000;

// ── 1. lastTurnEnd ─────────────────────────────────────────────────────────
console.log('1. lastTurnEnd（锚点取 turn 收尾，不是关闭时刻）');
check('history 为空 → 0', api.lastTurnEnd({}) === 0);
check('只有 running → 0（turn 没收尾过）',
  api.lastTurnEnd({ history: [{ t: 100, s: 'running' }] }) === 0);
check('最后一个 idle → 它的 t',
  api.lastTurnEnd({ history: [{ t: 100, s: 'running' }, { t: 200, s: 'idle' }] }) === 200);
check('结尾是 disposed → 往前找 idle（不能拿 disposed 当锚点，判据会恒真）',
  api.lastTurnEnd({ history: [{ t: 100, s: 'running' }, { t: 200, s: 'idle' }, { t: 300, s: 'disposed' }] }) === 200);
check('结尾是 running → 往前找 idle',
  api.lastTurnEnd({ history: [{ t: 100, s: 'idle' }, { t: 200, s: 'running' }] }) === 100);

// ── 2. closedWhileRunning ──────────────────────────────────────────────────
console.log('2. closedWhileRunning（关闭那一刻它是不是刚跑过）');

// 陈旧 idle：跑完就 idle、之后再没被碰过，13 小时前的事。
const staleIdle = {
  status: 'idle', running: false, at: now - 13 * HOUR, runningAt: now - 13 * HOUR - 12_000,
  history: [{ t: now - 13 * HOUR - 12_000, s: 'running' }, { t: now - 13 * HOUR, s: 'idle' }],
};
check('陈旧 idle（从未 dispose）→ 不算候选', api.closedWhileRunning(staleIdle) === false);

// dispose 时还在跑。
const disposedRunning = {
  status: 'disposed', running: false, runningAt: now - 30 * 1000, disposedAt: now - 20 * 1000,
  history: [{ t: now - 30 * 1000, s: 'running' }],
};
check('dispose 时还在跑 → 算候选', api.closedWhileRunning(disposedRunning) === true);

// 退出流程：先收尾 turn，再 dispose（实测有会话在关闭前 27 秒还在跑）。
const closedWhileRunningTurn = {
  status: 'disposed', running: false, runningAt: now - 20 * MIN, disposedAt: now - 5 * 1000,
  history: [{ t: now - 20 * MIN, s: 'running' }, { t: now - 8 * 1000, s: 'idle' }],
};
check('长 turn 收尾后 8 秒被 dispose → 算候选（锚点取 turn 收尾，不是 turn 起点）',
  api.closedWhileRunning(closedWhileRunningTurn) === true);

// dispose 之前先收尾，且间隔超过窗口 → 不算。
const longGap = {
  status: 'disposed', running: false, runningAt: now - 5 * HOUR, disposedAt: now - 1 * HOUR,
  history: [{ t: now - 5 * HOUR, s: 'running' }, { t: now - 5 * HOUR + MIN, s: 'idle' }],
};
check('turn 收尾后过了 1 小时才 dispose → 超过窗口，不算候选',
  api.closedWhileRunning(longGap) === false);

// ── 3. pick()：陈旧 idle 不再挤掉真正被中断的会话 ──────────────────────────
console.log('3. pick()（陈旧 idle 不该占名额）');
const state = {
  sessions: {
    stale: {
      status: 'idle', running: false, at: now - 13 * HOUR, runningAt: now - 13 * HOUR - 12_000,
      cwd: '/ws', history: [{ t: now - 13 * HOUR - 12_000, s: 'running' }, { t: now - 13 * HOUR, s: 'idle' }],
    },
    interrupted: {
      status: 'disposed', running: false, interrupted: true, at: now - 2 * HOUR,
      runningAt: now - 2 * HOUR - 10_000, disposedAt: now - 2 * HOUR,
      cwd: '/ws', history: [{ t: now - 2 * HOUR - 10_000, s: 'running' }, { t: now - 2 * HOUR, s: 'idle' }],
    },
  },
};
const pickSandbox = new Function(
  'cfg', 'state', 'archivedIds', 'defaultWorkspacePath', 'guard', 'samePath', 'log',
  [
    extractFunction('lastTurnEnd'),
    extractFunction('closedWhileRunning'),
    extractFunction('pick'),
    'return pick;',
  ].join('\n'),
);
const pick = pickSandbox(cfg, state, () => new Set(), () => '/ws', { crashes: 0 }, samePath, () => {});
const picked = pick(false, undefined).map(([id]) => id);
check('陈旧 idle 不再进候选', !picked.includes('stale'), picked.join(','));
check('被中断的会话进了候选', picked.includes('interrupted'), picked.join(','));

// ── 4. pick()：名额与保质期 ────────────────────────────────────────────────
console.log('4. pick()（名额 / 保质期 / 归档）');
const state2 = {
  sessions: {
    a: { status: 'disposed', interrupted: true, at: now - MIN, runningAt: now - MIN, disposedAt: now - MIN, cwd: '/ws' },
    b: { status: 'disposed', interrupted: true, at: now - 2 * MIN, runningAt: now - 2 * MIN, disposedAt: now - 2 * MIN, cwd: '/ws' },
    c: { status: 'disposed', interrupted: true, at: now - 3 * MIN, runningAt: now - 3 * MIN, disposedAt: now - 3 * MIN, cwd: '/ws' },
    old: { status: 'disposed', interrupted: true, at: now - 100 * HOUR, runningAt: now - 100 * HOUR, disposedAt: now - 100 * HOUR, cwd: '/ws' },
  },
};
const pick2 = pickSandbox(cfg, state2, () => new Set(), () => '/ws', { crashes: 0 }, samePath, () => {});
const picked2 = pick2(false, undefined).map(([id]) => id);
check('超过 maxSessions 只取前 2 个', picked2.length === 2, picked2.join(','));
check('按 runningAt 倒序取最新的', picked2[0] === 'a' && picked2[1] === 'b', picked2.join(','));
check('超过 maxAgeHours 的老会话不进候选', !picked2.includes('old'), picked2.join(','));
const pickArchived = pickSandbox(cfg, state2, () => new Set(['a']), () => '/ws', { crashes: 0 }, samePath, () => {});
check('归档掉的会话不进候选', !pickArchived(false, undefined).map(([id]) => id).includes('a'));
const pickOtherWs = pickSandbox(cfg, state2, () => new Set(), () => '/other', { crashes: 0 }, samePath, () => {});
check('不在当前工作区的不进候选', pickOtherWs(false, undefined).length === 0);

// ── 5. lastRestored 必须每次覆盖 ──────────────────────────────────────────
console.log('5. lastRestored（不能只写第一次）');
const guardOnce = src.includes('if (!state.lastRestored)');
check('源码里不再有"只写第一次"的写法', guardOnce === false);

console.log(`\n结果：${PASS.length} 通过 / ${FAIL.length} 失败`);
if (FAIL.length) {
  for (const name of FAIL) console.log(`  - ${name}`);
  process.exit(1);
}
