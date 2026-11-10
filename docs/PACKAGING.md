# ZeroFall v1.3.0 — DeepSeek Harness 守护与快照备份

解压即用。双击 `ZeroFall.exe` 启动（建议以管理员身份运行）。

---

## 〇、主控 / 子控 体系（v1.3.0 新增）

一套 ZeroFall 可以同时当**主控**和**子控**，通过共享目录协同。

### 角色

| 角色 | 能力 |
|---|---|
| **主控**（master） | 接收子控上报的错误 → **自动开 DSH 会话修复源码** → 重新打包 → 发布更新 → 分发给所有子控 → **自己也更新** |
| **子控**（agent） | 可选上报 ZF 错误 / DeepSeek 崩溃 → 收到更新包 → **自动应用并重启** |

### 工作流程

```
子控（某台机器）                    主控（你的机器）
     │                                  │
     │  ① 检测到错误/崩溃                │
     │──── 写入 reports/ ───────────────>│
     │                                  │ ② 读取报告
     │                                  │ ③ 拉起 DSH ACP 会话
     │                                  │    把错误详情交给 agent
     │                                  │    agent 读源码→改源码→自验
     │                                  │ ④ 检测改动（哈希比对）
     │                                  │ ⑤ pyinstaller 重新打包
     │                                  │ ⑥ 生成更新包（exe + 源码）
     │                                  │ ⑦ 发布到 updates/
     │                                  │ ⑧ 自己也替换 exe 并重启
     │                                  │
     │  ⑨ 轮询发现新版本                │
     │<─── 下载 → 校验 → 替换 → 重启 ────│
```

### 部署

**主控**（你的机器，需要源码 + DSH）：
1. `zerofall_config.json` 里 `role: "master"`
2. `master_source_dir` 指向 ZeroFall 源码目录（留空自动探测）
3. 需要 `pyinstaller`（主控要自己重新打包）

**子控**（其他机器，只需 exe）：
1. `zerofall_config.json` 里 `role: "agent"`
2. `control_channel_dir` 指向**主控和子控都能访问**的目录
3. 什么都不用装 —— 收到更新包自动换 exe

**通道目录**推荐用百度网盘同步盘（跨机器天然可达）：
```
<同步盘>\zerofall-ctl\
├── reports\<agent_id>\*.json     子控写
├── processed\*.done               主控标记已处理
├── updates\index.json             最新版本指针
├── updates\<version>.zip          更新包本体
└── applied\<agent_id>.json        子控已应用版本
```

### 子控可选项（设置页）

- `向主控提供错误/崩溃数据` — 总开关
- `包含 ZeroFall 自身报错`
- `包含 DeepSeek 崩溃数据`
- `收到更新包后自动更新并重启`

### 主控可选项

- `主控自动生成修复包`
- `主控生成更新包后也更新自己`

### 自动修复的工作方式

主控用 **DSH 的 ACP 协议**（Agent Client Protocol）驱动一个真实 DSH 会话：

```
ACP (stdio JSON-RPC)
  initialize              -> 拿能力与模型列表
  session/new             -> 用 ZeroFall 源码目录作为 cwd
  session/set_config_option -> 选模型（默认 DeepSeek-V4.1-Flash）
  session/prompt          -> 给它错误数据，让它修
```

- agent 有完整文件工具，能直接改源码并**自己验证**
- 修复前后对源码树做 **sha256 比对**，**没改动就不发包**（防止假修复）
- 全程可审计：报告原文、agent 的说明、改动文件列表都在更新包的 manifest 里

### 更新包格式

```
ZeroFall_update_<ver>.zip
├── manifest.json      版本/时间/来源子控/错误摘要/各文件 sha256
├── ZeroFall.exe       新版 exe（子控替换这个）
└── source.zip         源码快照（供主控自更新时重新打包）
```

应用时自动备份旧 exe 为 `ZeroFall.exe.bak-<版本>`，出问题可回滚。

### 注意

- 子控更新会**重启 ZeroFall**（会短暂中断守护）
- 主控修复需要本机 DSH 的 `zf-acp` profile（首次会自动创建）
- 同类错误 10 分钟内只上报一次，不刷屏

---


## 一、这个目录里有什么

```
ZeroFall_v1.2.0/
├── ZeroFall.exe            主程序（独立可执行，无需安装 Python）
├── ZeroFall.ico            图标
├── zerofall_config.json    配置（已含本机路径，首次运行会重新校准）
├── 启动 ZeroFall.bat       便利启动脚本
├── README.md               本文件
└── source/                 完整源代码（可自行修改后重新打包）
    ├── main.py
    ├── config.py
    ├── utils.py
    ├── logger.py
    ├── modules/            快照、守护、环境校验、云同步
    ├── ui/                 主窗口、托盘、6 个标签页
    ├── requirements.txt
    ├── ZeroFall.spec       PyInstaller 打包配置
    └── tools/              维护工具（重建索引、插件 effort 检测等）
```

