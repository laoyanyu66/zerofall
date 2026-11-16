# dsh-resume-sessions

dsh（桌面端 / web 端）启动后，**只把「上次关闭前正在运行」的会话**接着跑；历史里 idle 的会话一律不碰。

## 它到底恢复谁

候选只有两类：

1. 快照里 `status === 'running'` —— 关闭那一瞬间 agent 正在跑 turn 的会话；
2. `includeGoalActive` 打开时，`goal.phase === 'active'` 的会话（goal 本来就该被续行）。

其余（`idle` 的、从没跑过的、已归档的、超过 `maxAgeHours` 没活动的）**永远不会被自动拉起来**。
手动 `POST /resume-sessions/restore` 也只重试上次没恢复成的同一批（`failed` / `timeout` / `retry`），不会把历史会话全量重开。

## 为什么需要它

- 桌面端的关闭/重启是 `Stop-Process -Force`：没有退出钩子，正在跑的 turn、已 arm 的 goal 续行全部丢；
- DSH 的安全默认是「重启后绝不自行复活工作」（`resume` 后 goal 是 `disarmed`），「继续跑」这道权限必须由插件显式补回来。

## 两段式机制

1. **运行期快照** `~/.dsh/resume-sessions.json`
   - 监听 `agent/status` / `agent/created` / `agent/disposed`；
   - 进入 `running` 的那一帧同步落盘（`.tmp` + rename 原子写），硬杀也丢不了；
   - 同时记 `cwd`、标题、goal 的 `phase/activation/id/revision`。

2. **启动期恢复**
   - 等 `agents` 服务就绪 → 等 loader 收敛 → 再开始（启动早期 resume 会拿到 `agent loop is not active`）；
   - 串行恢复，默认**一次只恢复 1 个**，两个之间隔 `resumeGapMs`；
   - 每个 `resume` 带 `AbortSignal` + 超时，超时先 abort 再放弃；
   - 恢复后：有 active goal → `goals.resume()` 重新 arm；否则只有「日志里还有未关闭的 turn」（`turnBoundary.openTurnStartSeq` 存在）才 `followup()` 一条继续指令；上次其实已收尾 → 只恢复 live，不烧 token；
   - 启动窗口的瞬态失败（`agent loop is not active` / `no agent factory registered`）按 `3s / 8s / 20s / 45s` 梯度重试，最多 4 次。

## 崩溃防护（重要）

resume 会把整份会话日志读进内存。同时拉起多个大会话曾把 host **原生**打崩（exit `0xE0000008`）。因此：

- `maxSessions` 默认 **1**；`resumeGapMs` 默认 2500；
- `maxSessionLogMB`（默认 8）：单会话日志超过该大小直接跳过，并在报告里记 `skipped`；
- 恢复开始前置 `boot.restoring = true`，正常结束清掉。若下次启动发现它仍挂着 →
  - 第 1 次：自动恢复降级为 1 个 + 延后 15s；
  - 连续第 2 次：停掉自动恢复，只保留手动接口（日志会明确说）。

## 配置

```yaml
- insert:
    - id: resume-sessions
      name: 'dsh-resume-sessions'
      config:
        enabled: true
        mode: 'continue'         # continue = 恢复并接着跑；live = 只恢复成 live
        startupDelayMs: 8000
        resumeTimeoutMs: 90000
        maxAgeHours: 48
        maxSessions: 1           # 别调大：多个大会话同时 resume 会把 host 打崩
        resumeGapMs: 2500
        maxSessionLogMB: 8
        includeGoalActive: true
        rearmGoals: true
        workspacePath: ''        # 留空 = 默认工作区
        verbose: true
```

## 观察 / 手动补跑

- `GET  http://127.0.0.1:<port>/resume-sessions/state` — 快照、候选、`guard`（崩溃计数/上次 boot）、上次恢复报告
- `POST http://127.0.0.1:<port>/resume-sessions/restore` — 手动补跑（只重试上次那批）

日志前缀 `[resume-sessions]`。

## 恢复必须挂预设（2026-11-12 修）

`agents.resume` 只做「读日志 → 建 agent」，**组合预设要自己带**。官方 API 控制器
（`dsh-api-session-controller` 的 `composeAgent`）是在 setup 里显式
`presets.resolve()` + `presets.mount(agentCtx, id)` 的；本插件早期版本漏了这一步，
于是被它恢复的 agent **整个预设平面都不存在**：`pwsh` / `read` / `write` / `glob` /
`grep` / `edit` / `skill` / `schedule` / subagent 全都不进工具目录，只剩宿主平面
（`working_directory`、`tiangong_*`、`purge_*`）。模型这一侧的表现就是不断报
`unknown tool "pwsh"`，并声称"这个会话没有 shell 工具"。

现在两处都补上了：

| 位置 | 行为 |
|---|---|
| `resumeWithBudget` | resume 时带上 `setup` → 按会话投影里的 `agentPreset` 挂预设（缺失走 `selectedDefault`） |
| 已活着的会话 | 若读不到 `composedPreset`（说明没挂上），直接 `mount` 补挂并 `emit('tools/change')`，记进报告 `healed` |

快照里每条记录多了两个字段，可直接判断"工具面到底挂上了没"：

```json
{ "preset": "unrestricted", "healedAt": 1794446351858 }
```

`preset: null` = 没挂上（工具只剩宿主平面）。历史遗留的坏会话，重启后由自动恢复/
手动补跑修掉；也可以先在 GUI 里把它关掉再打开 —— 那条路走的是带 setup 的官方 resume。

回归测试：`node tests/test_resume_preset_mount.mjs`（ZeroFall 仓库内）。

## 回滚

1. `cordis.patch.yml` 里把该 `insert` 的 `enabled` 改成 `false`（或整段删掉）；
2. 需要彻底卸载时再从 `package.json` 的 `dependencies` 删掉 `"dsh-resume-sessions"`；
3. 重启 dsh。快照文件 `~/.dsh/resume-sessions.json` 可留可删。
