# 更新日志

本项目的所有重要改动都记在这里。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

分类说明：**新增** 功能 / **变更** 行为改动 / **修复** bug / **移除** 删除的能力 / **安全** 安全相关。

---

## [未发布]

### 变更

- **变更点快照打完后立刻刷新静默快照的比对基准（2026-11-18 自检）**：日志实证
  `10:13:26` 变更点快照之后 `10:14:26` 又拍一份静默快照、`12:39:44` 之后
  `12:41:44` 又一份 —— 间隔 1–2 分钟、各 37 MB，内容几乎一样。
  原因是 `_last_light_fingerprint` 只在静默快照成功后更新，变更点快照打完
  基准还停在"改动前"，下一轮定时快照自然认为"内容变了"。
  新增 `_mark_light_baseline(fingerprint)`，变更点快照成功后把基准同步成
  `_create_drift_snapshot(fp)` 传进来的改动后指纹；定时静默路径也统一走它。
- **变更检测的噪声清单补齐到"改名后的账本"（2026-11-18 自检）**：当日日志四条
  `[变更检测] 差异前 6 条` 里，除了 `profiles/desktop/cordis.patch.yml`，反复出现的
  还有 `tiangong-state.json.prev`、`tianheng/state/baseline.json`、
  `media/onebot/chat-sessions.json`。这些都不是插件环境：
  · `tianheng/state/` —— 守卫改名后的账本目录，`shadow.jsonl` 每几秒追一行、
    `boots.jsonl` 每次启动写一行。旧规则只排了 `guardian/state/`，改名后等于失效，
    09:55 / 10:12 两次变更点快照都是它换来的；
  · `*state.json.prev` —— 天工账本的上一版，主文件被 `*state.json` 排掉、带 `.prev`
    的漏在指纹里；
  · `chat-sessions.json` —— 2 字节的 `{}`，DSH 每次启动重写一遍。
  三条都已进 `volatile_rules.py`：只影响指纹/MD5 判定，快照内容照样包含它们。
- **宿主"原样重写"的配置改用内容哈希判据（2026-11-18 自检）**：DSH 每次启动都会
  重写 `profiles/desktop/cordis.patch.yml`，字节一样、只有 mtime 换新，而快指纹是
  (路径, mtime, 大小) —— 等于"开一次 DSH = 一次插件改动 = 一份 15 MB 快照"。
  新增 `CONTENT_HASH_FILE_NAMES`（目前只有这一份）：命中的文件读内容算 sha1，
  内容真变了指纹才变；读不出来退回 mtime，宁可多拍一份也不漏改动。
  指纹耗时实测 127 ms/次（966 个条目），60 秒轮询无压力。
- **续跑口径对齐插件的「子会话让位」（2026-11-17）**：`dsh-resume-sessions` 新增
  `skipSubagent`（父会话也在候选里时，子会话不占名额）。会话续跑页签的
  `pending_sessions()` 现在也按同一口径过滤，并多出两个字段：`subagent`（这条是不是委派子会话）、
  `skipped_subagent`（是不是因为父会话在场而让位）。此前页签会报"要恢复 N 条"，
  插件实际只动更少的条目 —— 读数和行为对不上。
- **内置的默认配置块同步到现役**：`PATCH_BLOCK` 的 `maxSessions` 由 8 收到 2，
  并补上 `skipSubagent: true`。这一块是"profile 条目丢了就照它重新插回去"的模板，
  模板比现役弱，等于在没人看见时把防护降级。
- **内容没变的轻量快照不再产新包（2026-11-17）**：`create_snapshot()` 会给快照目录算一份
  内容指纹（相对路径 + 文件内容，排序后 sha256，记在 `_zf_content_sigs.json`），
  与上一份完全一致时直接回收临时目录并返回上一份快照。最初用的是"路径 + 大小 + mtime"，
  但现成的 55 份快照里没有任何两份包级 md5 相同 —— 相隔几分钟，mtime 全变了而内容没变，
  mtime 版几乎不会命中，所以改成按内容算。完整快照与"崩溃现场"不参与
  ——前者是定时恢复点，后者不能和省体积做交易。实测环境里 58 份 ≈ 2.68 GB。
- **自愈不再用旧 payload 覆盖本地修复（2026-11-17 实机踩到）**：换新 exe 后第一次启动，
  日志出现「已复制插件源码到 vendor/」，把 vendor 里当天补的 `sessionLineage` /
  `skipSubagent` 覆盖回 11-14 的旧版。根因是判据看 mtime —— 打包版里 payload 每轮解包到
  临时目录，mtime 永远是"刚才"，"vendor 比 payload 新"永远不成立。改成哈希对账：
  复制 payload 时把它的 sha256 记进 `vendor/.zf-payload.sha256`；vendor 与该值一致
  （没人动过）才允许升级，改过就保留并警告"要固化请放回 modules/payload 重新打包"。
  回归用例：`tests/test_resume_guard.py` 的 L 段（本地修复不被覆盖 / 原样拷贝能升级）。
- **`_atomic_write` 固定 LF（2026-11-17 实机踩到）**：Python 在 Windows 上默认把 `\n`
  翻成 `\r\n`，而 `cordis.patch.yml` 是 DSH 生成的 LF 文件。一旦整份变成 CRLF，
  `tools/switch.ps1` 的行尾断言（`^(\s+)key: (true|false|shadow)($|[ \t])`）就匹配不到
  —— 它报的是「配置里没有 radar 这一行」，看起来像配置丢了，其实是行尾。守卫 doctor
  当场由 29/0 变 28/1。写入改用 `newline="\n"`，并把现役 patch 的行尾还原（内容逐行比对无差异）。
- **`RESUME_BASELINE` 自己就是静默降级的源头（2026-11-17）**：这张"每次启动纠回阈值"的表
  里 `maxSessions` 还写着 8，于是启动 4 秒后把 profile 里刚改好的 2 又纠回 8
  （日志「续跑阈值已纠回基线：maxSessions: 2 -> 8」）。改为 2，并补 `skipSubagent: true`；
  `ensure_thresholds` 按表遍历，新键自动纳入纠正范围。
- **恢复完成后自动跑一次守卫自检（2026-11-17）**：`restore_snapshot()` 在
  `_ensure_profile_deps()` 之后调用 `_post_restore_guardian_check()`，用 DSH 自带 node 跑
  `guardian.mjs doctor` 与 `tidy`（干跑），把"共 N 项 / 通过 M · 失败 K / 待归档 X 条"写进
  恢复日志。理由：完整恢复会把 `profiles`（守卫/续跑插件的修复）和 `storages`（归档集）
  一起带回快照那天，回退到什么状态不该靠人自己想起来。
- **守卫的记账本不再被当成"插件被改动"（2026-11-17 自检）**：`deepseek_plugins_dir` 是
  整个 `.dsh`，而守卫的 `guardian/state/` 就在里面 —— `live-port.json` 每跑一次 `guardian doctor`
  刷新、`shadow.jsonl` 每跑一次追一行。于是"验一次守卫 = 一次变更判定 + 静默快照"，
  当天日志两次差异条目只有 `plugins|guardian/state/live-port.json`（21:40、21:43 各拍了一份）。
  `volatile_rules.FINGERPRINT_EXTRA_REL_PREFIXES` 补 `guardian/state/`：只影响指纹/MD5 判定，
  快照内容照样包含它们。回归用例：`tests/test_plugin_drift.py` 第 3 项新增两条断言。
- **快照失败不再留半成品（2026-11-17 自检）**：`create_snapshot()` 的 `except` 只打了个日志
  就返回，复制到一半的目录（几十 MB）和没写完的 zip 会留在快照目录里 —— 启动清理能扫到，
  但那要等下次启动；运行期间反复失败就是白占空间。现在失败时清掉这两样，但只在
  "索引里还没有这份快照"时清（索引已写入说明包是好的，报错在收尾步骤，那时删包等于丢快照）。