---

## 二、核心功能

### 1. 快照生命周期（三层策略）

| 层级 | 频率 | 本地保留 | 内容 |
|---|---|---|---|
| **每日基线** | 每天首次启动 | 计入下面的完整快照额度 | 完整内容 + 运行时（当日第一个快照） |
| **轻量快照** | 每 5 分钟 | 10 个（约 26 MB/个） | 插件 + 配置 + 会话 + 运行时关键文件 |
| **完整快照** | 每 6 个轻量（约 30 分钟） | 3 个（约 109 MB/个） | 轻量全部内容 + primary-runtime（227 MB 程序运行时） |
| **云端归档** | 溢出时 | 不限 | 加密后传百度网盘同步盘 |

溢出流程：本地超限 → **最老的先加密上传** → 确认云端有副本后删本地。
次日开机：云端昨日快照收敛为 **1 轻量 + 1 完整**，其余自动清理。

#### 每日首次启动完整快照（v1.3.0 新增）

主控与子控**都生效**，默认开启（设置页「每日基线快照」可关）：

- **时机**：当天第一次启动后约 20 秒自动创建；程序跨天运行也会在下一个快照周期补上
- **内容**：`include_runtime=True`，与普通完整快照一致（含 primary-runtime）
- **去重**：状态记录在 `<快照目录>\.daily_full_state.json`；当天已有完整快照则跳过，不重复创建
- **容错**：状态文件丢失时会扫一遍快照库，发现当天已有完整快照同样跳过；创建失败不写状态，下次重试
- **审计日志**：`[每日完整快照] 开始创建 / 创建成功 / 今天已存在，跳过`

实测：`snapshot_20261108_091934`（226.9 MB，耗时 39 秒）；重启后日志正确显示「今天已创建过，本轮跳过」。

### 2. 崩溃自愈

```
DeepSeek 异常退出（进程退出码 ≠ 0 或 crash 日志）
   ↓
① 创建崩溃快照（保留现场）
② 挑一个"还没试过"的健康快照
③ 后台恢复：移走新增条目 + 同名片删后放（彻底还原）
④ pnpm install 重建依赖（约 2.5 秒）
⑤ 校验环境 → 自动重启
```

- 同一个快照不会重复试；失败自动换更早的
- 最多自动回退 2 次，之后转人工
- ZeroFall 主动关闭 DeepSeek **不记为崩溃**

### 3. 完美回退（已验证 10/10）

| 你做了什么 | 回退能否修复 |
|---|---|
| 改坏插件代码 | ✅ 还原为快照原始内容 |
| 插件里加了新文件 | ✅ 清除 |
| 删掉插件里的文件 | ✅ 恢复 |
| 改坏 cordis.patch.yml | ✅ 还原 |
| 装了新插件导致崩溃 | ✅ 移走（备份到 `_zerofall_pruned_时间戳/`） |
| dsh-runtimes 运行时损坏 | ⚠️ 需 DSH 自动重下（可重装，默认不备份） |
| 凭据 `.credentials.yaml` 丢失 | ⚠️ 需重新登录（隐私文件，默认不备份） |

保留不动：`dsh-runtimes`、`cache`、`attachments`、`node_modules`

### 4. 其他

- **环境自检**：11 项分级校验（缺失核心文件 = 失败，内容变化 = 正常演化）
- **插件管理**：启用/禁用、单插件备份、中文功能说明（完整显示不截断）
- **云同步**：百度网盘同步盘 + AES 加密（密码见 `config.py`）
- **健康报告**：每 6 小时记录快照数/健康度/占用/云端文件数
- **DSH 版本检测**：升级时自动创建完整快照作基线
- **托盘菜单**：一键回退、显示主界面、退出

---

## 三、首次使用

### 在**本机**（已装过 DeepSeek 的电脑）

1. 双击 `ZeroFall.exe`（建议右键 → 以管理员身份运行）
2. 等待 11 项自检通过（约 1 秒）
3. 主窗口出现即可用

### 在**其他电脑**（换机部署）

**直接解压运行即可**，程序会**自动探测**本机路径：

