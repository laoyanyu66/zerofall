# ZeroFall

**Windows 上的 DeepSeek Harness 前置守护工具** —— 在 DeepSeek 启动之前检查环境，崩溃之后把环境还原回去。

单文件 exe，不需要 Python 环境。默认形态纯本地运行，不连任何服务器，不收集任何数据。

---

## 它解决什么问题

DeepSeek Harness 的环境由一堆插件、配置、运行时组成。这套东西出问题时的典型症状是：

- 启动就崩，退出码 -2，看不出原因
- 装了某个插件之后一直白屏
- 昨天还好好的，今天就打不开了

手工排查要翻插件目录、比对文件、试着重装。ZeroFall 做的是把这些变成自动化：**定期给环境拍快照，出问题时按快照回滚**。

---

## 核心功能

| 功能 | 说明 |
|---|---|
| **定时快照** | 每 10 分钟自动打一个轻量快照（插件+配置），本地最多留 48 个 |
| **周期完整快照** | 每 30 分钟打一个完整快照（含运行时），本地最多留 12 个 |
| **变更检测** | 每 60 秒比对插件/配置指纹，检测到改动就自动打快照 |
| **每日基线快照** | 每天第一次启动后自动创建一个完整快照 |
| **崩溃自愈** | 崩溃后留现场快照 → 10 分钟倒计时 → 你确认 → 回滚并重启 |
| **环境校验** | 6 项检查：可执行文件、插件目录、MD5 一致性、未备份改动、上次退出状态、会话完整性 |
| **云备份**（可选） | 把最新健康快照加密后写入**你自己指定**的同步盘目录 |
| **诊断包导出** | 一键打包日志和配置（密钥已脱敏），用于提 Issue |
| **配置中心**（7 页签） | profile 补丁按 entry 改 config 块 / 提示词注入稿 / Agent 预设 / 备份与回滚 / 迁移与历史 / profile 健康 / 会话修复 |
| **迁移包** | 导出成 ZIP（默认剥离密钥、可设密码走 scrypt + AES-256-GCM），导入前逐项预览 diff、三种冲突策略、路径映射；支持三方合并 |
| **版本点与 tag** | 存点 / 列表 / 看差异 / 任意两点互 diff / 打 tag（带 tag 的点不会被索引截断挤掉） |
| **profile 健康诊断**（10 项） | manifest 可读性、bundle 可解析性、bundles↔dependencies、重置形状、lockfile、`file:` 联接、patch 悬空 insert、workspace 引用、storages 死路径、凭据完整度；一条 `can_restart` 判据 |
| **会话文件修复** | `session.v4.jsonl.zstd` 逐帧解析：拆单帧布局、按 seq 去重；修前必备份、修后三重校验；非 v4 只报不改 |
| **云端容器 ZFCH1** | 分块 AES-256-GCM（开销 ≈ 1.0，老的 Fernet 是 1.33），兼容老文件；带体量上限与格式迁移 |
| **故障时间线** | 每次启动成功记基线指纹，崩溃/退出/变更逐条记，看"自上次成功以来变了什么" |
| **升级预览** | 查 npm 最新版本，列出本地插件里会掉队/本地源/内建的三类 |

---

## 快速开始

1. 下载 `ZeroFall_v1.3.0_win64.zip`，解压到任意目录
2. 双击 `ZeroFall.exe`（首次会请求管理员权限，因为要读写 DeepSeek 的安装目录）
3. 首次启动会跑一遍自检，全绿后自动拉起 DeepSeek
4. 之后它会常驻托盘，每 10 分钟默默打快照，并在你改动插件后立刻补一个

**要求**：

- Windows 10 / 11（x64）
- 已安装 DeepSeek Harness
- 首次启动时程序会自动探测 DeepSeek 安装路径；探测不到就在「设置 → DeepSeek 路径」里手动指定

---

## 三种部署形态

同一个 exe，靠配置里的 `deployment_mode` 决定开机后做什么。**默认是 `standalone`**，普通人不需要改。

| 形态 | 做什么 | 适合谁 |
|---|---|---|
| **`standalone`** | 只在本机守护。不建控制通道、不起 HTTP 服务、不向任何人上报 | **默认。绝大多数用户** |
| `master` | 收子控上报的错误/崩溃，调 LLM 生成修复包，下发给子控并自更新 | 自建多机协同的维护者 |
| `agent` | 向主控上报错误/崩溃，接收更新包并自动重启 | 被主控纳管的其他机器 |

切换到 master/agent 之后，程序会开始**联网**（调 LLM、连主控）。这不是默认行为，需要你在设置页主动改，且要自己准备 API Key 或主控地址。

---

## 数据与隐私

**`standalone` 模式下：**