### 新增

- **会话帧助手 `tools/session_zstd.cjs` 增加 `head <file>`**：只解第一帧，返回
  `origin` / `parentSession` / `id` / `title`，供"子会话让位"判血缘用；解不出时返回
  `{"error":…}` 并以退出码 1 结束，调用方按"不是子会话"降级处理，不会凭空藏会话。
- **`tests/test_snapshot_identical_skip.py`**：跨快照去重的回归测试（跳过同内容轻量快照 /
  内容变了照常产包 / 完整快照不跳过 / 崩溃快照不跳过），7 项。
- **`_zf_content_sigs.json`**：快照内容指纹登记表，与 `D:\dsh备份\snapshot_index.json` 并列，
  首次生成新快照时自动出现；删快照时对应条目一并清掉。
- **崩溃「保留现场」后自动交给豆包**：dsh 崩溃且选择不回退快照（或已关自动回退、或多次回退后
  选择停手）时，自动做一次本机 GUI 接力 —— 拉起豆包 → `Ctrl+N` 新对话 → 粘贴
  「dsh又崩溃了，帮我分析一下是什么原因，并帮我修复优化一下」+ 现场信息（退出码 / 崩溃日志路径 /
  profile）→ 回车发送。任何一步失败都会把提示词留在剪贴板，手动 `Ctrl+V` 即可；
  支持 `doubao_relay_on_crash` 开关、自定义豆包路径与自定义文案。
- **会话续跑页签（融合 dsh-resume-sessions）**：dsh 崩溃或被强制重启后，把「关闭前正在跑」
  的会话接上。插件在 `agent/status` 变化的那一帧就把谁在跑写进 `~/.dsh/resume-sessions.json`；
  重启后只把 `status=running`（或有 active goal）的会话恢复成 live，日志里还有未关闭 turn
  的再补一条"继续"。idle 的历史会话永远不进候选 —— 不做"一重启全量重开"。
  ZeroFall 侧负责三件事：内置插件源码（`modules/payload/dsh-resume-sessions/`）、一键自愈
  （vendor / 依赖声明 / node_modules 链接 / patch 行 + `enabled: true`）、快照备份与还原，
  并自动找到 host 端口后直接 `POST /resume-sessions/restore`。
  插件侧自带崩溃防护：`maxSessions: 1` + 2.5s 间隔 + 8MB 单会话日志上限，外加
  「上次恢复把进程带走」的降级刹车（连续两次就停掉自动恢复，只留手动补跑）。

### 变更

- **会话续跑：修掉「重启后什么都没恢复」的根因**：插件原来在 `agent/disposed` 里直接把
  状态写成 `disposed`，而桌面端正常退出时**每个** live agent 都会收到 disposed ——
  于是"关闭前还在跑"的记录被自己抹掉，下次启动的候选永远是空的。
  现在 running 用独立布尔记录，disposed 时保留 `interrupted` 标记（只在当时确实在跑才置位），
  候选 = running / interrupted / active goal 三者之一。
  同时把快照里的标题改为从投影缓存读（`header.title` 通常是空的），并跳过从没发过 prompt
  的空白会话 —— 这类会话没有标题，恢复出来只会在侧栏显示工作区名，看着像"标题被顶掉了"。
  单次恢复上限从 1 调到 2（仍在 `maxSessions` 里可调，崩溃降级与 8MB 日志上限不变）。
- **快照页自动刷新（默认开启，5 秒）**：静默 / 变更点 / 定时完整快照全在后台线程里打，
  页面原来只在手动点「刷新」或某个前台动作之后才重建列表 —— 于是一整天明明在存快照，
  界面最上面还停在最后一次手动刷新的时刻，看起来像"程序不存快照了"。
  现在按索引文件 `(mtime, size)` 指纹判断，真变了才重建表格；重建时按 snapshot id
  还原选中行与滚动位置，不会把正在挑的那一行刷掉；模态框（恢复/删除/对比确认）打开时跳过。
- 按钮行新增「自动刷新」开关与「已同步 HH:MM:SS」时间戳，把"没有新快照"和"列表没刷新"
  两种情况区分开；开关状态写回配置。
- 后台每打完一个快照（变更点 / 定时完整 / 每日首次 / DSH 升级基线）立刻通知快照页刷新，
  不用等下一轮定时；Dashboard「快速快照」同步刷新快照页。
- 新增配置项 `snapshot_auto_refresh`（默认 `true`）、`snapshot_auto_refresh_seconds`（默认 `5`，
  夹在 2~120 秒）。

### 测试

- **回归用例跟上行为变化（2026-11-18 全量 sweep 抓到的两处红）**：
  · `test_ui_panel_smoke.py` 的"同一秒建两份快照不撞 id"原先连着建两份完全相同的
    快照，去重上线后第二份被正当地跳过，三条断言变红。改为第二份前先放一个
    `marker.txt`，验的仍是"同秒两份**不同内容**不能撞 id"。
  · `test_key_source_drift.py` 的"无 key 时不伪造"等 6 条会假失败：`load_credential_env()`
    从 `dict(os.environ)` 起底，而本机环境里确实带着 `OPENCODE_GO_API_KEY`。
    用例开始前暂存并清空所有 `*API_KEY*` / `*_TOKEN`，结束再恢复，与真实机器隔离。
- 新增 `tests/test_snapshot_autorefresh.py`（17 项）：默认开启、无变化不重建、后台新增后自动出现、
  自动刷新保住选中行、强制刷新、关掉开关即停、重开立刻补一次。
- 新增 `tests/test_snapshot_dedupe.py`（37 项）：同内容大文件只存一份、小文件与同尺寸异内容不误去重、
  恢复补齐副本且幂等、运行时垃圾（`*.pma` / `load_statistics.db*` / `relay-logs` / `SmartScreen` /
  `Crashpad` / 轮转日志）不进包、心跳文件重写不改指纹而真实插件改动仍然改指纹、两张排除表同源。

---

## [1.6.6] - 2026-11-16

### 修复（面板逐页自检抓到的）

- **插件页「启用/禁用」按钮点了必崩**：确认框文案有 3 个 `{}` 占位符却只传了
  `(name, action)` 两个实参 → `IndexError: Replacement index 2 out of range`。
  也就是说这个按钮从上线起就没成功执行过。已改为 `(action, name, action)`。
- **同一秒连建两份快照会撞 id**：`generate_snapshot_id()` 精确到秒，第二份直接
  覆盖第一份的目录/包，索引里只剩一条 —— 一份快照被静默吞掉。现在撞 id 时按秒
  往后挪到不冲突（时间戳仍保持 `snapshot_YYYYmmdd_HHMMSS` 格式）。
- **会话续跑页的"使用已运行的 DSH 端点（自动找端口）"复选框是假的**：全代码没有
  任何地方读它的值，页签永远自动探测。换成一行说明文字"端点：自动探测本机在跑的 DSH"，
  不再承诺不存在的选项。

### 自检方法（可复现）

- 静态：AST 扫 `"…".format(...)` 占位符与实参数量（全库只此 1 处不匹配）；
  AST 扫可交互控件"创建了从没被读值"（只剩上述假开关）；
  控件 → 信号连接（`QObject.receivers` 实测，排除主窗口外部接线）。
- 动态（offscreen，全在临时目录）：9 个页签逐个构造 + 只读刷新方法 + 内容断言；
  9 个侧栏页签逐个切换；只读按钮真点（刷新/校验/诊断/会话扫描）；
  设置页 54 个控件"改值→保存→落盘→重开页面回显"闭环；写操作按钮实测
  （首页快速快照、快照页新建/删除、插件页备份、崩溃恢复对话框构造）。

### 测试

