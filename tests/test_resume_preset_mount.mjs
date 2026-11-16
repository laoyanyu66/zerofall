// 回归测试：dsh-resume-sessions 恢复会话时必须把会话自己的 agent preset 挂回去。
//
// 背景（2026-11-12 的真实故障）：插件早期版本调用 agents.resume({resumeSessionId})，
// 没有 setup，于是恢复出来的 agent 只有「宿主平面」那几行工具
// （working_directory / tiangong_* / purge_*）。预设平面里的 pwsh / read / write /
// glob / grep / edit / skill / schedule / subagent 全都不在目录里，模型把 pwsh 当
// "unknown tool"，在两个会话里连着几十轮报"这个会话没有 shell 工具"。
//
// 跑法：node --test tests/test_resume_preset_mount.mjs    （或直接 node 这个文件）

import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const PLUGIN =
  process.env.DSH_RESUME_PLUGIN
  || path.join(os.homedir(), '.dsh', 'profiles', 'desktop', 'vendor', 'dsh-resume-sessions', 'index.js');

if (!fs.existsSync(PLUGIN)) {
  console.error(`跳过：找不到插件 ${PLUGIN}`);
  process.exit(0);
}

const SESSION_ID = 'session-test-preset';

/** 一个有工作区、有投影目录、有一个"关闭前在跑"会话的临时 DSH_HOME。 */
function makeHome() {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'zf_resume_preset_'));
  const ws = path.join(home, 'ws');
  fs.mkdirSync(ws, { recursive: true });
  fs.mkdirSync(path.join(home, 'storages', 'session_projcache', 'sessions'), { recursive: true });
  fs.writeFileSync(path.join(home, 'storages', 'workspace.json'), JSON.stringify({
    global: { defaultWorkspaceId: 'w1', archivedSessionIds: [] },
    tables: { workspaces: { w1: { id: 'w1', path: ws } } },
  }));
  // 会话元数据：有标题、有 1 轮，所以不是空白会话，会进候选。
  fs.writeFileSync(path.join(home, 'storages', 'session_projcache', 'sessions', `${SESSION_ID}.json`), JSON.stringify({
    record: {
      rows: {
        title: { val: 'zerofal 自检' },
        sessionListMetadata: { val: { blank: false } },
        turnOutline: { val: { turns: [{ turn: 1, prompt: '自检一下zerofal', response: '' }] } },
      },
    },
  }));
  writeSnapshot(home);
  return home;
}

/** 写一份"关闭前它正在跑"的快照。 */
function writeSnapshot(home) {
  const now = Date.now();
  fs.writeFileSync(path.join(home, 'resume-sessions.json'), JSON.stringify({
    version: 2,
    updatedAt: now,
    sessions: {
      [SESSION_ID]: {
        cwd: path.join(home, 'ws'),
        title: 'zerofal 自检',
        status: 'idle',
        at: now,
        running: true,
        runningAt: now,
        interrupted: true,
      },
    },
  }));
}

/** 最小 ctx：只提供插件真正用到的服务和签名。 */
function makeCtx({ mountedPreset }) {
  const calls = { resume: [], mount: [], emitted: [] };
  const agents = {
    async resume(options) {
      calls.resume.push(options);
      const agent = {
        id: options.resumeSessionId,
        session: { header: { cwd: '' }, id: options.resumeSessionId },
        ctx: { id: options.resumeSessionId, mounted: undefined },
      };
      if (typeof options.setup === 'function') await options.setup(agent.ctx, agent);
      agents._live = agent;
      return { agent };
    },
    get: () => agents._live,
  };
  const presets = {
    async mount(agentCtx, id) {
      calls.mount.push(id ?? null);
      agentCtx.mounted = id ?? mountedPreset;
    },
    presetForSession: () => mountedPreset,
    composedPreset: (agentCtx) => agentCtx?.mounted,
  };
  const routes = new Map();
  const webServer = { register: ({ path: routePath, handler }) => routes.set(routePath, handler) };
  const ctx = {
    get(name) {
      if (name === 'agents') return agents;
      if (name === 'agentPresets') return presets;
      if (name === 'webServer') return webServer;
      if (name === 'loader') return { await: async () => {} };
      return undefined;
    },
    on: () => () => {},
    effect: (fn) => {
      const disposer = fn();
      return () => {
        if (typeof disposer === 'function') disposer();
      };
    },
    inject: (_deps, cb) => cb({
      get: (name) => ctx.get(name),
      effect: (fn) => {
        fn();
      },
    }),
    emit: (name) => calls.emitted.push(name),
    logger: { warn: () => {}, info: () => {}, error: () => {} },
  };
  return { ctx, calls, routes, agents };
}