- 不发起任何网络连接
- 不收集、不上传任何数据
- 不开监听端口
- 所有文件只写在你指定的本地目录（默认快照目录 `<快照目录>`（默认 D 盘或程序同级 snapshots），可改）

**云备份是可选的**，而且做法是"把加密文件写进你自己填的本地目录"—— 比如百度网盘的同步空间文件夹，具体上传由网盘客户端完成。ZeroFall 本身不登录任何云服务。

**云端加密**：上传到网盘的快照用 AES 加密，密码是**每台机器首次使用时
随机生成**的（32 字节，存在本地密钥库 `secrets.json`，不进版本库）。
也可以在配置里用 `cloud_encrypt_password` 指定自己的密码，
多台机器共用同一把钥匙。

> 早期版本用的是写死在代码里的固定密码。为避免老用户的云端备份
> 变成废数据，解密时保留了对旧密码的回退 —— 解开后会提示你重新上传
> 一次，让新文件改用本机随机密码。

**诊断包**由你手动导出、手动提交。导出时所有密钥、令牌、密码类字段会被替换成 `***REDACTED***`，且不包含插件源码、会话内容、快照本体。

---

## 快照策略

```
轻量快照   每 10 分钟     本地留 48 个    仅插件 + 配置，约 25~35 MB
完整快照   每 30 分钟     本地留 12 个    含运行时，约 200~260 MB
变更快照   检测到改动时   计入轻量额度    改完静置 20 秒后拍
每日基线   每天首启 1 个
云端（可选） 每小时        留 5 轻 + 2 完整  加密后写入同步盘
```

超出上限时按时间从旧到新清理，被清掉的快照如果开了云同步，会先归档再删除本地。

恢复时可以选**任意**快照点。选轻量快照时，程序会自动以最近的完整快照为基线再叠加到那个时间点——所以恢复是精确的，不是"退回最近一个完整快照"。

---

## 崩溃恢复流程

```
DeepSeek 崩溃
  ↓ 先留一份崩溃现场快照（不覆盖任何健康快照）
  ↓ 读崩溃日志分类
      登录态损坏 → 引导清理旧登录态并重登（不回滚插件，因为回滚无效）
      其他原因   → 进入恢复流程
  ↓ 弹出恢复窗口，10 分钟倒计时
      你可以自己挑快照点，也可以什么都不做
  ↓ 倒计时结束 → 弹最终确认（默认「否」）
      确认 → 回滚 + 重启
      暂不 → 保留现场，等你手动处理
  ↓ 回滚后又崩？
      前 2 次：自动继续（换更早的快照，不会原地重试）
      第 3 次：弹框问你「还继续吗」（默认停手）
  ↓ 稳定运行 60 秒 → 计数清零
```

几个刻意的设计：

- **倒计时结束不直接回滚**，一定要你点确认。回滚会覆盖插件环境，不可逆
- 最终确认的默认按钮是**「否」**，误触回车也安全
- 连续回滚会**自动换更早的快照**，不会反复试同一个
- 回滚记录在稳定运行 60 秒后清零，说明环境真的恢复了

---

## 仓库结构

```
ZeroFall/
├── main.py              入口（单实例保护、自检、窗口调度）
├── config.py            配置模型与读写
├── logger.py            日志
├── utils.py             加密、时间、进程工具
├── build_release.py     出包（standalone / master / agent）
├── build_package.py     便携包 / 免安装包打包
├── make_icon.py         生成图标（多尺寸分级绘制）
├── modules/             核心能力
│   ├── snapshot_manager.py     快照创建/恢复/索引
│   ├── guardian.py             进程守护与崩溃捕获
│   ├── environment_checker.py  环境校验
│   ├── crash_classifier.py     崩溃日志分类
│   ├── diagnostics.py          诊断包导出
│   ├── secret_store.py         密钥自持（源码模式也认 exe 同级那份）
│   ├── acp_client.py           ACP 会话（主控自动修复用）
│   ├── master.py / control.py  主控 / 子控
│   ├── dsh_config.py           配置行级编辑（保留注释/!!js 标签）
│   ├── config_bundle.py        迁移包 / 版本点 / tag / 两点互 diff
│   ├── config_crypto.py        scrypt + AES-256-GCM 容器
│   ├── profile_health.py       profile 健康诊断 10 项 + 按建议修正
│   ├── session_repair.py       会话文件扫描/修复（依赖 tools/session_zstd.cjs）
│   ├── path_rewrite.py         恢复后旧路径改写
│   ├── secret_vault.py         本机密钥库（迁移包占位符回填）
│   ├── fault_timeline.py       故障时间线
│   ├── upgrade_preview.py      升级预览（npm 版本对比）
│   ├── cloud_crypto.py / cloud_migrate.py  云端加密容器与格式迁移
│   ├── housekeeping.py         备份残渣清理
│   └── ...
├── ui/                  界面（theme.py 是设计令牌，dialogs.py 是统一弹窗）
├── tools/               开发期辅助脚本，不参与运行时
├── tests/               测试（15 个文件 / 388 项断言，python tests/test_xxx.py 直接跑）
├── docs/
│   ├── PACKAGING.md     打包说明
│   └── archive/         历史版本文档
├── selfcheck.py         静态自检（扫 `or <数字>` 吞零、调用先于定义等陷阱）
├── rebuild_index.py     快照索引重建（索引损坏时用）
├── README.md / CHANGELOG.md / LICENSE
└── ZeroFall.ico
```