- 新增 `tests/test_ui_panel_smoke.py` **10 项**（插件启停不再崩、同秒建包不撞 id、
  假开关已移除）。
- 回归 18 组全绿（含 config_center_ui 52、cloud_window 58、sessions_in_full 19）；
  `selfcheck.py` 通过 9 / 提示 0 / 失败 0。

---

## [1.6.5] - 2026-11-16

### 修复

- **`backup_session_data` 以前名存实亡**：`_should_include_sessions()` 收的是
  `include_runtime`（真实含义是"运行时版本变了没有"，未变就跳过 primary-runtime 省盘），
  于是只有"换 DSH 版本那天"的完整快照才带会话 —— 实测 **52 份快照里只有 1 份**有 `sessions/`
  （`snapshot_20261116_000355`：181 文件 / 128.41 MB 原始、126.65 MB 压缩）。
  现在判据改成"这份是不是完整快照"：**每份完整快照都带会话，轻量快照都不带**。
  新增 `create_snapshot(include_sessions=...)` 供调用方显式覆盖。

### 变更

- **带会话的完整快照按"大包"管理**：
  - 保留策略：`snapshot_is_full_grade()` = 含运行时 **或** 带会话 → 吃 `max_full_snapshots`；
    既无运行时也无会话的老"完整快照"（20-80 MB）仍按轻量口径（宽松档 96 个才真删），
    不会再出现"完整快照超出配置 21/12"这种误报。
  - 云端：大包只留最新 `cloud_full_runtime_keep` 份，且**独占那一轮窗口**（~140 MB 的会话包
    同样会把 60 分钟窗口吃满，同批轻量包顺延）。
  - 诊断：`retention_reconcile()` 的分档同步，稳态预估按实际平均体积 × 实际份数算。
- 磁盘预期：轻量 48 × ~14 MB ≈ 0.7 GB + 完整 12 × ~140 MB ≈ 1.7 GB，稳态约 2.4 GB。

### 测试

- 新增 `tests/test_sessions_in_full.py` **19 项**：完整快照（不带运行时）必须带会话、
  轻量不带、显式 `include_sessions` 优先于配置、总开关关掉后一律不带、
  会话包算"大包"、保留策略不把会话包挤进轻量配额、云端候选包含会话包。
- 回归 14 组全绿（sessions_scope 13、quota 38、dedupe 55、lifecycle 53、cloud_window 58 等）。

---

## [1.6.4] - 2026-11-16

继续收包体。拆开 1.6.3 的产物（48.11 MB 原始 / 19.67 MB 压缩）逐层看，还剩两块白占：

### 变更

- **去重阈值 256 KB → 4 KB**：256 KB 只挡住了"大块头"，包里还有 **483 个文件 / 10.11 MB**
  是完全相同的小文件（`settings.yaml.bak` 系、`.zf-config-history` 快照、`_archive` 里的重复 md、
  vendor 脚本副本……）。降到 4 KB 后单份快照去重命中 30 → 512 个；4 KB 以下仍直接复制
  （清单条目成本会超过省下的体积）。安全性不变：只有 `(size, sha256)` 完全相同才省，
  恢复路径一律先 `materialize_dedup()` 补齐副本。
- **浏览器 profile 的缓存/历史不再进包**：P3 当时把 `douyin-browser/cli-profile/` 只加进了
  **指纹**排除表，快照表漏了 —— 实测包里仍有 3.51 MB / 195 个文件的 Chromium profile。
  整棵排除又不行（Cookies / Login Data / Local Storage 是同机可用的登录态），
  于是只排"缓存 + 历史 + 预览图"，且**只在 `*-profile` 目录内生效**：
  `ShaderCache/`、`CacheStorage/`、`Shared Dictionary/`、`Collections/`、`WebStorage/`、
  `Nurturing/`、`Workspaces/`、`Sessions_Encrypted/`，以及 `History`、`Web Data`、`DIPS*`、
  `Top Sites`、`Affiliation Database`、`Network Action Predictor`、`Edge Profile Picture.png`（1 MB，
  已压缩目录里占体最大单项）。
- 新增判定 `volatile_rules.is_snapshot_junk_in_browser_profile(rel, is_dir)`：
  按路径段判断是否处在浏览器 profile 内，避免把别的插件里同名的 `History` 误伤。

### 实测（同一份真实 `.dsh`）

| 指标 | 1.6.3 | 1.6.4 |
|---|---|---|
| 快照原始体积 | 47.94 MB | **35.05 MB**（-26.9%） |
| 压缩包体积 | 20.09 MB | **14.25 MB**（-29.1%） |
| 去重命中 | 30 个 / 15.48 MB | **512 个 / 25.58 MB** |
| 打包耗时 | 4.5 s | 4.6 s（小文件算 sha256 比整份复制便宜，没有变慢） |

从最初 32.60 MB 起算，累计 **32.60 → 14.25 MB（-56.3%）**；原始 79.58 → 35.05 MB（-56.0%）。

### 测试

- `tests/test_snapshot_dedupe.py` 37 → **55 项**（新增 18）：4 KB 阈值生效但 400 B 文件仍两份都在、
  清单指向正确、补齐内容一致；profile 里 ShaderCache/History/Web Data/DIPS/预览图/Nurturing 不进包，
  Cookies / Login Data / Local State / Local Storage 保留，非 profile 目录下同名 `History` 不被误伤。

---

## [1.6.3] - 2026-11-16

自检里剩下的 P5/P6/P7，一起收掉。

### 修复

- **P5 配置单一真相**：源码模式原来读源码树的 `zerofall_config.json`，而它比 dist 那份少 9 项
  云窗口字段（`cloud_client_mode` / `cloud_window_*` / `cloud_outbox_*`），于是从源码调试时
  窗口化悄悄失效、百度网盘退回常驻。现在 `default_config_path()` 在源码模式下**优先继承
  dist 的现役配置**（`ZF_CONFIG_PATH` 环境变量可显式指定，打包运行仍是 exe 同目录），
  两份文件也已同步为同一份内容。`selfcheck.py` 的 [9] 改为按"是否真的会被读到"出结论，
  不再拿不会生效的差异报 WARN。
- **P6 六处 `or <数字>`**：`ui/main_window.py` 四处时间戳读取改用新的 `_ts0()`
  （`None`/空/非法统一转 `0.0`，不再用 `or 0` 吞值），`ui/tabs/settings_tab.py` 两处改用
  `config_int()`。`selfcheck.py` 的 [1] 现在通过。
- **P7 恢复脚本对账**：索引重建时被登记成"孤儿"的快照只写索引、不生成 `恢复_*.cmd`，
  换台没装 ZeroFall 的机器就成了死数据（实测 `snapshot_20261115_142438` 缺脚本）。
  启动清理现在在索引对账之后调 `write_restore_scripts()`（幂等，只补缺的），
  并把补写数量写进启动清理日志。
- **补 P4 的体积兜底缺陷**：验 1.6.2 时发现老索引条目没有 `runtime_included` 字段，
  代码按"≥100 MB"猜运行时，而 P1/P3 之后真完整的包也只有 ~79 MB —— 真带运行时的
  完整包会被当成"无运行时完整"，既不算进 `max_full_snapshots` 名额，云端也不会让它独占窗口。
  新增 `backfill_runtime_included()`：启动清理对"标了完整且缺字段"的条目开包扫
  `primary-runtime/`，把真实结论写回索引（幂等，包不在/坏包不猜，仍走体积兜底）。

### 变更

- `reconcile_with_disk()` 登记孤儿快照时同时判定 `runtime_included`：
  包内有 `primary-runtime` 才算真完整，只有 `.zf_full` 标记的记为"无运行时完整"（P4 口径）。

### 测试