| 项目 | 探测规则 |
|---|---|
| DeepSeek 程序 | `Program Files` / `LOCALAPPDATA` / `C: D: E: F:` 常见目录 + 开始菜单快捷方式反查 |
| 插件目录 | `%USERPROFILE%\.dsh`（优先），程序目录旁 |
| runtime 目录 | `<DeepSeek目录>\resources\runtime` |
| 快照目录 | `<快照目录>`（默认 D 盘或程序同级 snapshots） → `E:\` → `C:\`，都没有则用 exe 同级 `snapshots\` |
| 云同步目录 | 各盘符的 `BaiduSyncdisk\zerofall-snapshots` |

**触发条件**：只要配置里的路径在本机失效（文件不存在 / 盘符不存在 / 用户名不同），
程序启动时会自动探测并写回配置，日志里会打印：

```
[配置自适应] 检测到路径在本机失效，已自动修正:
   deepseek_exe_path -> D:\DeepSeek\DeepSeek Harness.exe
   deepseek_plugins_dir -> <用户目录>\.dsh
   snapshot_dir -> <快照目录>
```

**如果自动探测失败**（例如 DeepSeek 装在非常规目录），手动改：
1. 打开「设置」页，逐个修正路径
2. 或直接编辑 `zerofall_config.json`
3. 改完重启 ZeroFall

> ⚠️ 前提：对方电脑**必须已安装 DeepSeek Harness**。ZeroFall 是守护/备份工具，不包含 DeepSeek 本体。
> ⚠️ 若对方没有 D 盘、也没有百度网盘同步盘，快照目录和云同步会自动回落到可用位置。

---

## 四、关键路径

| 用途 | 路径 |
|---|---|
| 快照库 | `<快照目录>\` |
| 云端同步 | `<同步盘>\zerofall-snapshots\` |
| 日志 | `D:\zerofall\logs\` |
| 被移走的多余文件 | `<快照目录>\_zerofall_pruned_<时间戳>\` |
| 单插件备份 | `<快照目录>\plugin_backups\` |

---

## 五、配置文件说明

`zerofall_config.json` 关键字段：

```jsonc
{
  // ── 路径 ──
  "deepseek_exe_path":       "D:\\DeepSeek\\DeepSeek Harness.exe",
  "deepseek_plugins_dir":    "C:\\Users\\<你>\\.dsh",
  "deepseek_config_dir":     "D:\\DeepSeek\\resources\\runtime",
  "snapshot_dir":            "D:\\dsh备份",
  "cloud_sync_dir":          "E:\\BaiduSyncdisk\\zerofall-snapshots",

  // ── 快照生命周期 ──
  "silent_snapshot_interval_minutes": 5,   // 轻量快照间隔
  "max_light_snapshots":              10,  // 本地轻量上限
  "max_full_snapshots":               3,   // 本地完整上限
  "full_snapshot_every_n_light":       6,  // 每 6 轻量 → 1 完整
  "cloud_overflow_enabled":         true,  // 溢出时云归档后删本地
  "daily_cloud_consolidate":        true,  // 次日开机收敛云端

  // ── 崩溃处理 ──
  "auto_rollback_on_crash":         true,
  "max_auto_rollback":                 2,  // 最多自动回退次数
  "auto_launch_deepseek":           true,
  "auto_exit_after_deepseek_close": true,  // 共生退出
}
```

---

## 六、重新打包（改了源码后）

```powershell
cd source
pip install -r requirements.txt
pyinstaller --onefile --windowed --icon=../ZeroFall.ico `
            --add-data "../ZeroFall.ico;." --name=ZeroFall `
            main.py --clean --noconfirm
# 产物：dist\ZeroFall.exe
```

---

## 七、维护工具（source/tools/）

| 工具 | 用途 |
|---|---|
| `rebuild_index.py` | 快照索引丢失/被覆盖时，从磁盘文件重建 |
| `test_restore_scenarios.py` | 回退逻辑回归测试（改坏/删文件/装新插件 10 个用例） |
| `detect_model_effort.js` | 从 opencode-go 提取每个模型的真实 reasoning effort 能力 |
| `apply_real_effort_map.py` | 把能力表注入 dsh-custom-header 的全局效验中间件 |
| `verify_real_map.js` | 校验 effort 修正行为（22 用例 + 全量模型） |
| `test_effort_guard.js` | effort 校验逻辑单元测试 |
| `inject_effort_guard.py` | 向 dsh-custom-header 注入全局 effort 校验 |
| `generate_icon.py` | 重新生成图标 |

---

## 八、注意事项

1. **首次运行会重建环境基线**，之后文件内容变化不再报错（只有文件丢失才报失败）
2. **不要用外部脚本操作运行中的快照库**（索引有内存副本，需重启或点「刷新」）
3. **完整快照恢复较慢**（20~40 秒，要覆盖 227 MB 运行时），UI 有进度提示不会卡死
4. **云同步依赖百度网盘客户端**：需保持 BaiduNetdisk 运行，否则只写本地同步盘不上传
5. **加密密码**在 `config.py` 的 `cloud_sync_password`，修改后旧快照无法解密

---

版本：v1.2.0
构建时间：2026-11-07
