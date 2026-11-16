/**
 * dsh-resume-sessions — dsh 正常启动后，自动把「上次关闭前还在运行」的会话跑起来。
 *
 * 背景（为什么需要插件）：
 *   桌面端的重启/关闭是 Stop-Process -Force，没有退出钩子，正在跑的 turn、
 *   已 arm 的 goal 续行全部丢失。DSH 自身的安全默认是「重启后绝不自行复活工作」
 *   （goal-round-driver 明确写了「驱动器绝不会自行复活工作」，resume 后 goal 是 disarmed），
 *   所以必须由插件显式把「继续跑」这道权限补回来。
 *
 * 两段式：
 *   1) 运行期快照：agent/status 每次变化立刻落盘（进入 running 的那一帧就写，
 *      下一秒被硬杀也不丢）。同时记 cwd / goal phase / 标题。
 *   2) 启动期恢复：等 agents 服务就绪，把「上次是 running」或「有 active goal」的会话
 *      逐个 ctx.agents.resume() 恢复成 live；再按下面的策略把它们真正「跑起来」：
 *        - 日志里还有未关闭的 turn（turnBoundary.openTurnStartSeq 存在）= 上次确实被打断
 *          → arm goal（如果有）或 followup 一条继续指令，让它接着干；
 *        - 上次只是 idle、没有未完成 turn → 只恢复 live，不替你说话（不烧 token）。
 *
 * 与已删除的旧版 dsh-resume-running 的关键差异（旧版会因为"resume 永不返回"把 host 拖死）：
 *   - 每个 resume 都带 AbortSignal + 超时，超时先 abort 再放弃；单点失败绝不影响其它会话；
 *   - 恢复流程整体 fire-and-forget，永不阻塞 host 启动路径；
 *   - followup 用真正的 createUserMessage（旧版直接丢裸对象，inbox 校验不过）；
 *   - 快照写盘全部 try/catch，状态文件坏了就当没有，绝不因为一个坏文件拖崩宿主。
 *
 * 观察方式：
 *   GET  /resume-sessions/state      看快照、候选与上次恢复报告
 *   POST /resume-sessions/restore    手动补跑一次（force）
 */

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { randomUUID } from 'node:crypto';

/** 尽量用 DSH 自己的消息构造器；解析不到就退化成结构等价的手写消息。 */
let createUserMessage;
try {
  ({ createUserMessage } = await import('@deepseek-ai/dsh-llm'));
} catch {
  createUserMessage = undefined;
}

const SNAPSHOT_VERSION = 2;
const STATE_FILE = 'resume-sessions.json';
const LOG_PREFIX = '[resume-sessions]';
/** 启动窗口瞬态失败最多试几次（含首轮）。 */
const MAX_ATTEMPTS = 4;
/** 梯度重试间隔：启动期 loop 可能还在挂载/重载。 */
const RETRY_DELAYS = [3000, 8000, 20000, 45000];

const DEFAULTS = {
  enabled: true,
  /** live = 只恢复成 live；continue = 额外让它接着跑（推荐，见 finishOne） */
  mode: 'continue',
  continuePrompt:
    '继续上次未完成的工作。先核对当前进度与工作区/会话里的实际状态，不要重复已经完成的部分；' +
    '需要证据的地方自己验证，做完再汇报。',
  /** 等 agents 服务就绪的宽限（webServer 起来 ≠ 插件全部挂完）。 */
  startupDelayMs: 8000,
  /** 单个 resume 的上限；超时先 abort 再放弃，绝不无限等。 */
  resumeTimeoutMs: 90000,
  /** 保质期：超过这么多小时没活动的会话不再拉起。 */
  maxAgeHours: 48,
  /**
   * 单次最多恢复几个。
   *
   * 2026-11-14 从 2 提到 8：用户实测"关之前开了 4~5 个会话在跑，重启只接上一个"。
   * 上限 8 而不是无限，是因为 resume 会把整份会话日志读进内存 ——
   * 真正的内存闸门是下面的 maxSessionLogMB / maxTotalLogMB，这两个才是硬约束。
   * 另外崩溃降级仍然生效：连续两次在恢复时把宿主带走，就自动降到 1 / 0。
   */
  maxSessions: 8,
  /**
   * 「关闭前这么多分钟内还在 running」也算关闭时在跑（分钟，0 = 关掉这个判据）。
   *
   * 为什么需要：桌面端退出时会先结束 turn（agent 从 running 短暂变 idle / 写掉 openTurn），
   * 再 dispose。只认「dispose 那一刻还在 running」会漏掉真实情况 ——
   * 实测有会话在关闭前 27 秒还在 running，却被记成「没在跑」，于是重启后什么都没恢复。
   */
  runningWindowMin: 15,
  /** 本轮恢复的日志总量上限（MB）。比"个数"更能约束内存峰值。 */
  maxTotalLogMB: 64,
  /**
   * 只让第一个恢复的会话续跑，其余只恢复 live。
   *
   * 2026-11-14 默认改成 false：旧默认会让"关之前 5 个都在跑"变成"只有 1 个继续跑"。
   * 关掉之后仍然**串行**恢复（每份之间留 resumeGapMs），不是并发打模型 ——
   * 所以要限流就调 maxSessions，不要指望这个开关去挡内存。
   */
  continueOnlyFirst: false,
  /** 两次 resume 之间的间隔，给上一份日志留出回收时间。 */
  resumeGapMs: 2500,
  /** 单会话日志超过这个大小就跳过（超大日志 resume 会吃满内存）。 */
  maxSessionLogMB: 8,
  /** 有 active goal 的会话即使当时 idle 也算「在运行」。 */
  includeGoalActive: true,
  /** 恢复后重新 arm goal 续行（把 DSH 的安全默认显式补回来）。 */
  rearmGoals: true,
  /** 限定工作区；留空 = 用当前默认工作区。 */
  workspacePath: '',
  /**
   * 跨工作区恢复。
   *
   * 2026-11-14 默认 true：「关闭前在跑」这件事跟它在哪个工作区无关。
   * 旧行为只恢复"当前默认工作区"的会话，人在另一个工作区开的会话会被静默丢掉，
   * 表现出来又是"只接上一个"。
   */
  anyWorkspace: true,
  /**
   * 上个进程留下的 live / restored 条目，按"上个进程消失的时刻"重新判定。
   *
   * 2026-11-14 加：崩溃 / 被 taskkill / 被守护进程重启时没有 dispose，
   * 条目停在 live，而 pick() 排除 live —— 结果崩溃之后一个都不恢复。
   */
  staleLiveAsInterrupted: true,
  verbose: true,
};

// ── 基础工具 ────────────────────────────────────────────────────────────────

function homeDir() {
  const env = typeof process.env.DSH_HOME === 'string' ? process.env.DSH_HOME.trim() : '';
  return env !== '' ? env : path.join(os.homedir(), '.dsh');
}