- 新增 `tests/test_config_and_restore_scripts.py` 14 项：配置路径四种情形（源码带/不带 dist、
  `ZF_CONFIG_PATH`、打包）、本仓库实际读到的就是 dist 配置且云窗口字段齐全、
  缺脚本补齐 / 幂等 / 缺失包不写 / 孤儿脚本清理不误删。
- `tests/test_full_quota_semantics.py` 增加 7 项回填用例（31 → 38）：真带运行时的 79.6 MB 包
  判 True、跳过运行时的判 False、轻量不写字段、包不在不瞎猜、重复调用幂等。
- 回归：`retention_plan` 19、`cloud_*` 系列、`lifecycle_health` 53、`snapshot_dedupe` 37、
  `full_quota_semantics` 31 等继续全绿。

---

## [1.6.2] - 2026-11-16

这一版只做一件事：让"完整快照"的名额和它在盘上占的体积对上，并让含运行时的大包不再
堵住云同步窗口。

### 修复

- **"完整快照"名不副实，挤爆配额**：`_full_snapshot_needs_runtime()` 在 DSH 运行时没变时
  会跳过 `primary-runtime` 省盘，产物只有 20-80 MB，但索引照旧写 `is_full=True`，
  于是它们全去挤 `max_full_snapshots` 那 12 个名额 —— 2026-11-16 实测盘上 21 份"完整"
  里 19 份其实跟轻量包一样大，每天启动都报一次"完整快照超出配置 21/12"。
  现在索引新增 `runtime_included`（创建时写死，权威），`snapshot_has_runtime()` 统一判据；
  老索引没有该字段时按体积粗判（含运行时的包 165-242 MB，跳过的 20-80 MB，100 MB 为界），
  体积也读不到就保守当成真完整。
- **配额按实际内容分档**：`plan_retention()` 与 `get_overflow_snapshots()` 改成
  「含运行时的完整快照」吃 `max_full`、「不含运行时的完整快照」并入轻量口径吃 `max_light`。
  这样那些 20 MB 的"完整"不再占完整名额，溢出时走 `cloud_overflow_light_policy`
  （默认直接删本地），也不会被当成完整包往云端塞。
- **诊断报告的配额口径同步**：`retention_reconcile()` 拆成「完整（含运行时）」与
  「轻量口径（轻量 + 无运行时完整）」，`disk` 里多出 `full_degraded` 计数，
  只有真完整超出配置才升 WARNING。稳态预估也改成按"现在真有几份完整"算 ——
  按 12 × 242 MB 满额估会把 1.9 GB 说成 4.4 GB（真实快照库实测）。

### 变更

- **含运行时的完整包独占一轮云同步**（新增配置 `cloud_full_runtime_keep`，默认 1）：
  这类包 165-242 MB，一个就能把 60 分钟窗口吃满 —— 2026-11-16 07:41 实测窗口里
  只推进了这一个包，同批轻量包全被挤到下一轮。现在云端只保留最新 N 份（默认 1），
  传它那一轮让它独占，其余包顺延；更老的靠 `cloud_history_keep_days` 退休。

### 实测

- 换版前日志：08:09–08:13 三分钟连拍 3 份 79.58 MB；换版后 08:16 起基准指纹不再抖动，
  首份静默快照 48.11 MB / 压缩 20.13 MB（去重清单 30 条）。
- P4 生效点：`retention_reconcile` 在真实快照库上应把 21 份"完整"重算成
  「完整（含运行时）1-2 份 + 轻量口径」，「完整快照超出配置」这条 WARNING 消失。

### 测试

- 新增 `tests/test_full_quota_semantics.py` 31 项：判据（新字段/老索引/体积未知/dict 探针）、
  配额分档（真完整吃 max_full、无运行时吃 max_light、保护与边界）、
  溢出分桶、云端只留最新 N 份、诊断口径与告警措辞。
- 回归 10 组全绿：`retention_plan` 19、`retention_wiring` 9、`cloud_sampling` 22、
  `cloud_snapshots` 32、`cloud_upload_policy` 10、`cloud_window` 58、`daily_full` 23、
  `full_interval` 22、`lifecycle_health` 53、`snapshot_dedupe` 37。

---

## [1.6.1] - 2026-11-16

### 修复

- **静默快照退化成"雷打不动的 10 分钟一份"**：指纹把三个每分钟重写的心跳文件
  （`nightly/relay-status.json`、`guardian/state/last-boot.json`、`dsh-windows-mcp.versions.json`）算成了
  "环境被改"，于是 `_should_skip_unchanged_snapshot()` 永远命中不了"未变"。
  2026-11-15 实测：430 次变更判定 / 143 份快照 / 约 4.6 GB 写入，插件其实一次没动过。
  现在这三类心跳进 `MD5_EXCLUDE_FILES`，Chromium profile（`douyin-browser/cli-profile/`）与
  心跳日志目录（`nightly/relay-logs/`）进按前缀排除，静默快照只在内容真变了时才拍。

### 变更

- **快照内容去重**：内容完全相同、且 ≥ 256 KB 的文件只存一份，另写 `_zf_dedup.json` 清单；
  恢复时在解压后、拷贝到目标之前先把缺的副本补齐（`materialize_dedup()`），
  后续 `_prune_extra_entries` / `_restore_dir_clean` 看到的仍是完整的树。
  实测三份 `dsh-rea-plugin-9.9.9.tgz` 二进制相同（MD5 `D3A12DB579D8`，各 2,463,028 B）——
  单这一项每份快照白背 7.05 MB。实测整机一次快照去重 30 个文件、省 15.48 MB。
- **排除表统一**：新增 `modules/volatile_rules.py` 作为"哪些文件天然会变"的单一真相源，
  指纹（`environment_checker`）与快照复制（`snapshot_manager`）共用，不再各排各的。
  快照侧新排除：`*.pma`、`load_statistics.db*`、`SmartScreen/`、`BrowserMetrics/`、`Crashpad/`、
  `relay-logs/`、轮转日志（`*.log.1`、`log.txt.1` 这类后缀不是 `.log` 的漏网之鱼）。
  浏览器 `Default/`（含 Cookies）照旧保留。

### 实测

- 同一份 `.dsh`：原始体积 79.58 MB → 47.91 MB，压缩包 32.60 MB → **20.04 MB**（-38.5%）。
  复制耗时 2.3 s、打包 1.4 s，仍在单份快照 5 秒量级内。

### 测试

- 相关 9 个用例全绿（约 250 项断言）：`cloud_snapshots` 32、`cloud_window` 58、
  `lifecycle_health` 53、`plugin_drift` 22、`restore_scenarios`、`retention_plan` 19、
  `runtime_dedup` 19、`silent_dedup` 10、`snapshot_dedupe` 37。

---

## [1.6.0] - 2026-11-11  ·  [tag v1.6.0](https://github.com/laoyanyu66/zerofall/releases/tag/v1.6.0)

这一天基本都在补"事故之后才会想到"的能力：把隔壁 DSH 生态里几套工具的核心思路
（配置打包迁移、profile 健康诊断、会话文件修复、插件护栏）吃进 ZeroFall 的配置中心，
同时修掉三个会静默丢数据的坑。

### 新增

- **配置中心（7 页签）**：profile 补丁编辑（按 entry 只改 config 块，保留注释与 `!!js` 标签）、
  提示词注入稿、Agent 预设、备份与回滚、迁移与历史、profile 健康、会话修复
- **迁移包**：导出 ZIP（默认剥离密钥，可设密码走 scrypt + AES-256-GCM）、导入前逐项预览 diff、
  三种冲突策略 + 三方合并 `merge3`、路径映射
- **版本点与 tag**：`save_point` / `list_points` / `diff_point` / 任意两点 `diff_points` /
  `tag_point`；带 tag 的点不会被索引截断挤掉