## 跑测试

```powershell
# 单个用例
python tests/test_lifecycle_health.py

# 主控/子控总验收（快，不含真实 ACP 调用和 pyinstaller）
python tests/test_all_control.py

# 全部 37 个（约 4 分钟，其中 e2e 会真的调 pyinstaller 打包）
Get-ChildItem tests -Filter "test_*.py" | ForEach-Object { python $_.FullName }

# 静态自检：不需要环境，扫一遍代码里的已知陷阱
python selfcheck.py
```

测试自带替身，跑完自动清理临时目录，不需要预先配置。
只有 `test_acp_fileops.py` 例外——它会连真实的 ACP 服务，
服务不可达时表现为超时（与环境有关，不是代码问题）。

> 测试里用到的相对路径（`Path('ui/main_window.py')` 等）依赖 cwd 是项目根。
> 所有用例在导入后都会 `chdir` 到项目根，所以从任何目录调用都可以。

## 从源码构建

```powershell
git clone <repo>
cd ZeroFall
pip install pyinstaller psutil cryptography requests
python make_icon.py            # 生成图标（可选，仓库已带）
pyinstaller --onefile --windowed --icon=ZeroFall.ico `
            --add-data "ZeroFall.ico;." --name=ZeroFall main.py
python build_release.py --mode standalone    # 出开源发布包
```

三种打包模式：

```powershell
python build_release.py --mode standalone   # 开源发布用（默认推荐）
python build_release.py --mode master       # 自建主控
python build_release.py --mode agent        # 被纳管子机
python build_release.py --mode both         # master + agent
```

---

## 关键配置

配置文件是 exe 同目录的 `zerofall_config.json`，也可以全在设置页里改。

| 字段 | 默认 | 说明 |
|---|---|---|
| `deployment_mode` | `standalone` | `standalone` / `master` / `agent` |
| `deepseek_exe_path` | 自动探测 | DeepSeek 可执行文件路径 |
| `deepseek_plugins_dir` | `~/.dsh` | 插件目录 |
| `snapshot_dir` | `<快照目录>`（默认 D 盘或程序同级 snapshots） | 快照存放目录 |
| `silent_snapshot_interval_minutes` | `5` | 轻量快照间隔（分钟） |
| `max_light_snapshots` | `10` | 本地轻量快照上限 |
| `max_full_snapshots` | `3` | 本地完整快照上限 |
| `cloud_sync_dir` | 空 | 你的同步盘目录（留空即不启用云备份） |
| `cloud_sync_encrypt` | `true` | 上传前 AES 加密 |
| `crash_restore_countdown` | `600` | 崩溃恢复倒计时（秒） |
| `auto_rollback_on_crash` | `true` | 崩溃后是否走自动回滚流程 |
| `max_auto_rollback` | `2` | 连续自动回滚几次后转为人工确认 |
| `crash_alert_enabled` | `true` | 崩溃时是否弹窗告警 |
| `pre_launch_check_enabled` | `true` | 启动时是否自动执行前置校验 |
| `auto_start_enabled` | `true` | 开机自启（写当前用户注册表 Run 项） |

---

## 已知限制

- **仅 Windows**。所有实现都基于 Win32 API（托盘、注册表、DWM、进程监控），没有跨平台计划
- **依赖 DeepSeek Harness 的目录结构**。如果 DSH 改了插件或配置的存放方式，路径探测需要跟着改
- 完整快照 150~300 MB，打快照时磁盘会有几秒 IO 波动
- 托盘图标在部分 Windows 环境需要手动固定到任务栏（程序会尝试自动设置）
- 主控模式需要一台**常开**的机器 + API Key，不适合笔记本随开随关的场景

---

## 许可

见 [LICENSE](LICENSE)。

## 问题反馈

提 Issue 时请附上诊断包：**设置 → 校验与自启 → 导出诊断包**。里面是日志和环境信息，密钥已脱敏。