const stateFile = () => path.join(homeDir(), STATE_FILE);
const workspaceFile = () => path.join(homeDir(), 'storages', 'workspace.json');
const projectionDir = () => path.join(homeDir(), 'storages', 'session_projcache', 'sessions');

function readJson(file, fallback) {
  try {
    const parsed = JSON.parse(fs.readFileSync(file, 'utf8'));
    return parsed === null || typeof parsed !== 'object' ? fallback : parsed;
  } catch {
    return fallback;
  }
}

/** 原子写：先写同目录 .tmp 再 rename，硬杀不会留下半个 JSON。 */
function writeJson(file, value) {
  const dir = path.dirname(file);
  fs.mkdirSync(dir, { recursive: true });
  const tmp = `${file}.${process.pid}.${Math.random().toString(36).slice(2, 8)}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(value, null, 2), 'utf8');
  fs.renameSync(tmp, file);
}

function samePath(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string' || a === '' || b === '') return false;
  const norm = (p) => {
    try {
      return path.resolve(p).replace(/[\\/]+$/, '').toLowerCase();
    } catch {
      return String(p).replace(/[\\/]+$/, '').toLowerCase();
    }
  };
  return norm(a) === norm(b);
}

function normalizeConfig(raw) {
  const cfg = { ...DEFAULTS, ...(raw && typeof raw === 'object' ? raw : {}) };
  const num = (value, fallback, min, max) => {
    const n = Number(value);
    if (!Number.isFinite(n)) return fallback;
    return Math.min(Math.max(n, min), max);
  };
  return {
    ...cfg,
    enabled: cfg.enabled !== false,
    mode: cfg.mode === 'live' ? 'live' : 'continue',
    continuePrompt: String(cfg.continuePrompt || DEFAULTS.continuePrompt),
    startupDelayMs: num(cfg.startupDelayMs, DEFAULTS.startupDelayMs, 0, 10 * 60_000),
    resumeTimeoutMs: num(cfg.resumeTimeoutMs, DEFAULTS.resumeTimeoutMs, 3_000, 30 * 60_000),
    maxAgeHours: num(cfg.maxAgeHours, DEFAULTS.maxAgeHours, 0, 24 * 30),
    maxSessions: Math.round(num(cfg.maxSessions, DEFAULTS.maxSessions, 0, 32)),
    runningWindowMin: num(cfg.runningWindowMin, DEFAULTS.runningWindowMin, 0, 1440),
    maxTotalLogMB: num(cfg.maxTotalLogMB, DEFAULTS.maxTotalLogMB, 0, 4096),
    continueOnlyFirst: cfg.continueOnlyFirst !== false,
    resumeGapMs: num(cfg.resumeGapMs, DEFAULTS.resumeGapMs, 0, 60_000),
    maxSessionLogMB: num(cfg.maxSessionLogMB, DEFAULTS.maxSessionLogMB, 0, 1024),
    includeGoalActive: cfg.includeGoalActive !== false,
    rearmGoals: cfg.rearmGoals !== false,
    workspacePath: typeof cfg.workspacePath === 'string' ? cfg.workspacePath.trim() : '',
    verbose: cfg.verbose !== false,
  };
}

/** 上一次 turn 是否真的没关（有 openTurnStartSeq = 关闭时正在跑）。 */
function hasOpenTurn(sessionId) {
  const file = path.join(projectionDir(), `${sessionId}.json`);
  const doc = readJson(file, null);
  const boundary = doc?.record?.rows?.turnBoundary?.val;
  const open = boundary?.openTurnStartSeq;
  return typeof open === 'number' && open >= 0;
}

/**
 * 是不是"空白会话"（从没发过 prompt）。
 * 这种会话没有标题，侧栏只会拿工作区名兜底显示，恢复它毫无意义，
 * 还会让人以为"标题被工作区名顶掉了"。
 */
function isBlankSession(sessionId) {
  const file = path.join(projectionDir(), `${sessionId}.json`);
  const doc = readJson(file, null);
  const meta = doc?.record?.rows?.sessionListMetadata?.val;
  if (meta && typeof meta === 'object' && meta.blank === true) return true;
  const outline = doc?.record?.rows?.turnOutline?.val;
  const turns = Array.isArray(outline?.turns) ? outline.turns : [];
  const first = turns[0]?.prompt;
  // 没有会话元数据但有 outline 的会话按 outline 判断；两者都读不到就不当空白。
  if (meta && turns.length === 0 && (typeof first !== 'string' || first.trim() === '')) return true;
  return false;
}

/** 该会话日志的总字节数（resume 会把它整份读进内存，太大就跳过）。 */
function sessionLogBytes(sessionId) {
  const root = path.join(homeDir(), 'sessions');
  let total = 0;
  try {
    for (const workspace of fs.readdirSync(root, { withFileTypes: true })) {
      if (!workspace.isDirectory()) continue;
      const dir = path.join(root, workspace.name, sessionId);
      let stat;
      try {
        stat = fs.statSync(dir);
      } catch {
        continue;
      }
      if (!stat.isDirectory()) continue;
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        if (!entry.isFile()) continue;
        try {
          total += fs.statSync(path.join(dir, entry.name)).size;
        } catch {
          /* ignore */
        }
      }
    }
  } catch {
    return 0;
  }
  return total;
}

function userMessage(text) {
  const content = [{ type: 'text', text }];
  const source = { kind: 'resume-sessions' };
  if (typeof createUserMessage === 'function') {
    try {
      return createUserMessage({ content, source });
    } catch {
      /* 退化成手写结构 */
    }
  }
  return Object.freeze({
    id: randomUUID(),
    role: 'user',
    content: Object.freeze(content.map((block) => Object.freeze(block))),
    source: Object.freeze(source),
  });
}

// ── 插件 ────────────────────────────────────────────────────────────────────

export function apply(ctx, rawConfig) {
  const cfg = normalizeConfig(rawConfig);
  if (!cfg.enabled) return;
  // 插件被装载的时刻：用来量「从启动到真正开始恢复」花了多久。
  const appliedAt = Date.now();

  const log = (msg) => {
    if (cfg.verbose) console.error(`${LOG_PREFIX} ${msg}`);
  };
  const warn = (msg) => console.error(`${LOG_PREFIX} ${msg}`);

  // ── 单实例锁 ───────────────────────────────────────────────────────────────
  // 同一个 DSH_HOME 起两个 dsh（比如修完又开了一个）会互抢 resume-sessions.json，
  // 表现是"同一个会话被恢复两次"或快照被交叉覆盖。锁文件记 pid，活着的持有者才挡住后来者。
  const lockFile = path.join(homeDir(), 'resume-sessions.lock');
  let lockOwner = null;
  try {
    const current = readJson(lockFile, null);
    const pid = Number(current?.pid);
    if (Number.isFinite(pid) && pid > 0 && pid !== process.pid) {
      let alive = false;
      try {
        process.kill(pid, 0);   // 只探活，不发信号
        alive = true;
      } catch {
        alive = false;          // ESRCH / EPERM 都当"已不在"
      }
      if (alive) lockOwner = pid;
    }
    if (lockOwner === null) writeJson(lockFile, { pid: process.pid, at: Date.now() });
  } catch {
    /* 锁失败不该阻止插件工作 */
  }
  if (lockOwner !== null) {
    warn(`另一个 dsh 实例（pid ${lockOwner}）已经持有快照，本实例只记录不恢复`);
    return;
  }

  const file = stateFile();
  let state = readJson(file, null);
  if (state === null || typeof state !== 'object' || typeof state.sessions !== 'object' || state.sessions === null) {
    state = { version: SNAPSHOT_VERSION, sessions: {} };
  }
  state.version = SNAPSHOT_VERSION;
  if (state.sessions === null || typeof state.sessions !== 'object') state.sessions = {};
  // 上个进程最后一次写盘的时刻 = 它挂掉/退出的时刻（后面判定 stale live 要用）
  const previousUpdatedAt = Date.parse(state.updatedAt ?? '') || 0;

  // ── 崩溃保护 ───────────────────────────────────────────────────────────────
  // resume 会把整份会话日志读进内存；同时拉起多个会话曾把 host 原生打崩（exit 0xE0000008）。
  // 机制：恢复开始前置 boot.restoring=true，正常结束后清掉。若下次启动发现它还挂着，
  // 说明上一次恢复把进程带走了 —— 于是降级：第一次只恢复 1 个并延后，连续两次就停掉自动恢复，
  // 只保留手动接口（POST /resume-sessions/restore）。
  const previousBoot = state.boot && typeof state.boot === 'object' ? state.boot : undefined;
  const crashedDuringRestore = previousBoot?.restoring === true;
  const guard = state.guard && typeof state.guard === 'object' ? state.guard : { crashes: 0 };
  guard.crashes = crashedDuringRestore ? (Number(guard.crashes) || 0) + 1 : 0;
  state.guard = guard;
  state.boot = { pid: process.pid, at: Date.now(), restoring: false };
  if (crashedDuringRestore) {
    warn(`上次启动在恢复会话时异常退出（累计 ${guard.crashes} 次）——本次自动恢复降级`);
  }

  // ── 上个进程留下的 live / restored：必须重新判定 ─────────────────────────
  //
  // 正常关闭会 dispose，条目变成 disposed + interrupted；但**崩溃 / 被 taskkill /
  // 被守护进程重启**时没有 dispose，条目会一直停在 live。而 pick() 明确排除
  // live / restored（那本来是"本进程里已经活着"的语义）——于是崩溃之后这些会话
  // 永远不会被恢复，表现就是"关掉再开，一个都没回来"。
  //
  // 锚点：上一个进程最后一次写盘的时刻（previousUpdatedAt）= 它消失的时刻。
  // 只有"最后活动落在 runningWindowMin 窗口内"的才认为关的时候在跑；
  // 早就闲置的老会话不动，避免一重启把一堆历史会话拉起来。
  if (cfg.staleLiveAsInterrupted && previousUpdatedAt > 0) {
    const windowMs = Math.max(0, cfg.runningWindowMin) * 60_000;
    let revived = 0;
    for (const [, entry] of Object.entries(state.sessions)) {
      if (!entry || typeof entry !== 'object') continue;
      if (entry.status !== 'live' && entry.status !== 'restored') continue;
      if (entry.disposedAt) continue;
      const lastActive = Math.max(Number(entry.runningAt) || 0, lastTurnEnd(entry));
      if (lastActive <= 0) continue;
      if (previousUpdatedAt >= lastActive && previousUpdatedAt - lastActive <= windowMs) {
        entry.status = 'disposed';
        entry.disposedAt = previousUpdatedAt;
        entry.interrupted = true;
        entry.staleLiveRevivedAt = Date.now();
        revived += 1;
      }
    }
    if (revived) {
      log(`把上个进程留下的 ${revived} 个 live 会话重新判为「关闭时在跑」`);
      persist();
    }
  }

  let flushTimer = null;
  function persist() {
    try {
      state.updatedAt = new Date().toISOString();
      state.pid = process.pid;
      writeJson(file, state);
    } catch (err) {
      warn(`快照写盘失败：${err && err.message ? err.message : String(err)}`);
    }
  }
  /** running 状态要立刻写；其它变化合并成一次写。 */
  function schedulePersist(immediate) {
    if (immediate) {
      if (flushTimer !== null) {
        clearTimeout(flushTimer);
        flushTimer = null;
      }
      persist();
      return;
    }
    if (flushTimer !== null) return;
    flushTimer = setTimeout(() => {
      flushTimer = null;
      persist();
    }, 1500);
    if (typeof flushTimer.unref === 'function') flushTimer.unref();
  }

  function entryFor(sessionId) {
    let entry = state.sessions[sessionId];
    if (entry === null || typeof entry !== 'object') {
      entry = {};
      state.sessions[sessionId] = entry;
    }
    return entry;
  }

  /**
   * 快照瘦身：只留"还可能被用到"的记录。
   *   · 90 天没动、且不是 running/interrupted 的（早就没意义）
   *   · 总量超过 200 条时按最近活动截断
   * 不清理正在跑 / 被打断的，否则会把要恢复的东西自己删掉。
   */
  function pruneState() {
    const cutoff = Date.now() - 90 * 86_400_000;
    let removed = 0;
    for (const [id, entry] of Object.entries(state.sessions)) {
      if (entry === null || typeof entry !== 'object') {
        delete state.sessions[id];
        removed += 1;
        continue;
      }
      if (entry.running === true || entry.interrupted === true) continue;
      if (entry.status === 'running' || entry.status === 'retry') continue;
      const at = Number(entry.at) || 0;
      if (at > 0 && at < cutoff) {
        delete state.sessions[id];
        removed += 1;
      }
    }
    const entries = Object.entries(state.sessions);
    if (entries.length > 200) {
      entries.sort((a, b) => (Number(b[1].at) || 0) - (Number(a[1].at) || 0));
      for (const [id] of entries.slice(200)) {
        delete state.sessions[id];
        removed += 1;
      }
    }
    if (removed > 0) {
      log(`快照瘦身：移除 ${removed} 条过期记录`);
      persist();
    }
  }

  function safeGet(name) {
    try {
      const service = ctx.get ? ctx.get(name) : ctx[name];
      return service && typeof service === 'object' ? service : null;
    } catch {
      return null;
    }
  }

  function cwdOf(agent) {
    try {
      return agent?.session?.header?.cwd ?? agent?.session?.cwd ?? '';
    } catch {
      return '';
    }
  }

  function titleOf(agent) {
    try {
      const title = agent?.session?.header?.title;
      if (typeof title === 'string' && title.trim() !== '') return title;
    } catch {
      /* 继续走投影兜底 */
    }
    // header 里通常没有标题：真正的标题在投影里（UI 侧用 sessionListMetadata / title 行）。
    try {
      const sessionId = agent?.id ?? agent?.session?.id;
      if (typeof sessionId === 'string') {
        const doc = readJson(path.join(projectionDir(), `${sessionId}.json`), null);
        const projected = doc?.record?.rows?.title?.val;
        if (typeof projected === 'string' && projected.trim() !== '') return projected;
      }
    } catch {
      /* ignore */
    }
    return '';
  }

  // ── 1. 运行期快照 ─────────────────────────────────────────────────────────

  ctx.on('agent/created', ({ agent } = {}) => {
    if (!agent || typeof agent.id !== 'string') return;
    const entry = entryFor(agent.id);
    entry.cwd = cwdOf(agent) || entry.cwd || '';
    entry.title = titleOf(agent) || entry.title || '';
    entry.status = typeof entry.status === 'string' ? entry.status : 'idle';
    entry.at = Date.now();
    schedulePersist(false);
    log(`created ${agent.id}`);
  });

  ctx.on('agent/status', ({ agent, status } = {}) => {
    if (!agent || typeof agent.id !== 'string') return;
    const entry = entryFor(agent.id);
    entry.cwd = cwdOf(agent) || entry.cwd || '';
    entry.title = titleOf(agent) || entry.title || '';
    entry.status = typeof status === 'string' ? status : 'unknown';
    entry.at = Date.now();
    // running 用布尔单独立记录：agent 被 dispose（关窗口/正常退出/重启）时，
    // 状态会从 running 变成 disposed，但这恰恰说明"关闭那一刻它还在跑"。
    entry.running = status === 'running';
    if (entry.running) entry.runningAt = Date.now();
    // 留一小段状态轨迹：下次"到底为什么没恢复"有据可查，不用靠猜。
    try {
      const hist = Array.isArray(entry.history) ? entry.history : [];
      hist.push({ t: Date.now(), s: entry.status });
      entry.history = hist.slice(-6);
    } catch {
      /* ignore */
    }
    try {
      const goals = safeGet('goals');
      const goal = goals && typeof goals.get === 'function' ? goals.get(agent) : null;
      entry.goalPhase = goal?.phase ?? null;
      entry.goalActivation = goal?.activation ?? null;
      entry.goal = goal ? { id: goal.id, revision: goal.revision } : null;
    } catch {
      /* goal 服务不在 / agent 已不是 live 实例：保持上次记录 */
    }
    schedulePersist(status === 'running');
    if (status === 'running') log(`running ${agent.id}`);
  });

  ctx.on('agent/disposed', ({ agent } = {}) => {
    if (!agent || typeof agent.id !== 'string') return;
    const entry = state.sessions[agent.id];
    if (!entry) return;
    // 关键：disposed 不能把"在跑"这件事抹掉。桌面端正常退出时每个 live agent 都会
    // 收到 disposed，若这里写成 status='disposed' 又不留 interrupted 标记，下次启动
    // 的候选就永远是空的 —— 表现就是"重启后什么都没恢复"。
    // 关的那一刻还在跑（running 标记），或者 turn 刚刚收尾（entry.at 紧跟 runningAt）
    // —— 后者覆盖"退出流程先把 turn 收尾再 dispose"，实测有会话在关闭前 27 秒还在跑。
    // 注意这里传 Date.now()：disposedAt 还没写，此刻"关闭时刻"就是现在这一刻。
    entry.interrupted = entry.running === true || closedWhileRunning(entry, Date.now());
    entry.running = false;
    entry.disposedAt = Date.now();
    entry.status = 'disposed';
    entry.at = Date.now();
    try {
      const hist = Array.isArray(entry.history) ? entry.history : [];
      hist.push({ t: Date.now(), s: 'disposed' });
      entry.history = hist.slice(-6);
    } catch {
      /* ignore */
    }
    // dispose 往往就是"进程要没了"的时刻，同步落盘，别等节流。
    schedulePersist(true);
    if (entry.interrupted) log(`disposed while running ${agent.id}（下次启动会恢复它）`);
  });

  // ── 2. 候选挑选 ───────────────────────────────────────────────────────────

  function defaultWorkspacePath() {
    const ws = readJson(workspaceFile(), null);
    const id = ws?.global?.defaultWorkspaceId;
    const table = ws?.tables?.workspaces;
    if (typeof id === 'string' && table && typeof table === 'object') {
      const row = table[id];
      if (row && typeof row.path === 'string' && row.path !== '') return row.path;
    }
    return '';
  }

  function archivedIds() {
    const ws = readJson(workspaceFile(), null);
    const list = ws?.global?.archivedSessionIds;
    return new Set(Array.isArray(list) ? list.filter((id) => typeof id === 'string') : []);
  }

  /**
   * 这次的候选：**只取关闭前确实在运行的**。
   *   - status === 'running'（关闭那一刻 agent 正在跑 turn）
   *   - 或 includeGoalActive 且 goal.phase === 'active'（goal 本该被续行）
   *   - force（手动补跑）额外只放宽到 failed/timeout/retry 的**同一批**，绝不把 idle 历史会话拉起来。
   * @param {boolean} force 手动补跑。
   * @param {Set<string>|undefined} only 只处理这些会话 id（失败自动重试用）。
   */
  /**
   * 最后一轮 turn 收尾的时刻。
   *
   * history 里存的是状态变更轨迹（{t, s}），最后一个 'idle' 就是 turn 收尾那一刻。
   * 'disposed' 不能当 turn 收尾 —— 它的 t 就是关闭时刻，拿它当锚点会让判据恒真。
   */
  function lastTurnEnd(entry) {
    const hist = Array.isArray(entry?.history) ? entry.history : [];
    for (let i = hist.length - 1; i >= 0; i -= 1) {
      const h = hist[i];
      if (!h || typeof h.t !== 'number') continue;
      if (h.s === 'idle' || (h.s !== 'running' && h.s !== 'disposed')) return h.t;
    }
    return 0;
  }

  /**
   * 关闭那一刻它是不是刚跑过 → 认为「关闭那一刻它在跑」。
   *
   * 锚点（取较晚的那个）：
   *   - runningAt：最后一次进入 running 的时刻（turn 起点）
   *   - turnEnd：  最后一轮 turn 收尾的时刻（history 里最后一个 idle）
   * 关闭时刻：**只认 disposedAt**（真正被 dispose 过才算"关闭"）。
   *
   * 为什么不能拿 entry.at 兜底：entry.at 是"这条记录最后一次被写入的时间"，
   * 对一个跑完就 idle、之后再没被碰过的会话来说，它等于 turn 收尾时刻，
   * 于是 closed - anchor ≈ 0 → 判据恒真。这类会话会在 maxAgeHours（48h）里
   * 一直占着候选名额，把真正被中断的会话挤出 maxSessions（2）的坑位。
   * 实测：本机 24 个候选里 14 个是这种陈旧 idle 会话。
   *
   * @param {object} entry
   * @param {number} [closedAt] 关闭时刻；dispose 事件当场调用时传 Date.now()。
   */
  function closedWhileRunning(entry, closedAt) {
    const windowMs = Math.max(0, cfg.runningWindowMin) * 60_000;
    if (windowMs <= 0) return false;
    const closed = Number(closedAt ?? entry?.disposedAt ?? 0);
    if (closed <= 0) return false;
    const lastActive = Math.max(Number(entry?.runningAt) || 0, lastTurnEnd(entry));
    if (lastActive <= 0) return false;
    return closed >= lastActive && closed - lastActive <= windowMs;
  }

  function pick(force, only) {
    const target = cfg.anyWorkspace
      ? ''
      : (cfg.workspacePath !== '' ? cfg.workspacePath : defaultWorkspacePath());
    const archived = archivedIds();
    const maxAge = Math.max(0, cfg.maxAgeHours) * 3_600_000;
    const now = Date.now();
    const rows = Object.entries(state.sessions)
      .filter(([id]) => only === undefined || only.has(id))
      .filter(([, entry]) => entry !== null && typeof entry === 'object')
      .filter(([, entry]) => {
        // 已经恢复过 / 用户自己开着的，不重复抢写锁。
        if (entry.status === 'live' || entry.status === 'restored') return false;
        // 三种"关闭前确实在跑"：还在 running / 在 running 时被 dispose（正常退出或崩溃）/
        // goal 仍 active（本来就该续行）。
        const wasRunning = entry.status === 'running' || entry.running === true;
        const interrupted = entry.interrupted === true;
        const goalActive = cfg.includeGoalActive && entry.goalPhase === 'active';
        // 退出流程会把 turn 收尾再 dispose，只看「dispose 时还在 running」会漏。
        // 关闭前窗口内确实在跑（disposedAt 锚定）也算数。
        const recentlyRunning = closedWhileRunning(entry);
        if (wasRunning || interrupted || goalActive || recentlyRunning) return true;
        // 只有手动补跑时才重试上次没恢复成的同一批，idle 的历史会话永远不进来。
        if (force && (entry.status === 'failed' || entry.status === 'timeout' || entry.status === 'retry')) return true;
        return false;
      })
      .filter(([id]) => !archived.has(id))
      .filter(([, entry]) => force || maxAge === 0 || now - (Number(entry.at) || 0) <= maxAge)
      .filter(([, entry]) => target === '' || samePath(entry.cwd, target))
      .sort((a, b) => (Number(b[1].runningAt) || Number(b[1].at) || 0) - (Number(a[1].runningAt) || Number(a[1].at) || 0));
    // 崩溃降级：连续两次在恢复时把进程带走，就不再自动恢复（手动接口仍然可用）。
    const limit = force === true
      ? Math.max(1, cfg.maxSessions)
      : (guard.crashes >= 2 ? 0 : guard.crashes === 1 ? 1 : Math.max(0, cfg.maxSessions));
    log(`候选 ${rows.length} 个（工作区=${target || '未限定'}，上限 ${limit}${guard.crashes ? `，崩溃降级 ${guard.crashes} 级` : ''}）`);
    return rows.slice(0, limit);
  }

  // ── 3. 恢复 ───────────────────────────────────────────────────────────────

  let busy = false;
  let restoredThisProcess = false;
  let lastReport = null;

  /**
   * 恢复时把会话自己的 agent preset 重新挂上。
   *
   * `agents.resume` 只负责「读日志 → 建 agent」，**不组合预设**；官方 API 控制器
   * （dsh-api-session-controller 的 composeAgent）是在 setup 里显式
   * `presets.resolve()` + `presets.mount(agentCtx, id)` 的。
   *
   * 少这一步的后果不是"少几个工具"，而是**整个预设平面消失**：pwsh / read / write /
   * glob / grep / edit / skill / schedule / subagent 全都不进目录，只剩宿主平面那几行
   * （working_directory、tiangong_*、purge_*）。模型于是把 pwsh 当 "unknown tool"，
   * 以为会话"没有 shell 工具"。这正是 2026-11-12 那两个会话（上下文75%频报Token上限、
   * zerofal 自检）工具消失的原因。
   *
   * @returns setup 回调；宿主没装预设服务时返回 undefined（保持老行为）。
   */
  function presetSetup() {
    const presets = safeGet('agentPresets');
    if (!presets || typeof presets.mount !== 'function') return undefined;
    return async (agentCtx, agent) => {
      // header / 投影里记着会话当初用的预设；缺失就走 selectedDefault。
      let id;
      try {
        id = presets.presetForSession(agent.session) ?? undefined;
      } catch {
        id = undefined;
      }
      try {
        await presets.mount(agentCtx, id);
        log(`预设已挂载 ${agent.id}：${id || '(默认)'}`);
      } catch (err) {
        // 预设坏了不该拖死恢复流程：agent 照样起来，只是工具面回退到宿主平面。
        // 原因写进日志和快照，别让"工具没了"再变成猜。
        const text = errorText(err);
        agent.lastPresetError = text;
        warn(`挂载预设失败 ${agent.id}：${text}`);
      }
    };
  }

  /** 读一个活着的 agent 实际用的预设 id；读不到就是 null（= 没挂上）。 */
  function readComposedPreset(agent) {
    try {
      const presets = safeGet('agentPresets');
      if (!presets || typeof presets.composedPreset !== 'function' || !agent) return null;
      return presets.composedPreset(agent.ctx) ?? null;
    } catch {
      return null;
    }
  }

  /**
   * 给「活着但没有预设」的 agent 补挂预设。
   *
   * 为什么需要：本插件早期版本用 `agents.resume`（不带 setup）恢复会话，
   * 恢复出来的 agent 没有预设平面，工具目录只剩宿主那几行，模型于是报
   * "unknown tool pwsh"。resume 修好之后，**已经坏在内存里的**会话还得救一次：
   * 它们不能走 select（select 只对空白会话开放，已开始的会报 agent-preset/locked），
   * 但可以直接 mount —— bind() 支持在 live ctx 上重绑，再 emit tools/change
   * 让工具目录重建。
   *
   * @returns 是否真的补挂了。
   */
  async function healLivePreset(sessionId, entry, agent) {
    const presets = safeGet('agentPresets');
    if (!presets || typeof presets.mount !== 'function') return false;
    if (entry.status === 'running') return false;   // 别在请求进行中改工具目录
    if (readComposedPreset(agent) !== null) return false;
    let id;
    try {
      id = presets.presetForSession(agent.session) ?? undefined;
    } catch {
      id = undefined;
    }
    if (id === undefined) return false;
    try {
      await presets.mount(agent.ctx, id);
      try {
        ctx.emit('tools/change');
      } catch {
        /* 观察者报错不影响挂载结果 */
      }
      entry.preset = readComposedPreset(agent);
      entry.healedAt = Date.now();
      log(`已给会话补挂预设 ${sessionId}：${entry.preset}`);
      return true;
    } catch (err) {
      entry.lastError = `preset mount: ${errorText(err)}`;
      warn(`给会话补挂预设失败 ${sessionId}：${entry.lastError}`);
      return false;
    }
  }

  /** 带超时的 resume：超时先 abort（让 dsh 自己回收写锁/半成品 agent），再放弃。 */
  async function resumeWithBudget(agents, sessionId) {
    const controller = new AbortController();
    let timer = null;
    const timedOut = new Promise((resolve) => {
      timer = setTimeout(() => {
        try {
          controller.abort(new Error(`resume timed out after ${cfg.resumeTimeoutMs}ms`));
        } catch {
          /* ignore */
        }
        resolve({ kind: 'timeout' });
      }, cfg.resumeTimeoutMs);
      if (typeof timer.unref === 'function') timer.unref();
    });
    try {
      const setup = presetSetup();
      const options = { resumeSessionId: sessionId, signal: controller.signal };
      if (setup !== undefined) options.setup = setup;
      const attempt = agents
        .resume(options)
        .then((handle) => ({ kind: 'ok', handle }), (error) => ({ kind: 'error', error }));
      const result = await Promise.race([attempt, timedOut]);
      return result;
    } finally {
      if (timer !== null) clearTimeout(timer);
    }
  }

  function errorText(value) {
    return value instanceof Error ? value.message : String(value);
  }

  /** 恢复成 live 之后，决定要不要让它真的继续跑。 */
  function finishOne(agents, sessionId, entry, agent, report, allowContinue = true) {
    const goals = safeGet('goals');
    let goal = null;
    try {
      goal = goals && typeof goals.get === 'function' ? goals.get(agent) : null;
    } catch {
      goal = null;
    }

    // goal 续行：把「自动续行」这道权限显式补回来，剩下交给 goal-round-driver。
    if (cfg.rearmGoals && goal && goal.phase !== 'complete' && goal.activation !== 'armed'
        && goals && typeof goals.resume === 'function') {
      try {
        goals.resume(agent, { id: goal.id, revision: goal.revision });
        entry.armedAt = Date.now();
        report.armed.push(sessionId);
        log(`goal 续行已重新授权 ${sessionId}`);
        return;
      } catch (err) {
        entry.lastError = `goal.resume: ${errorText(err)}`;
        warn(`goal 续行授权失败 ${sessionId}：${entry.lastError}`);
      }
    }

    if (cfg.mode !== 'continue') return;

    // 只有「日志里确实还有未关闭的 turn」才续跑；否则单恢复 live，不替你说话。
    if (!entry.interrupted) {
      log(`跳过续跑 ${sessionId}（上次没有未完成的 turn）`);
      return;
    }
    if (!allowContinue) {
      entry.deferredContinue = true;
      log(`跳过续跑 ${sessionId}（本轮续跑额度已用完，只恢复 live）`);
      return;
    }
    try {
      if (typeof agent.followup !== 'function') throw new Error('agent.followup unavailable');
      agent.followup(userMessage(cfg.continuePrompt));
      entry.continuedAt = Date.now();
      entry.deferredContinue = false;
      report.continued.push(sessionId);
      // "上次恢复"优先指向真正接着干的那个会话 —— 用户找的正是它，
      // 而不是同一轮里只恢复成 live 的那一个。
      state.lastRestored = { sessionId, at: entry.continuedAt, title: entry.title || '' };
      log(`已注入继续指令 ${sessionId}`);
    } catch (err) {
      entry.lastError = `followup: ${errorText(err)}`;
      warn(`注入继续指令失败 ${sessionId}：${entry.lastError}`);
    }
  }

  async function restore(force, only) {
    if (busy) return { skipped: 'busy' };
    busy = true;
    const report = {
      at: new Date().toISOString(),
      force: force === true,
      picked: [],
      live: [],
      alreadyLive: [],
      healed: [],
      armed: [],
      continued: [],
      retried: [],
      skipped: [],
      failed: [],
    };
    // 崩溃保护：这一位一直挂着就说明恢复把进程带走了。
    state.boot = { pid: process.pid, at: Date.now(), restoring: true };
    state.phase = 'resuming';
    state.restoreStartAt = Date.now();
    persist();
    try {
      const rows = pick(force === true, only);
      report.picked = rows.map(([id]) => id);
      if (rows.length === 0) {
        log('没有需要恢复的会话');
        lastReport = report;
        return report;
      }
      const agents = safeGet('agents');
      if (!agents || typeof agents.resume !== 'function') {
        warn('agents.resume 不可用，无法恢复');
        report.failed.push({ sessionId: '*', error: 'agents.resume unavailable' });
        lastReport = report;
        return report;
      }

      // 续跑额度：默认让每个恢复上来的会话都真的继续跑（continueOnlyFirst=false）。
      // 打开 continueOnlyFirst 就退回"只让第一个续跑"，其余只留 live。
      // 无论哪个档位，下面的 for 循环都是**串行**的，不会同时压多路请求。
      const continueBudget = cfg.continueOnlyFirst ? 1 : Math.max(1, cfg.maxSessions);
      let continuedCount = 0;
      const allowContinue = () => continuedCount < continueBudget;
      // 本轮日志总量预算：比"个数"更贴近内存峰值。
      const totalLimitBytes = cfg.maxTotalLogMB * 1024 * 1024;
      let totalLogBytes = 0;

      for (const [sessionId, entry] of rows) {
        // 已经活着（用户手动开了 / 这轮已经恢复过）就别再 resume 一次去抢写锁。
        let existing;
        try {
          existing = typeof agents.get === 'function' ? agents.get(sessionId) : undefined;
        } catch {
          existing = undefined;
        }
        if (existing) {
          entry.status = 'live';
          entry.at = Date.now();
          report.alreadyLive.push(sessionId);
          persist();
          // 活着的会话如果压根没挂预设（旧版 resume 留下的），工具面只剩宿主平面，
          // 在这儿直接补挂，别让用户自己去关掉再重开。
          try {
            if (await healLivePreset(sessionId, entry, existing)) report.healed.push(sessionId);
          } catch (err) {
            warn(`补挂预设 ${sessionId} 异常：${errorText(err)}`);
          }
          entry.preset = entry.preset ?? readComposedPreset(existing);
          try {
            const before = report.continued.length;
            // 已经活着的会话不续跑：它可能正是用户当前在用的那个，
            // 抢走「只给第一个」的续跑额度反而会让真正被打断的会话轮不到。
            finishOne(agents, sessionId, entry, existing, report, false);
            if (report.continued.length > before) continuedCount += 1;
          } catch (err) {
            warn(`收尾 ${sessionId} 失败：${errorText(err)}`);
          }
          entry.interrupted = false;   // finishOne 用过之后再清，避免影响续跑判断
          continue;
        }

        // 取并集：dispose 时还在跑（interrupted），日志里 turn 真的没关（hasOpenTurn），
        // 或关闭前窗口内还在 running（退出流程会先收尾 turn，不能只看前两者）。
        entry.interrupted = entry.interrupted === true
          || hasOpenTurn(sessionId)
          || closedWhileRunning(entry);

        // 空白会话（从没发过话）不恢复：没有标题、侧栏只会显示工作区名，
        // 恢复它既没意义，又容易让人以为标题被顶掉了。
        if (isBlankSession(sessionId)) {
          entry.status = 'skipped';
          entry.lastError = 'blank session (never had a prompt)';
          entry.at = Date.now();
          report.skipped.push({ sessionId, reason: entry.lastError });
          log(`跳过空白会话 ${sessionId}`);
          continue;
        }

        // 超大日志直接跳过：resume 会整份读进内存，超过上限足以把 host 打崩。
        const limitBytes = cfg.maxSessionLogMB * 1024 * 1024;
        const logBytes = sessionLogBytes(sessionId);
        if (limitBytes > 0 && logBytes > limitBytes) {
          entry.status = 'skipped';
          entry.lastError = `session log ${(logBytes / 1024 / 1024).toFixed(1)}MB exceeds maxSessionLogMB=${cfg.maxSessionLogMB}`;
          entry.at = Date.now();
          report.skipped.push({ sessionId, reason: entry.lastError });
          warn(`跳过 ${sessionId}：${entry.lastError}`);
          continue;
        }
        if (totalLimitBytes > 0 && totalLogBytes + logBytes > totalLimitBytes) {
          entry.status = 'skipped';
          entry.lastError = `本轮累计日志已到 maxTotalLogMB=${cfg.maxTotalLogMB}，留给下次`;
          entry.at = Date.now();
          report.skipped.push({ sessionId, reason: entry.lastError });
          warn(`跳过 ${sessionId}：${entry.lastError}`);
          continue;
        }
        totalLogBytes += logBytes;

        let result;
        try {
          result = await resumeWithBudget(agents, sessionId);
        } catch (err) {
          result = { kind: 'error', error: err };
        }

        if (result.kind === 'ok') {
          const agent = result.handle?.agent ?? (typeof agents.get === 'function' ? agents.get(sessionId) : undefined);
          entry.status = 'live';
          entry.restoredAt = Date.now();
          entry.lastError = null;
          // 记下这次恢复到底挂上哪个预设：null 就是没挂上（工具面只剩宿主平面）。
          entry.preset = readComposedPreset(agent);
          report.live.push(sessionId);
          // 记下"这次恢复的是谁"：ZeroFall/人都能用它确认到底接上了没有。
          // 必须每次覆盖：只写第一次的话，"上次恢复"会永远停在历史上第一次恢复
          // （实测停在 11-12 上午那次，而当天已经又恢复过好几轮），面板上就成了假信息。
          state.lastRestored = { sessionId, at: entry.restoredAt, title: entry.title || '' };
          if (agent) {
            try {
              const before = report.continued.length;
              finishOne(agents, sessionId, entry, agent, report, allowContinue());
              if (report.continued.length > before) continuedCount += 1;
            } catch (err) {
              warn(`收尾 ${sessionId} 失败：${errorText(err)}`);
            }
          }
          entry.interrupted = false;   // finishOne 用过之后再清
        } else if (result.kind === 'timeout') {
          // 超时：agent 可能还在后台初始化，标记 timeout 而不是 failed，别在下次启动重复抢。
          entry.status = 'timeout';
          entry.lastError = `resume timed out after ${cfg.resumeTimeoutMs}ms`;
          entry.at = Date.now();
          report.failed.push({ sessionId, error: entry.lastError });
          warn(`恢复 ${sessionId} 超时：已 abort，继续下一个`);
        } else {
          // writer-held / 迁移中 / 会话已删，都属正常，记下来跳过。
          // 「agent loop is not active」「no agent factory registered」属于启动窗口的瞬态：
          // loop 还没挂完或正被配置层重载，隔几秒重试就能成功，不该记成永久失败。
          const text = errorText(result.error);
          const transient = /agent loop is not active|agent factory|not configured|loading/i.test(text);
          entry.lastError = text;
          entry.lastStack = result.error instanceof Error ? String(result.error.stack ?? '').split('\n').slice(0, 6).join('\n') : '';
          entry.at = Date.now();
          entry.attempts = (Number(entry.attempts) || 0) + 1;
          if (transient && entry.attempts < MAX_ATTEMPTS) {
            entry.status = 'retry';
            report.retried.push(sessionId);
            log(`恢复 ${sessionId} 时 loop 还没就绪（${text}），第 ${entry.attempts} 次尝试后重试`);
          } else {
            entry.status = 'failed';
            report.failed.push({ sessionId, error: text, stack: entry.lastStack });
            warn(`恢复 ${sessionId} 失败：${text}`);
          }
        }
        persist();
        // 给上一份日志留出回收时间，别让内存峰值叠加。
        // 小日志不需要整整 2.5s：大会话才等满间隔，小会话 400ms 就够 GC 转一圈。
        if (cfg.resumeGapMs > 0) {
          const big = logBytes > 4 * 1024 * 1024;
          const gap = big ? cfg.resumeGapMs : Math.min(cfg.resumeGapMs, 400);
          await new Promise((resolve) => {
            const timer = setTimeout(resolve, gap);
            if (typeof timer.unref === 'function') timer.unref();
          });
        }
      }

      restoredThisProcess = true;
      lastReport = report;
      state.lastRestoreEndAt = Date.now();
      log(`恢复完成：live=${report.live.length} alreadyLive=${report.alreadyLive.length} `
        + `healed=${report.healed.length} `
        + `armed=${report.armed.length} continued=${report.continued.length} `
        + `retry=${report.retried.length} skipped=${report.skipped.length} failed=${report.failed.length}`);
      return report;
    } finally {
      busy = false;
      state.boot = { pid: process.pid, at: Date.now(), restoring: false };
      state.phase = 'done';
      try {
        persist();
      } catch {
        /* ignore */
      }
    }
  }

  /**
   * 等真正能 resume 了再动手：
   *   1) 等 agents 服务出现；
   *   2) 等 loader 收敛（fiber 还没 active 时 resume 会得到 "agent loop is not active"）；
   *   3) 启动窗口里的瞬态失败（loop 还没挂完 / 正被重载）走梯度重试，
   *      不必把窗口期问题留给用户，也不至于无限重试。
   * 全程 fire-and-forget，绝不阻塞 host 启动。
   */
  function scheduleRestore() {
    let waited = 0;
    let retry = null;
    let retryRound = null;
    let finished = false;

    const sleep = (ms) => new Promise((resolve) => {
      const timer = setTimeout(resolve, ms);
      if (typeof timer.unref === 'function') timer.unref();
    });

    const waitForLoader = async () => {
      try {
        const loader = safeGet('loader');
        if (loader && typeof loader.await === 'function') {
          const settled = loader.await();
          if (settled && typeof settled.then === 'function') await settled;
          // loader 已经结算 = 插件树都装好了，只需让事件循环转一圈。
          // （原来固定再等 1200ms，纯属白等。）
          await sleep(150);
          return;
        }
      } catch {
        /* loader 已结算或不可用：给一点余量再走 */
      }
      await sleep(400);
    };

    /** 把还标着 retry 的会话按梯度延迟再试几轮。 */
    const scheduleRetries = () => {
      const ids = Object.entries(state.sessions)
        .filter(([, entry]) => entry !== null && typeof entry === 'object' && entry.status === 'retry')
        .map(([id]) => id);
      if (ids.length === 0) return;
      let round = 0;
      const step = () => {
        if (round >= RETRY_DELAYS.length) return;
        const delay = RETRY_DELAYS[round++];
        retryRound = setTimeout(() => {
          retryRound = null;
          restore(true, new Set(ids))
            .catch((err) => warn(`重试异常：${errorText(err)}`))
            .finally(() => {
              try {
                step();
              } catch {
                /* ignore */
              }
            });
        }, delay);
        if (typeof retryRound.unref === 'function') retryRound.unref();
      };
      log(`${ids.length} 个会话还在等 loop 就绪，按 ${RETRY_DELAYS.join('/')}ms 梯度重试`);
      step();
    };

    /** 依赖是否就绪：agents 服务在，且工作区能定下来（否则候选会选错范围）。 */
    const depsReady = () => {
      const agents = safeGet('agents');
      if (!agents || typeof agents.resume !== 'function') return false;
      if (cfg.workspacePath !== '') return true;
      try {
        return defaultWorkspacePath() !== '';
      } catch {
        return false;
      }
    };

    const run = async () => {
      if (finished) return;
      if (guard.crashes >= 2) {
        warn(`启动恢复已连续 ${guard.crashes} 次把宿主带走，本次不自动恢复；手动补跑：POST /resume-sessions/restore`);
        finished = true;
        return;
      }
      // 早恢复的关键：不等固定 8 秒，探测到依赖就绪就走（250ms 粒度）。
      if (!depsReady()) {
        waited += 250;
        if (waited > 120_000) {
          warn('等了 120s 仍没等到 agents.resume / 工作区就绪，放弃本次自动恢复');
          return;
        }
        retry = setTimeout(() => void run(), 250);
        if (typeof retry.unref === 'function') retry.unref();
        return;
      }
      if (restoredThisProcess) return;
      state.timing = {
        appliedAt,
        readyAt: Date.now(),
        waitedMs: Date.now() - appliedAt,
        // 进程启动到插件被装载花了多久（dsh 自身的启动开销，能区分是谁慢）
        bootUptimeMs: Math.round(process.uptime() * 1000),
      };
      await waitForLoader();
      // 上次恢复把进程带走时多等一会儿，避免又在同一个窗口踩到。
      if (guard.crashes > 0) await sleep(15_000);
      try {
        await restore(false);
        scheduleRetries();
      } catch (err) {
        warn(`恢复流程异常：${errorText(err)}`);
      }
      finished = true;
    };

    const first = setTimeout(() => void run(), Math.max(0, cfg.startupDelayMs));
    if (typeof first.unref === 'function') first.unref();
    return () => {
      clearTimeout(first);
      if (retry !== null) clearTimeout(retry);
      if (retryRound !== null) clearTimeout(retryRound);
    };
  }

  pruneState();
  const cancelSchedule = scheduleRestore();
  ctx.effect(() => () => {
    cancelSchedule();
    if (flushTimer !== null) {
      clearTimeout(flushTimer);
      flushTimer = null;
    }
    try {
      persist();
    } catch {
      /* ignore */
    }
    // 释放单实例锁（只有自己持有才删，别把别人的锁删了）
    try {
      const held = readJson(lockFile, null);
      if (Number(held?.pid) === process.pid) fs.rmSync(lockFile, { force: true });
    } catch {
      /* ignore */
    }
  }, 'resume-sessions: startup restore');

  // ── 4. HTTP：看状态 / 手动补跑 ─────────────────────────────────────────────

  ctx.inject?.(['webServer'], (scoped) => {
    const webServer = scoped.get?.('webServer');
    if (!webServer || typeof webServer.register !== 'function') return;

    const send = (res, code, body) => {
      res.statusCode = code;
      res.setHeader('Content-Type', 'application/json; charset=utf-8');
      res.end(JSON.stringify(body));
    };

    scoped.effect(() => {
      webServer.register({
        kind: 'exact',
        path: '/resume-sessions/state',
        handler: (req, res) => {
          const rows = Object.entries(state.sessions)
            .map(([sessionId, entry]) => ({
              sessionId,
              ...entry,
              hasOpenTurn: hasOpenTurn(sessionId),
            }))
            .sort((a, b) => (Number(b.runningAt) || Number(b.at) || 0) - (Number(a.runningAt) || Number(a.at) || 0))
            .slice(0, 60);
          send(res, 200, {
            enabled: cfg.enabled,
            mode: cfg.mode,
            workspace: cfg.workspacePath || defaultWorkspacePath(),
            stateFile: file,
            updatedAt: state.updatedAt ?? null,
            pid: state.pid ?? null,
            guard: { crashes: guard.crashes, boot: state.boot ?? null, maxSessions: cfg.maxSessions, maxSessionLogMB: cfg.maxSessionLogMB },
            phase: state.phase ?? null,
            lastRestored: state.lastRestored ?? null,
            timing: state.timing ?? null,
            restoreStartAt: state.restoreStartAt ?? null,
            total: Object.keys(state.sessions).length,
            candidates: pick(false).map(([id]) => id),
            lastReport,
            sessions: rows,
          });
        },
      });

      // 上一轮恢复的是谁 —— 给"恢复了但没跳到那个会话"这种场景一个可查的锚点。
      webServer.register({
        kind: 'exact',
        path: '/resume-sessions/last',
        handler: (req, res) => {
          send(res, 200, {
            sessionId: state.lastRestored?.sessionId ?? null,
            at: state.lastRestored?.at ?? null,
            title: state.lastRestored?.title ?? '',
            phase: state.phase ?? null,
          });
        },
      });

      webServer.register({
        kind: 'exact',
        path: '/resume-sessions/restore',        handler: async (req, res) => {
          try {
            const report = await restore(true);
            send(res, 200, { ok: true, report });
          } catch (err) {
            send(res, 500, { ok: false, error: errorText(err) });
          }
        },
      });

      log('HTTP 路由就绪：/resume-sessions/state、/resume-sessions/restore');
    });
  });
}