- **profile 健康诊断 10 项** + `can_restart` 判据 + 「按建议修正」（摘除不可解析 bundle）
- **会话文件扫描/修复**：`session.v4.jsonl.zstd` 逐帧解析（RFC 8878 帧边界，Node 自带
  zstd 只解第一帧），拆单帧布局、按 seq 去重；修前备份、修后三重校验；非 v4 只报不改
- **云端容器 ZFCH1**：分块 AES-256-GCM（开销 ≈1.0，老 Fernet 是 1.33），兼容老 `.enc`；
  新增体量上限 `cloud_max_total_gb` 与格式迁移 `cloud_migrate`
- **本机密钥库 + 占位符回填**：迁移包里密钥写成 `__ZF_SECRET__:<槽位>`，同机导入自动回填
- **恢复后路径改写**：快照写入 `_zf_meta.json`，恢复时把旧 `$DSH_HOME`/用户目录前缀换成新值
- **故障时间线、升级预览、残渣清理**：分别解决"崩了不知道为什么"、"升级会掉哪些插件"、
  "`.bak` 只增不减"
- **每份快照挂独立恢复脚本** `恢复_*.cmd`（不装本程序也能还原）

### 修复

- **会话不进备份**：`deepseek_session_dir` 为空时不再退化成当前工作目录，而是回落
  `$DSH_HOME/sessions`；会话默认进完整快照、不进 10 分钟粒度的轻量快照（省 1.3 GB）
- **密钥库分裂**：源码模式与打包版各读一份 `secrets.json`，导致同一批云端快照出现
  两把钥匙；`base_dir()` 改为源码模式优先用 exe 同级那份，并把孤儿密码加密的包重新归队
- **云端失控**：只按天数保留会让一次事故的 10 GB 包躺满两周，补体量上限；
  云端加密改二进制容器，省 25%
- **迁移临时文件清理失败被静默吞掉**：同步盘占用时明文快照会留在会同步到云的目录里，
  现在上报 `LEFTOVERS` 并提供 `cleanup_leftovers()`

### 变更

- `max_light_snapshots` 保持 48 不变；改为「会话只进完整快照」来控体积
- README 与仓库结构同步更新；两处测试文件改名以避开"敏感文件名"误报

---

## [1.5.3] - 2026-11-11

接着 1.5.2 往下查生产日志，两条都是"没人报案、但一直在流血"的问题。

### 修复

- **一个 Unity/OneDrive 级别的 junction 就能把整次快照打死**
  本机实测（`D:\zerofall\logs\zerofall_20261111.log`）：
  ```
  08:50:42 ERROR 创建快照失败: [WinError 5] 拒绝访问。:
     'D:\dsh备份\snapshot_20261111_084102\sessions\%userprofile%\OneDrive'
  08:50:42 WARNING [每日完整快照] 创建失败
  ```
  DSH 会话目录里那个字面量 `%userprofile%` 目录，里面的 `OneDrive` 是重解析点
  （junction），读它直接被拒。`shutil.copytree` 碰到这种条目整体抛异常，
  `create_snapshot` 整个 `try/except Exception` 兜住 → 当天的每日完整快照直接没了，
  而且只留一行 error，看不出是它。
  改法（`utils.py`）：
  - `copy_directory` 不再用 copytree，改成逐项遍历：重解析点整棵跳过
    （junction/符号链接/OneDrive 占位），单个文件失败（被占用/ACL 拒绝/
    正在轮转）只跳过它，目录遍历失败跳过该子树
  - `get_directory_size` 同理，`os.walk(onerror=...)` + 逐文件 try
  - `create_archive` 逐文件兜异常，不让一个读不了的残留文件整包失败
  实测用一个指向 `C:\Windows\System32\config` 的 junction 复现：改前整棵树复制失败，
  改后 junction 被识别跳过，其余文件照常复制/统计/打包

- **前置启动校验长期停在 warning（改不掉的黄灯）**
  `MD5一致性校验` 每轮都报「有 1 个文件被删除: plugins/resume-running.json」。
  这个文件是插件"正在运行"的标记，启动时写、退出时删，天然会消失 ——
  它被写进了 MD5 基线，于是每 65 秒一次的前置校验永远是 warning，
  真正的告警反而看不清。补进 `MD5_EXCLUDE_FILES`。
  实测改前 `状态: warning`，改后 `状态: passed`

### 说明

- 1.5.2 那三项（跨机路径自愈、测试吞失败、日志污染）已随本次一起发布

---

## [1.5.2] - 2026-11-11

早上一轮自检（`selfcheck.py` + 37 个测试全跑）挖出来的问题。selfcheck 本身
0 失败，问题都在它查不到的缝隙里：测试自己吞掉失败、日志被测试污染、
换机器路径自愈只做了一半。

### 修复

- **跨机路径自愈只修一半：`.dsh` 与 `runtime` 目录永远修不掉**
  `ConfigManager.load()` 里"当前值是否有效"对 `deepseek_plugins_dir`、
  `deepseek_config_dir`、`dsh_appdata_dir` 只判 `bool(current)` —— 非空字符串
  就算有效。换机器后旧用户名路径 `C:\Users\旧名\.dsh`、已拔掉的盘符
  `Z:\NoSuchDrive\resources\runtime` 都是"非空的坏路径"，于是探测到的正确值
  永远写不进去，程序抱着坏路径继续跑。改为：两个目录必须 `is_dir()`，
  `dsh_appdata_dir` 要求盘符可用（目录本身可首次运行创建）。
  这是 `test_crossmachine.py` 一直在报的两个 FAIL。

- **`test_crossmachine.py` 无论如何都返回 0**
  断言失败只打印"最终: 存在失败"然后正常退出，批量跑测试时这个回归一直被
  当成通过 —— 上面那个缺口就是这样活下来的。现在失败即 `sys.exit(1)`。

- **`test_exit_state.py` 断言与 1.5.1 的语义相反**
  1.5.1 已把「有崩溃标记 + 当前没在运行」从 FAILED 降级成 WARNING：
  报 FAILED 会走进"环境校验失败 → 拒绝启动 DeepSeek"，而唯一的修复动作
  就是启动它，等于崩一次之后再起不来（1.5.1 注释里记着实测日志
  `15:36:48 ERROR 环境校验失败，拒绝启动DeepSeek`）。测试还在断言 FAILED，
  现在按现行语义断言 WARNING，并额外断言"不是 FAILED"。

### 变更

- **测试日志与生产日志分离**
  测试会 import 主程序模块触发日志初始化，而日志目录按 cwd 算，测试又把
  cwd 切到项目根 —— 跑一遍测试就往生产日志 `logs/zerofall_YYYYMMDD.log`
  里灌假退出码 1/2/3/9、假 sha256 不匹配、假"无可用快照"。真排障时
  噪音比线索多。现在 `logger.py` 检测到进程是 `tests/` 下的 `test_*.py`
  就落 `logs/_tests/`，生产日志只留真事件；需要指到别处用环境变量
  `ZF_LOG_DIR`。

### 说明

- 自检残留的 18 条 `or <数字>` 提示核对过：12 条是 `or 0`（0 仍是 0，无害），
  6 条是 `or` 非零默认值（`ftp_port or 21`、`http_port or 8765`、
  `log_backup_count or 5`、`control_poll_seconds or 60`）。端口 0 在界面上
  本就设不了（1024–65535），控制轮询另有 `max(15, …)` 兜底，影响面窄，未改
- 11/10 日志里那条 `找不到 node: …\.dsh\dsh-runtimes\…\node.exe` 复现不了，
  该文件当前存在

---

## [1.5.1] - 2026-11-10

上午跑了一轮实测，暴露三个会慢慢吃掉磁盘的问题，都不是崩给用户看的，
而是"静默积累"型，靠看日志和核对数字才挖出来。

### 修复（按严重程度）