async function triggerRestore(routes) {
  const handler = routes.get('/resume-sessions/restore');
  assert.ok(handler, '/resume-sessions/restore 没注册');
  const chunks = [];
  const res = {
    statusCode: 0,
    setHeader() {},
    end(body) {
      chunks.push(body);
    },
  };
  await handler({ method: 'POST' }, res);
  return { statusCode: res.statusCode, body: chunks.join('') };
}

const { apply } = await import(pathToFileURL(PLUGIN).href);

// ── 场景 1：resume 必须带 setup 并挂上预设 ──────────────────────────────────
{
  const home = makeHome();
  process.env.DSH_HOME = home;
  const { ctx, calls, routes } = makeCtx({ mountedPreset: 'unrestricted' });
  apply(ctx, { enabled: true, mode: 'live', startupDelayMs: 600000, resumeGapMs: 0, maxSessions: 1, verbose: false });

  const result = await triggerRestore(routes);
  assert.equal(result.statusCode, 200, `restore 失败：${result.body}`);

  const options = calls.resume[0];
  assert.ok(options, '没有调用 agents.resume');
  assert.equal(
    typeof options.setup,
    'function',
    'resume 没有带 setup —— 预设不会挂上，恢复出来的 agent 只有宿主平面工具',
  );
  assert.deepEqual(calls.mount, ['unrestricted'], `预设没有挂载：${JSON.stringify(calls.mount)}`);

  const report = JSON.parse(result.body).report;
  assert.deepEqual(report.live, [SESSION_ID]);
  const entry = JSON.parse(fs.readFileSync(path.join(home, 'resume-sessions.json'), 'utf8')).sessions[SESSION_ID];
  assert.equal(entry.preset, 'unrestricted', '快照里没记下实际挂载的预设');

  fs.rmSync(home, { recursive: true, force: true });
  console.log(`场景1 通过：resume 带 setup，预设=${calls.mount[0]}`);
}

// ── 场景 2：已经活着但没预设的会话要补挂 ────────────────────────────────────
{
  const home = makeHome();
  process.env.DSH_HOME = home;
  const { ctx, calls, routes, agents } = makeCtx({ mountedPreset: 'unrestricted' });
  apply(ctx, { enabled: true, mode: 'live', startupDelayMs: 600000, resumeGapMs: 0, maxSessions: 1, verbose: false });

  // 伪造一个"活着但没有预设"的 agent（旧版 resume 留下的现场）。
  agents._live = { id: SESSION_ID, session: { header: { cwd: path.join(home, 'ws') } }, ctx: { id: SESSION_ID, mounted: undefined } };
  writeSnapshot(home);   // 状态回到 idle，让它重新进候选

  const result = await triggerRestore(routes);
  assert.equal(result.statusCode, 200, `restore 失败：${result.body}`);
  assert.deepEqual(calls.mount, ['unrestricted'], '活着的无预设会话没有被补挂');
  assert.equal(agents._live.ctx.mounted, 'unrestricted');

  const report = JSON.parse(result.body).report;
  assert.deepEqual(report.healed, [SESSION_ID], '报告里没有 healed');

  fs.rmSync(home, { recursive: true, force: true });
  console.log('场景2 通过：活着的无预设会话被补挂');
}

console.log('test_resume_preset_mount：全部通过');

