/**
 * 插件快照逻辑的离线验证（不需要真的起 dsh）。
 *
 * 盯的是这次修掉的两个坑：
 *   1. agent/disposed 不能把「在跑」抹掉 —— 桌面端正常退出时每个 live agent 都会
 *      disposed，如果这里不留 interrupted，下次启动的候选就永远是空的；
 *   2. 标题要从投影缓存读（header 里通常没有），否则快照里 title 全是空串，
 *      侧栏只能拿工作区名兜底。
 *
 * 用假的 ctx 触发事件，然后直接读快照 JSON 断言。
 * 运行：node tests/plugin_snapshot_test.mjs
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const pluginPath = path.join(here, '..', 'modules', 'payload', 'dsh-resume-sessions', 'index.js');

const home = fs.mkdtempSync(path.join(os.tmpdir(), 'zf-plugin-sim-'));
process.env.DSH_HOME = home;

const sessionId = 'session-sim-run';
const projectDir = path.join(home, 'storages', 'session_projcache', 'sessions');
fs.mkdirSync(projectDir, { recursive: true });
fs.writeFileSync(path.join(projectDir, `${sessionId}.json`), JSON.stringify({
  record: {
    rows: {
      title: { val: '真实标题：别用工作区名' },
      turnBoundary: { val: { openTurnStartSeq: 59 } },
      sessionListMetadata: { val: { blank: false } },
    },
  },
}), 'utf8');

const PASS = [];
const FAIL = [];
const check = (name, cond, extra = '') => {
  (cond ? PASS : FAIL).push(name);
  console.log(`  ${cond ? 'OK  ' : 'FAIL'} ${name}${cond || !extra ? '' : `  [${extra}]`}`);
};

// ── 假 ctx ─────────────────────────────────────────────────────────────────
const handlers = new Map();
const ctx = {
  on(event, fn) {
    if (!handlers.has(event)) handlers.set(event, []);
    handlers.get(event).push(fn);
    return () => {};
  },
  get() { return undefined; },
  inject() { /* webServer 不在：跳过 HTTP 注册 */ },
  effect(fn) {
    const dispose = fn();
    return () => { if (typeof dispose === 'function') dispose(); };
  },
};
const emit = (event, payload) => {
  for (const fn of handlers.get(event) ?? []) fn(payload);
};

const { apply } = await import(`file://${pluginPath.replace(/\\/g, '/')}`);
apply(ctx, { enabled: true, startupDelayMs: 9_999_999, verbose: false, maxSessions: 2 });

const stateFile = path.join(home, 'resume-sessions.json');
const readState = () => JSON.parse(fs.readFileSync(stateFile, 'utf8'));

const agent = { id: sessionId, session: { id: sessionId, header: { cwd: 'C:/ws' } } };

console.log('A. running 时落盘');
emit('agent/status', { agent, status: 'running' });
let st = readState().sessions[sessionId];
check('status=running', st.status === 'running', JSON.stringify(st));
check('running 布尔为 true', st.running === true, JSON.stringify(st));
check('从投影读到真实标题', st.title === '真实标题：别用工作区名', st.title);

console.log('B. disposed 不能抹掉「在跑」');
emit('agent/disposed', { agent });
st = readState().sessions[sessionId];
check('marker: interrupted=true', st.interrupted === true, JSON.stringify(st));
check('running 归零', st.running === false, JSON.stringify(st));
check('status 记为 disposed', st.status === 'disposed', st.status);

console.log('C. 从来没跑过的会话：关掉不算被打断');
const idleId = 'session-sim-idle';
const idleAgent = { id: idleId, session: { id: idleId, header: { cwd: 'C:/ws' } } };
emit('agent/status', { agent: idleAgent, status: 'idle' });   // 只有 idle，没有 running
emit('agent/disposed', { agent: idleAgent });
const idleEntry = readState().sessions[idleId];
check('没跑过：interrupted 不为 true', idleEntry.interrupted !== true, JSON.stringify(idleEntry));

console.log('D. 退出流程先收尾 turn 再 dispose（真实丢恢复的场景）');
const warmId = 'session-sim-warm';
const warmAgent = { id: warmId, session: { id: warmId, header: { cwd: 'C:/ws' } } };
emit('agent/status', { agent: warmAgent, status: 'running' });
emit('agent/status', { agent: warmAgent, status: 'idle' });   // 退出时 turn 被收尾
emit('agent/disposed', { agent: warmAgent });                  // 然后才 dispose
const warm = readState().sessions[warmId];
check('刚跑过（窗口内）仍记 interrupted', warm.interrupted === true, JSON.stringify(warm));
check('runningAt 保留（用于窗口判定）', typeof warm.runningAt === 'number' && warm.runningAt > 0, warm.runningAt);
check('有状态轨迹可回溯', Array.isArray(warm.history) && warm.history.length >= 2,
  JSON.stringify(warm.history));

fs.rmSync(home, { recursive: true, force: true });
console.log(`\n结果：${PASS.length} 通过 / ${FAIL.length} 失败`);
process.exit(FAIL.length === 0 ? 0 : 1);