- **大快照永远无法加密归档 → 磁盘只进不出**
  `encrypt_file` 用 Fernet 整体加密，把整个文件读进内存，内部还要再做
  CBC 输出和 base64 编码（+33%），峰值约为文件大小的 5 倍。实测 194 MB
  的完整快照在 4.8 GB 可用内存下直接 `MemoryError`，异常被
  `except Exception: return False` 吞掉，只留一行"加密失败"。
  后果：大快照归档失败 → 保留本地 → 重试 3 次后放弃 → 永久堆积。
  改为**分块流式加密**（8 MB 一块，每块独立 salt + Fernet），超过 64 MB
  自动走新格式，小文件保持原格式。实测 200 MB 文件加密 1.5 s、解密 1.1 s，
  内存占用恒定在 40 MB 量级。新格式用 `ZFCHUNK1` 魔数区分，
  已有的旧 `.enc` 文件照常可解（已验证 24 MB 与 182 MB 两种）

- **本地溢出归档只在启动时跑一次**
  `_sync_latest_to_cloud` / `_consolidate_cloud_daily` / `_handle_overflow_to_cloud`
  三个调度全是 `QTimer.singleShot`，开机后各跑一次就再也不跑。
  而 `_handle_overflow_to_cloud` 是本地磁盘的**唯一出口**（超限快照传云端
  再删本地）。实测跑一上午：轻量堆到 68、完整堆到 15，全都没归档，
  本地占用从 2.4 GB 涨到 4.3 GB。改为周期定时器
  （`cloud_overflow_check_minutes` 默认 10 分钟）

- **插件指纹把运行时状态当成"环境改动" → 快照暴涨**
  `quick_fingerprint()` 取 `(路径, mtime, size)` 做哈希，但没有排除
  DSH 自身的运行时状态文件：`dsh-purge/last-cwd.txt`（每次切目录就写）、
  `tiangong-state.json`、`redteam/preset-declare.json`、
  `profiles/*/cordis.yml`。结果指纹每 1~2 分钟就变一次，变更检测被打穿。
  实测 2 小时触发 **88 次**告警、打出 **49 个**快照。
  补进 `MD5_EXCLUDE_FILES` / `MD5_EXCLUDE_DIRS` 后，同样时长 **0 次**。
  另外加 `plugin_drift_cooldown_minutes`（默认 5）兜底：万一以后还有
  没预料的易活文件，最多 5 分钟一次，不至于失控

### 说明

- 那两个用**旧固定密码**加密的历史 `.enc`（182 MB / 82 MB）仍能用
  `decrypt_snapshot` 的 legacy 回退解开，数据没丢；只是 `decrypt_file`
  直接调用时不会回退，所以直接调它会报 `InvalidToken`
- crashed 快照不参与溢出判定（`get_full_snapshots` 只算 healthy），
  这是有意为之——崩溃现场保留下来供事后分析，不计入常规上限

### 修复（下午自检第二轮）

- **健康度被一条改不掉的警告长期染黄**
  `check_system_resources` 把「页面文件偏小」判成 WARNING。本机 15.7 GB
  内存只配了 1.88 GB 页面文件（12%），Windows 确实报过事件 2004 并终止过
  ZeroFall，但这是**系统配置问题**，不是 ZeroFall 的故障；而改它需要管理员
  权限，本机 UAC 又处在 `ConsentPromptBehaviorAdmin=0` +
  `PromptOnSecureDesktop=1` 的矛盾状态（既不弹窗也不提权，静默拒绝），
  用户无法自行修复。结果是一条永远消不掉的黄灯挂在界面上，把真正需要
  处理的失败项冲淡了。改为 PASSED，完整数值和建议保留在 detail 里，
  点开仍能看到「页面文件 1.88 GB（占物理 12%，偏小）」。

- **DeepSeek 崩溃后陷入「拒绝启动 → 起不来 → 继续拒绝」的死循环**
  `check_last_exit` 在「上次崩溃且当前未运行」时报 FAILED，而 FAILED 会走进
  「环境校验失败 → 拒绝启动 DeepSeek」的分支——**唯一的修复动作（启动它）
  恰恰被这条校验拦住**。实测日志：
  `15:36:44 CRITICAL DeepSeek异常退出 | 退出码: 4294967295` →
  `15:36:48 ERROR 环境校验失败，拒绝启动DeepSeek` →
  `15:36:52 DeepSeek客户端启动成功`（靠用户手动才绕过去）。
  降为 WARNING：提示「上次崩了、现在没运行」，但放行启动。启动成功后
  下一次校验会走 running 分支自动清标记，无需人工干预。

- **Chromium leveldb 轮转让整次快照失败**
  `Local Storage/leveldb` 里的 `.ldb`/`.log` 会被 Chromium 持续轮转
  （旧的删除、新的落盘），复制过程中原文件随时消失，`copytree` 抛
  `FileNotFoundError: [WinError 2] ... 000014.ldb`，被最外层的
  `except Exception` 记成 ERROR「创建快照失败」，整次快照作废：
  `15:13:32 ERROR 创建快照失败: [WinError 2] ... '000014.ldb'` →
  `15:13:32 WARNING 静默快照创建失败`。
  这不是缺陷而是 leveldb 的正常行为。改为对每个存储目录单独兜住
  `FileNotFoundError` / `OSError`，已复制的部分照样保留，不再牵连主快照。

---

## [1.5.0] - 2026-11-10

### 变更
- **测试代码归入 `tests/`**：37 个 `test_*.py` 从仓库根目录移出，
  根目录只剩 8 个入口/工具脚本。所有测试的 `sys.path` 基准、
  `HERE`/`ROOT` 变量、以及引用 `ui/`、`modules/`、`ZeroFall.ico`
  的相对路径都已修正；统一在导入后 `chdir` 到项目根，
  保证 `Path('ui/main_window.py')` 这类写法仍可用

### 优化
- **快照打包按文件类型分流压缩**：`.zstd / .tgz / .zip / .png / .whl / .enc`
  等本来就压缩过的格式改用 `ZIP_STORED` 原样存储。实测一个完整快照里
  这类数据占约三成，压它们只是白烧 CPU。对照测试（49.9 MB 混合内容）：
  耗时 **0.75s → 0.27s（-64%）**，包体大小 **27.9 MB → 27.9 MB（不变）**
- 崩溃快照的状态列补充 tooltip，说明"创建时 DeepSeek 未运行、不参与恢复链、
  保留供事后分析"，避免用户点了才发现回不去

### 修复
- **`build_release.py --mode both` 漏了 standalone**：原来只出 master + agent，
  开源的默认形态反而不在其中。现在 both = 三个全出
- **版本号散落 5 处且容易漏改**：升到 1.5.0 时界面 `QLabel` 和 User-Agent
  各漏了一次，靠人工比对才发现。已在 `selfcheck.py` 加第 8 项检查，
  扫出 `modules/__init__.py`、`build_release.py`、`main.py`、
  `ui/main_window.py`、`modules/net_transport.py` 五处的版本号，不一致即提示
- **最小化到托盘时没有任何提示**
  刚加「可选最小化到托盘」就发现：点 X 后窗口消失、日志写着
  `[关闭] 托盘不支持气泡提示，跳过` —— 因为 `NativeTrayIcon` 从来没实现过
  `show_message`。用户会以为程序被关掉了。
  补上 `show_message(title, text, level)`，走 `NIF_INFO` + `Shell_NotifyIconW`。
  实现时踩到一个坑：`shell32` 在该模块里是**每个方法内部各自取的局部变量**，
  模块级根本没有这个名字，直接引用会抛 `AttributeError`，而异常被
  `except` 吞掉后只表现为"返回 False"，查了半天。
  现在失败会打 WARNING 并带上 GetLastError

- **点 X 直接退出，没有"最小化到托盘"这个选项**
  QQ / 微信那套习惯：关窗口不等于退程序。ZeroFall 原来点 X 就直接走退出流程
  （`setQuitOnLastWindowClosed(False)` 只管住"窗口关了进程还在"，但
  `closeEvent` 自己把退出做完了）。
  新增 `close_action` 三档：`ask`（每次弹窗，可勾"记住我的选择"）/
  `minimize`（直接缩托盘，继续守护）/ `exit`（直接退出）。
  设置页「启动行为」里可改。最小化时首次会尝试气泡提示，
  托盘不支持气泡则只记日志，不会让人以为程序没了

- **托盘图标看着像"只剩一个青色 C"，和桌面那个方块不像**
  查下来不是加载问题：`build_simple()` 给 16/24px 重画了简化版，
  深色圆角底 (12,18,30) 明明画上了（不透明像素 95.7%、中心是亮白玉）。
  问题是两层：**深色底在深色任务栏上和背景融成一片**，
  而且简化版把**构图也改了**（完整版是双环 + 小中心点，简化版变成单环 +
  大圆点），等于托盘和桌面是两个不同的图标。
  现在：加一圈亮青描边把方块轮廓勾出来（alpha 170），并且**保留完整版的
  双环结构**（外环 0.320/0.092、内环 0.178/0.055、中心点 0.072），
  只加粗线条、去掉辉光和渐变球。对比过三种画法
  （单环简化 / 完整版直接缩 / 本方案），完整版直接缩会因渐变被平均而发暗，
  单环版构图对不上，本方案在 16px 下仍能认出"双环 + 中心点"

- **进程突然消失但查不到原因**
  实测一上午发生两次：ZeroFall 无声无息没了，日志停在最后一行托盘图标，
  既没有"优雅退出"记录、也没有崩溃日志，`last_exit_code` 还是 0。
  翻 Windows 事件日志才找到真相——事件 2004：
  「Windows 成功诊断出虚拟内存不足的情况……ZeroFall.exe 使用了大部分虚拟内存」。
  本机物理内存 15.7 GB，页面文件只有 1.88 GB（占物理 12%），
  ZeroFall 常驻约 200 MB 且长期运行，在内存吃紧时最容易被系统挑中终止。
  新增环境检查项「系统资源」：页面文件 < 2 GB 或占物理 < 15% 时报 WARNING，
  并说明"进程突然消失不一定是程序的问题"。只警告不阻止启动——
  这是系统配置问题，不该挡住用户干活

- **崩溃判定把你正常快照也标成崩溃**
  `create_snapshot` 原来只看"创建那一刻 DeepSeek 在不在跑"：
  `status = HEALTHY if deepseek_proc else CRASHED`。但"没在跑"有三种情况，
  只有一种是真崩溃，另外两种是正常快照：
    · 用户手动「快速快照」时没开 DeepSeek
    · 环境校验失败、DeepSeek 拒绝启动期间的定时静默快照
    · 用户就是想关掉它再打快照
  这些快照内容完全正常，却被标成 crashed —— 而 crashed 不参与恢复链
  （`find_overlay_chain` 只取 HEALTHY），等于白白损失一个可用恢复点，
  还会占满 `max_crashed_snapshots` 名额。
  改成以 `notes` 为准：只有 guardian / 崩溃处理逻辑显式创建、
  并在 notes 里写明"崩溃"的才算崩溃现场，其余一律 HEALTHY。
  同时加 `repair_misjudged_crashed()`，启动时把历史上误标的改回来
  （只改标签不动文件）。实测修正 1 个，修正后它成功进入恢复链

- **崩溃快照永久占本地磁盘**
  `get_light_snapshots()` / `get_full_snapshots()` 都硬编码 `status == HEALTHY`，
  而 `get_overflow_snapshots()` 只从这两个列表取溢出——crashed 快照既不算
  轻量也不算完整，两个上限都管不到，只能永久留着。实测 3 个占了 168 MB。
  成因更值得说：状态只看"创建时 DeepSeek 在不在跑"
  （`HEALTHY if deepseek_proc else CRASHED`）。环境校验不通过时 DeepSeek
  会拒绝启动，那段时间每个静默快照都会被判成 crashed，而且永远不清理——
  等于磁盘无限涨。
  现在给崩溃现场单独一条上限 `max_crashed_snapshots`（默认 3），
  超出部分先归档云端再删本地，本地留最近几个供事后分析。
  `get_overflow_snapshots()` 相应改为返回 (轻量, 完整, 崩溃) 三元组

- **改了排除规则后健康度持续红**
  MD5 基线是某次 `update_baseline()` 按当时的排除规则生成的，之后往
  `MD5_EXCLUDE_FILES` 里加新条目，旧基线不会自动更新——里面仍存着
  那些易变文件的哈希。于是每次对比都判"不一致"，健康度永远红，
  还会 `拒绝启动DeepSeek`。实测踩到过一次：10:46~11:07 连续 failed，
  直到某次启动顺带重建基线才自己好。
  现在 `run_full_check()` 会先扫一遍基线是否含"现已排除"的条目，
  有就静默重建（代价约 100~200 ms，只在真检测到残留时才做）

- **孤儿完整快照被误判为轻量**：`adopt_orphans` 用
  `n.startswith('primary-runtime')` 判断，但归档内真实路径是
  `config/primary-runtime/...`，判据永远为假。索引损坏后重新登记孤儿包时，
  完整快照会被当轻量登记，进而按 48 个轻量上限被清理掉。
  改为按路径段匹配后，与索引记录的 11 个完整快照完全一致
- 设置页「同步目录」默认值与「通道目录」placeholder 写死了开发者本机路径，
  别人机器上没有那个盘；改为空值 + 通用提示
- `test_cloud_snapshots.py` 硬编码真实云目录，改为读环境变量 / 部署配置
- 文档里的示例路径统一脱敏为 `<同步盘>\...`

---

## [1.4.1] - 2026-11-10

### 修复
- **认证态备份刷屏**：`Network/Cookies` 被 Chromium 以完全不共享的句柄持有，
  实测 `CreateFileW` 带 `FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE`
  任意组合仍返回 `error 32`，普通进程无法读取。原实现每次快照都记一条
  WARNING（实测一天 138 条，占全部 WARNING 的 60%），把真正的错误淹没了。
  现按 `winerror == 32` 判定为预期行为并降到 DEBUG，其他异常仍然 WARNING；
  日志里会写明跳过了哪些文件。
  影响面很小：登录态主要由 `Local Storage`（Electron 把 token 放那儿）承载，
  实测认证态备份仍能完整拿到 16 个文件 / 147.7 KB
- `selfcheck.py` 的 `or <数字>` 检查没跳过文档字符串，把用反引号包裹的示例代码
  当成真代码报了出来（`ui/main_window.py:2384`）

### 说明
- 顺带核对全部 18 处 `or <数字>` 用法，均安全：6 处是刻意让 `0` 成为合法值
  （禁用某功能），3 处端口默认值不会取 0，`stride` 有 `if stride < 1` 兜底

---

## [1.4.0] - 2026-11-09

### 新增
- **插件变更检测**：每 60 秒比对插件/配置目录指纹（只 stat，约 43 ms），
  检测到改动后静置 20 秒自动打快照。解决"坏改动被拍进快照后回退也无效"的问题
- 完整快照支持按时间触发：以当天首个完整快照为锚点，每 30 分钟一个
- 云端历史保留期（默认 14 天），解决云端无限增长
- 云端上传抽样：轻量每 12 个传 1 个、完整每 4 个传 1 个

### 变更
- 轻量快照间隔 5 → 10 分钟，上限 10 → 48 个
- 完整快照上限 3 → 12 个
- 云端「按数量保留」改为「按天数保留」，删除 `cloud_sync_keep` / `cloud_sync_keep_full`

### 修复
- **轻量快照体积过大**：`plugins/sessions` 等会话数据被误打包，单个快照
  126 MB 里有 94 MB 是会话记录。排除后降到约 32 MB（省 75%）
- 设置页「最多保留」错绑已废弃字段 `max_healthy_snapshots`，改了无效果；
  轻量/完整上限在界面上无处可改
- 设置页说明文字与输入框重叠
- `int(x or 20)` 吞掉合法的 0（`plugin_drift_settle_seconds` 设 0 不生效）
- 云端快照页「按保留策略清理」按已删除的字段清理，改用保留天数语义
- `tools/` 下脚本的模块路径基准错误，`import ui` 失败
- 移除废弃字段 `max_healthy_snapshots`

### 移除
- 旧备份目录 `_backup_*`（并加入 .gitignore）

## [1.3.0] - 2026-11-09

这一版是最大的一次改动：从"单机守护工具"变成了支持多机协同的系统，同时把界面整套重做。

### 新增

- **部署形态三档**：`standalone`（默认，纯本地）/ `master`（主控）/ `agent`（子控）。
  开源分发的默认形态不连任何服务器、不上报任何数据
- **主控 / 子控体系**：子控上报错误与崩溃，主控调 LLM 分析并生成修复包，下发给子控自动升级，主控自身同步更新
- **模型路由与记忆**：候选模型按"实测可用性"排序，失败类型细分（余额不足 / 地区限制 / 模型下线 / 认证失败 / 限流），避免反复试已知不可用的模型
- **凭据自持**：API Key 存进自己的密钥库，`.credentials.yaml` 被 DSH 重写也不会失效；支持 `<KEY>.txt` 文件投放
- **内建 HTTP 更新服务器**：子控可直连拉取更新包，不必等网盘同步
- **跨网中转通道**：WebDAV / S3 / FTP 三种，用于主控在 NAT 后方的分发场景
- **云端快照浏览页**：以 `.meta.json` 为准列出同步盘里的快照，可解密导入、删除、清理孤儿元数据、按保留策略清理
- **导出诊断包**：一键打包日志、配置（密钥脱敏）、插件清单、环境校验结果，用于提交 Issue
- **全新界面**：深色侧边导航、渐变顶栏、卡片式布局、统一设计令牌；原生标题栏同步深色
- **统一风格对话框**：替换系统弹窗，支持状态徽标、可拖动、自适应高度
- **应用图标重设计**：扫描环 + 能量核心，7 个尺寸分级绘制
- **开机自启开关**（写当前用户注册表 Run 项）
- **云同步一致性校验**：上传后核对云端文件大小与头部完整性
- `test_exit_state` / `test_crash_report_once` / `test_report_ratelimit` /
  `test_deployment_mode` / `test_crash_dialog` / `test_rollback_manual` /
  `test_dialogs` / `test_settings_effective` / `test_cloud_snapshots` /
  `test_model_memory` 等测试套件

### 变更

- **崩溃恢复倒计时 60 秒 → 10 分钟**，给用户充足判断时间
- **倒计时结束不再直接回滚**，改为先弹最终确认（默认「否」，误触安全）
- **连续自动回滚上限改为人工确认**：超过 `max_auto_rollback` 次不再硬性停止，而是问用户是否继续
- **回滚会换更早的快照**：记录已试过的快照点，避免反复重试同一个
- **崩溃上报去重**：用事件序号保证同一崩溃只上报一次，跨重启也有效
- **上报限流按类型分层**：`dsh_crash` 不再受 10 分钟限制（去重已由序号保证），`zf_error` 保留
- **"上次退出状态"降级**：历史崩溃但当前已恢复时只报警告并清除标记，不再永久污染健康度
- **前置校验开关只拦自动流程**：关掉后手动校验仍可执行
- 快照 ID 完整显示（原来截断到 18 字符，导致一列快照看起来一样）
- 表格列宽按内容自适应，长字段不再被截成 `xxx...`
- 设置页所有开关纳入滚动容器（原来会把窗口高度顶到 2100px）

### 修复

- 无健康快照时崩溃对话框抛 `AttributeError: '_ok_btn'`
- 健康度刷新引用未定义变量导致检测静默失败
- 托盘图标注册失败刷 ERROR（`NIS_FORCEINLINE` 在 Win10/11 是预留标志）
- 云同步统计把孤儿元数据算进占用，`last_sync` 取错时间
- 云端快照计数与占用统计和磁盘实际不符（把孤儿元数据也算进去了）
- e2e 测试在缺 `ZF_DATA_DIR` 时静默失败 10 项，改为明确提示
- 模型路由测试受真实记忆文件影响而结果漂移

### 移除

- `orchestratorx_enabled` / `orchestratorx_sync_on_restore`（目标程序已不存在，零引用）
- `baidu_pan_auto_sync`（无调度器实现，且百度网盘默认关闭）

### 安全

- 诊断包强制脱敏：所有 key/token/secret/password 类字段（含嵌套结构）替换成 `***REDACTED***`
- 开源默认形态（standalone）不监听端口、不发起网络连接、不收集数据
- 老配置文件按原 `role` 迁移部署形态，不会把在用的主控静默降级

---

## [1.2.0] - 2026-11-07

第一版"能备份到云、能上报崩溃"的版本。

### 新增

- **云同步**：把最新健康快照复制到百度网盘同步目录，支持 AES 加密后上传
- **云端保留策略**：轻量 / 完整快照分别限制数量，超出按时间清理
- **崩溃上报**：崩溃时生成报告（退出码、日志尾部、插件快照信息）
- **通知模块**：崩溃、恢复、快照结果的状态提示
- **环境校验器**：MD5 一致性、插件目录、会话完整性等检查项
- 插件管理页、环境校验页、云端同步页

---

## [1.1.0] - 2026-11-06

### 新增

- **启动自检进度条**：11 项检查实时显示进度与百分比，全绿后自动进主界面
- **原生托盘图标**：用 Win32 API 替代 Qt 托盘，支持右键菜单、双击唤出、尝试固定到任务栏
- 深色主题、渐变进度条、状态色区分

### 技术

- 新增 `ui/loading_overlay.py`（自检工作线程 + 覆盖层）
- 新增 `ui/native_tray.py`（原生托盘）
- 用 `QStackedWidget` 管理加载页与主界面切换，异步自检不阻塞 UI

---

## [1.0.0] - 2026-11-06

首个可用版本。

### 新增

- 定时轻量快照（插件 + 配置）
- 周期完整快照（含运行时）
- 快照恢复（完整基线 + 轻量叠加）
- DeepSeek 进程守护：退出后按策略重启
- 基础界面：仪表盘、快照管理、设置

---

## 版本号约定

| 变更 | 版本号 |
|---|---|
| 只修 bug，不改行为 | `1.3.0 → 1.3.1` |
| 加功能，向后兼容 | `1.3.0 → 1.4.0` |
| 改配置格式或破坏兼容 | `1.3.0 → 2.0.0` |

---

## 升级注意

**从 1.2 及更早升到 1.3：**

- 配置文件会自动补齐 `deployment_mode`。老配置里的 `role` 会被沿用，**已经在跑主控的机器不会被降级**
- 崩溃恢复倒计时从 60 秒变成 600 秒。如果你习惯原来的一分钟，改 `crash_restore_countdown`
- 倒计时结束后会多一次确认弹窗。这是有意加的，不是故障
- 界面结构改了（侧边导航替代横排页签），设置项位置有调整，但配置字段名不变

**卸载：**

- 程序不写系统目录，删掉 exe 和同目录的配置、日志、`secrets.json` 即可
- 如果开过开机自启，先关掉开关（或手动删注册表 `HKCU\...\Run\ZeroFall`）
- 快照目录（默认 `<快照目录>`（默认 D 盘或程序同级 snapshots））需要你自己删
