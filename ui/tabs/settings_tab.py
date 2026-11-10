# -*- coding: utf-8 -*-
"""
Settings Tab - 设置标签页
"""
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QGroupBox,
    QGridLayout, QFormLayout, QCheckBox, QLineEdit, QFileDialog,
    QMessageBox, QComboBox, QSpinBox
)
# 统一风格对话框：外观与主界面同源，调用形状与 QMessageBox 一致
from ui import theme as th
from ui.dialogs import ZFMessageBox as QMessageBox


class SettingsTab(QWidget):
    """设置标签页"""
    
    def __init__(self, config, config_manager, parent=None):
        super().__init__(parent)
        self.config = config
        self.config_manager = config_manager
        self.parent_window = parent
        
        self._init_ui()
    
    def _init_ui(self):
        """初始化用户界面"""
        layout = QVBoxLayout(self)
        
        path_group = QGroupBox("DeepSeek路径")
        path_layout = QFormLayout()
        
        self.exe_path_edit = QLineEdit(self.config.deepseek_exe_path)
        browse_exe_btn = QPushButton("浏览...")
        browse_exe_btn.clicked.connect(lambda: self._browse_file(self.exe_path_edit))
        exe_layout = QHBoxLayout()
        exe_layout.addWidget(self.exe_path_edit)
        exe_layout.addWidget(browse_exe_btn)
        path_layout.addRow("可执行文件:", exe_layout)
        
        self.plugins_dir_edit = QLineEdit(self.config.deepseek_plugins_dir)
        browse_plugins_btn = QPushButton("浏览...")
        browse_plugins_btn.clicked.connect(lambda: self._browse_dir(self.plugins_dir_edit))
        plugins_layout = QHBoxLayout()
        plugins_layout.addWidget(self.plugins_dir_edit)
        plugins_layout.addWidget(browse_plugins_btn)
        path_layout.addRow("插件目录:", plugins_layout)
        
        path_group.setLayout(path_layout)
        layout.addWidget(path_group)
        
        backup_group = QGroupBox("备份内容")
        backup_layout = QGridLayout()
        
        self.backup_plugins_check = QCheckBox("备份插件目录")
        self.backup_plugins_check.setChecked(self.config.backup_plugins)
        backup_layout.addWidget(self.backup_plugins_check, 0, 0)
        
        self.backup_config_check = QCheckBox("备份插件配置")
        self.backup_config_check.setChecked(self.config.backup_plugin_config)
        backup_layout.addWidget(self.backup_config_check, 0, 1)
        
        self.backup_session_check = QCheckBox("备份会话数据")
        self.backup_session_check.setChecked(self.config.backup_session_data)
        backup_layout.addWidget(self.backup_session_check, 1, 0)
        
        self.backup_keys_check = QCheckBox("备份密钥配置（不推荐）")
        self.backup_keys_check.setChecked(self.config.backup_keys)
        backup_layout.addWidget(self.backup_keys_check, 1, 1)
        
        # 定时快照间隔
        backup_layout.addWidget(QLabel("定时静默快照间隔:"), 2, 0)
        interval_row = QHBoxLayout()
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 1440)
        self.interval_spin.setSuffix(" 分钟")
        self.interval_spin.setValue(getattr(self.config, 'silent_snapshot_interval_minutes', 5))
        interval_row.addWidget(self.interval_spin)
        
        # 绑定真正生效的轻量上限（旧版错绑到已废弃字段，改了没效果）
        self.max_snap_spin = QSpinBox()
        self.max_snap_spin.setRange(3, 200)
        self.max_snap_spin.setSuffix(" 个")
        self.max_snap_spin.setValue(getattr(self.config, 'max_light_snapshots', 24))
        self.max_snap_spin.setToolTip(
            "轻量快照最多保留几个（含插件+配置，单个约 60~150 MB）。\n"
            "24 个 × 5 分钟 ≈ 覆盖 2 小时。")
        interval_row.addWidget(QLabel("  轻量最多保留"))
        interval_row.addWidget(self.max_snap_spin)
        interval_row.addStretch()
        backup_layout.addLayout(interval_row, 2, 1)
        
        self.drift_check = QCheckBox("检测到插件/配置变更时自动创建快照")
        self.drift_check.setChecked(
            getattr(self.config, "plugin_drift_snapshot", True))
        self.drift_check.setToolTip(
            "每 60 秒比对一次插件目录指纹（只用文件属性，很轻）。\n"
            "检测到改动后静置 20 秒再拍快照，避免装插件过程中打一串废快照。\n\n"
            "这比定时快照更有用：定时快照可能把坏改动也拍进去，\n"
            "导致回退到的点本身就带着问题。")
        backup_layout.addWidget(self.drift_check, 3, 0, 1, 2)

        backup_group.setLayout(backup_layout)
        layout.addWidget(backup_group)
        
        # ── 每日首个完整快照 ──
        daily_group = QGroupBox("每日基线快照")
        daily_layout = QGridLayout()
        
        self.daily_full_check = QCheckBox("每天第一次启动后自动创建一个完整快照（含运行时）")
        self.daily_full_check.setChecked(getattr(self.config, 'daily_full_snapshot', True))
        daily_layout.addWidget(self.daily_full_check, 0, 0, 1, 2)

        # 定时完整快照间隔：0 = 关闭（退回"每 N 个轻量触发"）
        daily_layout.addWidget(QLabel("定时完整快照间隔:"), 1, 0)
        self.full_interval_spin = QSpinBox()
        self.full_interval_spin.setRange(0, 720)
        self.full_interval_spin.setSuffix(" 分钟")
        self.full_interval_spin.setSingleStep(10)
        self.full_interval_spin.setMaximumWidth(140)
        self.full_interval_spin.setValue(
            int(getattr(self.config, 'full_snapshot_interval_minutes', 30) or 0))
        self.full_interval_spin.setToolTip(
            "以当天第一个完整快照为锚点，之后每隔这么多分钟再打一个。\n"
            "填 0 则关闭定时触发，改回\"每 N 个轻量快照整一个完整快照\"。\n"
            "注意：完整快照含运行时，单个 150~300 MB，间隔太短会很吃磁盘。")
        daily_layout.addWidget(self.full_interval_spin, 1, 1)
        self.full_interval_spin.valueChanged.connect(self._refresh_full_hint)

        # 完整快照上限（原来只在配置里，界面上改不了）
        daily_layout.addWidget(QLabel("完整最多保留:"), 2, 0)
        self.full_max_spin = QSpinBox()
        self.full_max_spin.setRange(1, 100)
        self.full_max_spin.setSuffix(" 个")
        self.full_max_spin.setMaximumWidth(140)
        self.full_max_spin.setValue(getattr(self.config, 'max_full_snapshots', 12))
        self.full_max_spin.setToolTip(
            "完整快照最多保留几个（含运行时，单个约 150~300 MB）。\n"
            "12 个 × 30 分钟 ≈ 覆盖 6 小时。")
        self.full_max_spin.valueChanged.connect(self._refresh_full_hint)
        daily_layout.addWidget(self.full_max_spin, 2, 1)

        self.full_limit_label = QLabel()
        self.full_limit_label.setObjectName('dim')
        daily_layout.addWidget(self.full_limit_label, 3, 0, 1, 2)
        self._refresh_full_hint()

        
        daily_hint = QLabel(
            "主控与子控都生效。当天第一次启动（或程序跨天后第一次进入快照周期）时自动补齐；"
            "当天已有完整快照则跳过，不会重复创建。")
        daily_hint.setWordWrap(True)
        daily_hint.setStyleSheet("color: gray; font-size: 11px;")
        daily_layout.addWidget(daily_hint, 4, 0, 1, 2)
        
        daily_group.setLayout(daily_layout)
        layout.addWidget(daily_group)
        
        # ── 云端同步（同步盘方案）──
        cloud_group = QGroupBox("云端同步（百度网盘同步盘）")
        cloud_layout = QGridLayout()
        
        self.cloud_sync_check = QCheckBox("启用云端同步（把最新健康快照复制到同步盘）")
        self.cloud_sync_check.setChecked(getattr(self.config, "cloud_sync_enabled", True))
        cloud_layout.addWidget(self.cloud_sync_check, 0, 0, 1, 2)
        
        cloud_layout.addWidget(QLabel("同步目录:"), 1, 0)
        self.cloud_dir_edit = QLineEdit(getattr(self.config, "cloud_sync_dir", "") or "")
        self.cloud_dir_edit.setPlaceholderText(
            r"选一个网盘同步目录，例如 <同步盘>\zerofall-snapshots")
        browse_cloud_btn = QPushButton("浏览...")
        browse_cloud_btn.clicked.connect(lambda: self._browse_dir(self.cloud_dir_edit))
        cloud_dir_row = QHBoxLayout()
        cloud_dir_row.addWidget(self.cloud_dir_edit)
        cloud_dir_row.addWidget(browse_cloud_btn)
        cloud_layout.addLayout(cloud_dir_row, 1, 1)
        
        cloud_layout.addWidget(QLabel("同步间隔:"), 2, 0)
        cloud_row = QHBoxLayout()
        self.cloud_interval_spin = QSpinBox()
        self.cloud_interval_spin.setRange(5, 1440)
        self.cloud_interval_spin.setSuffix(" 分钟")
        self.cloud_interval_spin.setValue(getattr(self.config, "cloud_sync_interval_minutes", 60))
        cloud_row.addWidget(self.cloud_interval_spin)
        
        # 云端不再按"保留几个"管理（上传抽样 + 次日收敛 + 保留期已接管），
        # 这里只留一个"历史保留天数"，它是云端唯一的容量闸门。
        self.cloud_history_spin = QSpinBox()
        self.cloud_history_spin.setRange(0, 365)
        self.cloud_history_spin.setSuffix(" 天")
        self.cloud_history_spin.setMaximumWidth(140)
        self.cloud_history_spin.setValue(
            int(getattr(self.config, "cloud_history_keep_days", 14) or 0))
        self.cloud_history_spin.setToolTip(
            "云端只保留最近这么多天的整组快照，超期自动清理。\n"
            "填 0 = 不限制（云端会一直增长，不建议）。")
        cloud_row.addWidget(QLabel("  历史保留"))
        cloud_row.addWidget(self.cloud_history_spin)
        cloud_row.addStretch()
        cloud_layout.addLayout(cloud_row, 2, 1)
        
        cloud_hint = QLabel("提示：需保持百度网盘客户端运行，文件才会自动上传到云端")
        cloud_hint.setStyleSheet("color: #888; font-size: 11px;")
        cloud_layout.addWidget(cloud_hint, 3, 0, 1, 2)
        
        self.cloud_encrypt_check = QCheckBox("上传前 AES 加密（恢复时需用 ZeroFall 解密）")
        self.cloud_encrypt_check.setChecked(getattr(self.config, "cloud_sync_encrypt", False))
        cloud_layout.addWidget(self.cloud_encrypt_check, 4, 0, 1, 2)
        
        cloud_group.setLayout(cloud_layout)
        layout.addWidget(cloud_group)
        
        # 主题设置
        theme_group = QGroupBox("界面主题")
        theme_layout = QHBoxLayout()
        
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["深色", "浅色"])
        # 根据当前主题设置默认值
        current_theme = getattr(self.config, 'theme', 'dark')
        self.theme_combo.setCurrentIndex(0 if current_theme == "dark" else 1)
        self.theme_combo.currentTextChanged.connect(self._on_theme_changed)
        theme_layout.addWidget(QLabel("主题:"))
        theme_layout.addWidget(self.theme_combo)
        theme_layout.addStretch()
        theme_group.setLayout(theme_layout)
        layout.addWidget(theme_group)
        
        # ══════════════════════════════════════════════════════
        #  主控 / 子控 体系
        # ══════════════════════════════════════════════════════
        ctl_group = QGroupBox("部署形态（单体 / 主控 / 子控）")
        ctl_outer = QVBoxLayout()
        
        role_row = QHBoxLayout()
        role_row.addWidget(QLabel("部署形态:"))
        self.role_combo = QComboBox()
        self.role_combo.addItems([
            "单体 —— 只在本机守护，不连任何通道（开源默认）",
            "主控 —— 收子控报错，自动修复并下发更新",
            "子控 —— 上报报错，接收更新并自动重启",
        ])
        _modes = ('standalone', 'master', 'agent')
        try:
            _cur = str(getattr(self.config, 'deployment_mode', 'standalone')).lower()
            self.role_combo.setCurrentIndex(
                _modes.index(_cur) if _cur in _modes else 0)
        except Exception:
            self.role_combo.setCurrentIndex(0)
        self.role_combo.currentIndexChanged.connect(self._on_role_changed)
        role_row.addWidget(self.role_combo, 1)
        ctl_outer.addLayout(role_row)
        
        # ── 主控专属 ──
        self.master_box = QGroupBox("主控设置")
        mb = QGridLayout()
        self.auto_fix_check = QCheckBox("接收子控报错，自动生成修复包")
        self.auto_fix_check.setChecked(getattr(self.config, 'auto_fix_enabled', True))
        mb.addWidget(self.auto_fix_check, 0, 0, 1, 2)
        self.self_update_check = QCheckBox("生成更新包后，主控自己也更新")
        self.self_update_check.setChecked(getattr(self.config, 'master_self_update', True))
        mb.addWidget(self.self_update_check, 0, 2, 1, 2)
        self.http_server_check = QCheckBox("启动内建更新服务器")
        self.http_server_check.setChecked(
            bool(getattr(self.config, 'http_server_enabled', True)))
        mb.addWidget(self.http_server_check, 1, 0, 1, 2)
        self.http_port_spin = QSpinBox()
        self.http_port_spin.setRange(1024, 65535)
        self.http_port_spin.setValue(int(getattr(self.config, 'http_port', 8765) or 8765))
        mb.addWidget(QLabel("端口:"), 1, 2)
        mb.addWidget(self.http_port_spin, 1, 3)
        self.http_token_edit = QLineEdit(getattr(self.config, 'http_token', ''))
        self.http_token_edit.setPlaceholderText("访问令牌（可留空 = 不鉴权）")
        self.http_token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        mb.addWidget(QLabel("令牌:"), 2, 0)
        mb.addWidget(self.http_token_edit, 2, 1, 1, 3)
        _src_hint = QLabel("源码目录: {}".format(
            getattr(self.config, 'master_source_dir', '') or "（未设置，自动修复会失败）"))
        _src_hint.setStyleSheet("color: gray; font-size: 11px;")
        _src_hint.setWordWrap(True)
        mb.addWidget(_src_hint, 3, 0, 1, 4)
        # 认证兜底：DSH 重写凭据文件后，ZeroFall 仍要能连模型
        try:
            from modules import secret_store as _ss
            _cur_key = _ss.get('OPENCODE_GO_API_KEY')
            _key_hint = _ss.mask(_cur_key) if _cur_key else '未设置'
        except Exception:
            _key_hint = '未设置'
        self.api_key_edit = QLineEdit('')
        self.api_key_edit.setPlaceholderText(
            "留空 = 自动从 DSH 凭据读取；已缓存: {}".format(_key_hint))
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        mb.addWidget(QLabel("API Key:"), 4, 0)
        mb.addWidget(self.api_key_edit, 4, 1, 1, 3)
        _key_note = QLabel("DSH 会重写自己的凭据文件（实测已发生过），"
                           "届时 ZeroFall 会用手上缓存的 last-known-good 继续工作。"
                           "若缓存也失效，把 key 填到这里即可，一次填好长期有效。")
        _key_note.setStyleSheet("color: gray; font-size: 11px;")
        _key_note.setWordWrap(True)
        mb.addWidget(_key_note, 5, 0, 1, 4)
        # 认证自检：不用等子控报错就能知道能不能调模型
        self.probe_auth_btn = QPushButton("测试认证 / 模型连通性")
        self.probe_auth_btn.clicked.connect(self._on_probe_auth)
        mb.addWidget(self.probe_auth_btn, 6, 0, 1, 2)
        self.probe_result_label = QLabel("未测试")
        self.probe_result_label.setWordWrap(True)
        self.probe_result_label.setStyleSheet("color: gray; font-size: 11px;")
        mb.addWidget(self.probe_result_label, 6, 2, 1, 2)
        self.master_box.setLayout(mb)
        ctl_outer.addWidget(self.master_box)
        
        # ── 子控专属 ──
        self.agent_box = QGroupBox("子控设置")
        ab = QGridLayout()
        self.agent_id_edit = QLineEdit(getattr(self.config, 'agent_id', ''))
        self.agent_id_edit.setPlaceholderText("留空自动生成")
        ab.addWidget(QLabel("子控标识:"), 0, 0)
        ab.addWidget(self.agent_id_edit, 0, 1)
        self.master_url_edit = QLineEdit(getattr(self.config, 'master_url', ''))
        self.master_url_edit.setPlaceholderText("http://主控IP:8765（留空则用共享目录）")
        ab.addWidget(QLabel("主控地址:"), 0, 2)
        ab.addWidget(self.master_url_edit, 0, 3)
        self.report_master_check = QCheckBox("向主控提供错误/崩溃数据")
        self.report_master_check.setChecked(
            getattr(self.config, 'report_to_master', True))
        self.report_master_check.toggled.connect(self._sync_report_children)
        ab.addWidget(self.report_master_check, 1, 0, 1, 2)
        self.report_zf_check = QCheckBox("含 ZeroFall 自身报错")
        self.report_zf_check.setChecked(getattr(self.config, 'report_zf_error', True))
        ab.addWidget(self.report_zf_check, 1, 2)
        self.report_crash_check = QCheckBox("含 DeepSeek 崩溃")
        self.report_crash_check.setChecked(getattr(self.config, 'report_dsh_crash', True))
        ab.addWidget(self.report_crash_check, 1, 3)
        self.auto_update_check = QCheckBox("收到更新包后自动更新并重启")
        self.auto_update_check.setChecked(
            getattr(self.config, 'auto_apply_update', True))
        ab.addWidget(self.auto_update_check, 2, 0, 1, 4)
        self.agent_box.setLayout(ab)
        ctl_outer.addWidget(self.agent_box)
        
        # ── 传输通道（两边都需要）──
        chan_box = QGroupBox("传输通道")
        cb = QGridLayout()
        self.channel_edit = QLineEdit(getattr(self.config, 'control_channel_dir', ''))
        self.channel_edit.setPlaceholderText(
            r"主控与子控都能访问的共享目录，例如 <同步盘>\zerofall-ctl")
        cb.addWidget(QLabel("共享目录:"), 0, 0)
        cb.addWidget(self.channel_edit, 0, 1, 1, 3)
        _chan_hint = QLabel("主控子控都能访问即可（网盘同步盘 / 局域网共享 / NAS）。"
                            "HTTP 与中继都不通时，靠它兜底。")
        _chan_hint.setStyleSheet("color: gray; font-size: 11px;")
        _chan_hint.setWordWrap(True)
        cb.addWidget(_chan_hint, 1, 0, 1, 4)
        
        _relay_row = 2
        self.relay_check = QCheckBox("启用中继中转（跨网络，子控零安装）")
        self.relay_check.setChecked(bool(getattr(self.config, 'relay_enabled', False)))
        self.relay_check.toggled.connect(self._on_relay_kind_changed)
        cb.addWidget(self.relay_check, _relay_row, 0, 1, 2)
        self.relay_kind_combo = QComboBox()
        self.relay_kind_combo.addItems([
            "WebDAV 网盘", "S3 对象存储", "FTP / FTPS"])
        _km = {'webdav': 0, 's3': 1, 'ftp': 2}
        self.relay_kind_combo.setCurrentIndex(
            _km.get(str(getattr(self.config, 'relay_kind', 'webdav')), 0))
        self.relay_kind_combo.currentIndexChanged.connect(self._on_relay_kind_changed)
        cb.addWidget(self.relay_kind_combo, _relay_row, 2, 1, 2)
        
        # WebDAV
        self.relay_url_edit = QLineEdit(getattr(self.config, 'relay_url', ''))
        self.relay_url_edit.setPlaceholderText("https://dav.jianguoyun.com/dav")
        cb.addWidget(QLabel("地址:"), _relay_row + 1, 0)
        cb.addWidget(self.relay_url_edit, _relay_row + 1, 1, 1, 3)
        self.relay_user_edit = QLineEdit(getattr(self.config, 'relay_user', ''))
        self.relay_user_edit.setPlaceholderText("账号（多为邮箱）")
        cb.addWidget(QLabel("账号:"), _relay_row + 2, 0)
        cb.addWidget(self.relay_user_edit, _relay_row + 2, 1)
        self.relay_pass_edit = QLineEdit(getattr(self.config, 'relay_password', ''))
        self.relay_pass_edit.setPlaceholderText("应用密码")
        self.relay_pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        cb.addWidget(QLabel("密码:"), _relay_row + 2, 2)
        cb.addWidget(self.relay_pass_edit, _relay_row + 2, 3)
        
        # S3
        self.s3_endpoint_edit = QLineEdit(getattr(self.config, 's3_endpoint', ''))
        self.s3_endpoint_edit.setPlaceholderText("https://<account>.r2.cloudflarestorage.com")
        cb.addWidget(QLabel("端点:"), _relay_row + 3, 0)
        cb.addWidget(self.s3_endpoint_edit, _relay_row + 3, 1, 1, 3)
        self.s3_bucket_edit = QLineEdit(getattr(self.config, 's3_bucket', ''))
        self.s3_bucket_edit.setPlaceholderText("桶名")
        cb.addWidget(QLabel("桶名:"), _relay_row + 4, 0)
        cb.addWidget(self.s3_bucket_edit, _relay_row + 4, 1)
        self.s3_region_edit = QLineEdit(getattr(self.config, 's3_region', 'auto'))
        self.s3_region_edit.setPlaceholderText("auto")
        cb.addWidget(QLabel("区域:"), _relay_row + 4, 2)
        cb.addWidget(self.s3_region_edit, _relay_row + 4, 3)
        self.s3_ak_edit = QLineEdit(getattr(self.config, 's3_access_key', ''))
        self.s3_ak_edit.setPlaceholderText("Access Key")
        cb.addWidget(QLabel("AK:"), _relay_row + 5, 0)
        cb.addWidget(self.s3_ak_edit, _relay_row + 5, 1)
        self.s3_sk_edit = QLineEdit(getattr(self.config, 's3_secret_key', ''))
        self.s3_sk_edit.setPlaceholderText("Secret Key")
        self.s3_sk_edit.setEchoMode(QLineEdit.EchoMode.Password)
        cb.addWidget(QLabel("SK:"), _relay_row + 5, 2)
        cb.addWidget(self.s3_sk_edit, _relay_row + 5, 3)
        
        # FTP
        self.ftp_host_edit = QLineEdit(getattr(self.config, 'ftp_host', ''))
        self.ftp_host_edit.setPlaceholderText("ftp.example.com")
        cb.addWidget(QLabel("主机:"), _relay_row + 6, 0)
        cb.addWidget(self.ftp_host_edit, _relay_row + 6, 1)
        self.ftp_port_spin = QSpinBox()
        self.ftp_port_spin.setRange(1, 65535)
        self.ftp_port_spin.setValue(int(getattr(self.config, 'ftp_port', 21) or 21))
        cb.addWidget(QLabel("端口:"), _relay_row + 6, 2)
        cb.addWidget(self.ftp_port_spin, _relay_row + 6, 3)
        self.ftp_user_edit = QLineEdit(getattr(self.config, 'ftp_user', ''))
        self.ftp_user_edit.setPlaceholderText("FTP 账号")
        cb.addWidget(QLabel("账号:"), _relay_row + 7, 0)
        cb.addWidget(self.ftp_user_edit, _relay_row + 7, 1)
        self.ftp_pass_edit = QLineEdit(getattr(self.config, 'ftp_password', ''))
        self.ftp_pass_edit.setPlaceholderText("FTP 密码")
        self.ftp_pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        cb.addWidget(QLabel("密码:"), _relay_row + 7, 2)
        cb.addWidget(self.ftp_pass_edit, _relay_row + 7, 3)
        self.ftp_tls_check = QCheckBox("FTPS")
        self.ftp_tls_check.setChecked(bool(getattr(self.config, 'ftp_tls', False)))
        cb.addWidget(self.ftp_tls_check, _relay_row + 8, 0)
        
        # 中转目录（三种共用）
        self.relay_root_edit = QLineEdit(getattr(self.config, 'relay_root', '/zerofall-ctl'))
        self.relay_root_edit.setPlaceholderText("/zerofall-ctl")
        cb.addWidget(QLabel("中转目录:"), _relay_row + 8, 2)
        cb.addWidget(self.relay_root_edit, _relay_row + 8, 3)
        
        # 子控专用凭据
        self.relay_agent_user_edit = QLineEdit(
            getattr(self.config, 'relay_agent_user', ''))
        self.relay_agent_user_edit.setPlaceholderText("留空与主控共用（WebDAV/FTP）")
        cb.addWidget(QLabel("子控账号:"), _relay_row + 9, 0)
        cb.addWidget(self.relay_agent_user_edit, _relay_row + 9, 1)
        self.relay_agent_pass_edit = QLineEdit(
            getattr(self.config, 'relay_agent_password', ''))
        self.relay_agent_pass_edit.setPlaceholderText("子控专用密码")
        self.relay_agent_pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        cb.addWidget(QLabel("子控密码:"), _relay_row + 9, 2)
        cb.addWidget(self.relay_agent_pass_edit, _relay_row + 9, 3)
        self.s3_agent_ak_edit = QLineEdit(
            getattr(self.config, 's3_agent_access_key', ''))
        self.s3_agent_ak_edit.setPlaceholderText("子控专用 AK（留空共用）")
        cb.addWidget(QLabel("子控 AK:"), _relay_row + 10, 0)
        cb.addWidget(self.s3_agent_ak_edit, _relay_row + 10, 1)
        self.s3_agent_sk_edit = QLineEdit(
            getattr(self.config, 's3_agent_secret_key', ''))
        self.s3_agent_sk_edit.setPlaceholderText("子控专用 SK")
        self.s3_agent_sk_edit.setEchoMode(QLineEdit.EchoMode.Password)
        cb.addWidget(QLabel("子控 SK:"), _relay_row + 10, 2)
        cb.addWidget(self.s3_agent_sk_edit, _relay_row + 10, 3)
        
        _relay_hint = QLabel(
            "三种任选一种，主控子控填同一组参数即可跨网络推送（秒级、不需同局域网、子控零安装）。\n"
            "安全建议：子控机器填「子控专用凭据」，即便配置被翻看也改不了更新包。")
        _relay_hint.setStyleSheet("color: gray; font-size: 11px;")
        _relay_hint.setWordWrap(True)
        cb.addWidget(_relay_hint, _relay_row + 11, 0, 1, 4)
        chan_box.setLayout(cb)
        ctl_outer.addWidget(chan_box)
        
        # 单体模式（开源默认）：主控/子控能力默认关闭，相关设置块收起来。
        # 只隐藏控件不动配置，切回主控模式时原设置还在。
        try:
            _mode = str(getattr(self.config, 'deployment_mode', 'standalone') or '').strip().lower()
            if _mode == 'standalone':
                for _w in (getattr(self, 'master_box', None),
                           getattr(self, 'agent_box', None)):
                    if _w is not None:
                        _w.setVisible(False)
                _hint = QLabel(
                    "当前是单体模式：只在本机做守护、快照与回退，\n"
                    "不连接任何控制通道、不向任何人上报数据。\n"
                    "需要多机协同时把部署形态改成「主控」或「子控」。")
                _hint.setWordWrap(True)
                _hint.setStyleSheet(
                    "color: {}; font-size: 12px;".format(th.TEXT_MUTE))
                ctl_outer.addWidget(_hint) if hasattr(ctl_outer, 'addWidget') \
                    else ctl_outer.layout().addWidget(_hint)
        except Exception:
            pass

        ctl_group.setLayout(ctl_outer)
        self._on_relay_kind_changed()
        self._on_role_changed(self.role_combo.currentIndex())
        layout.addWidget(ctl_group)
        
        check_group = QGroupBox("环境校验")
        check_layout = QGridLayout()
        
        self.md5_check = QCheckBox("启用MD5一致性校验")
        self.md5_check.setChecked(self.config.md5_check_enabled)
        check_layout.addWidget(self.md5_check, 0, 0)
        
        self.session_check = QCheckBox("启用会话完整性校验")
        self.session_check.setChecked(self.config.session_integrity_check)
        check_layout.addWidget(self.session_check, 0, 1)
        
        check_group.setLayout(check_layout)
        layout.addWidget(check_group)
        
        # ── 启动行为 ──
        behavior_group = QGroupBox("启动行为")
        behavior_layout = QGridLayout()
        
        self.auto_launch_check = QCheckBox("自检全部通过后自动启动 DeepSeek")
        self.auto_launch_check.setChecked(getattr(self.config, "auto_launch_deepseek", True))
        behavior_layout.addWidget(self.auto_launch_check, 0, 0)
        
        self.auto_exit_check = QCheckBox("DeepSeek 关闭后 ZeroFall 自动退出")
        self.auto_exit_check.setChecked(getattr(self.config, "auto_exit_after_deepseek_close", True))
        behavior_layout.addWidget(self.auto_exit_check, 0, 1)
        
        self.auto_rollback_check = QCheckBox("DeepSeek 崩溃后自动回退最新健康快照并重启")
        self.auto_rollback_check.setChecked(getattr(self.config, "auto_rollback_on_crash", True))
        behavior_layout.addWidget(self.auto_rollback_check, 1, 0, 1, 2)
        
        # 崩溃恢复倒计时：默认 10 分钟。崩溃时人常不在电脑前，
        # 60 秒太短，还没看清是哪个快照就被自动恢复了。
        behavior_layout.addWidget(QLabel("崩溃后等待选择:"), 2, 0)
        self.crash_countdown_spin = QSpinBox()
        self.crash_countdown_spin.setRange(0, 3600)
        self.crash_countdown_spin.setSingleStep(30)
        self.crash_countdown_spin.setSuffix(" 秒")
        self.crash_countdown_spin.setValue(
            int(getattr(self.config, "crash_restore_countdown", 600) or 0))
        self.crash_countdown_spin.setToolTip(
            "崩溃后弹出恢复选择框的自动倒计时。\n"
            "默认 600 秒（10 分钟）；填 0 表示不限时，必须手动点「立即恢复」。")
        behavior_layout.addWidget(self.crash_countdown_spin, 2, 1)
        _cd_hint = QLabel("0 = 不限时，必须手动确认")
        _cd_hint.setStyleSheet("color: gray; font-size: 11px;")
        behavior_layout.addWidget(_cd_hint, 2, 2)
        
        behavior_group.setLayout(behavior_layout)
        layout.addWidget(behavior_group)
        
        # ── 校验与自启 ──
        # 这几个开关以前只在配置里存在、界面无处可改，等于写死了默认值。
        # 暴露出来后改动即刻生效，不用手改 json。
        misc_group = QGroupBox("校验与自启")
        misc_layout = QGridLayout()

        self.pre_launch_check = QCheckBox("启动时自动执行前置校验")
        self.pre_launch_check.setChecked(
            getattr(self.config, "pre_launch_check_enabled", True))
        self.pre_launch_check.setToolTip(
            "关闭后启动与后台刷新不再自动校验；手动点「执行校验」仍可运行。")
        misc_layout.addWidget(self.pre_launch_check, 0, 0)

        self.cloud_consistency_check = QCheckBox("云同步后校验加密包完整性")
        self.cloud_consistency_check.setChecked(
            getattr(self.config, "cloud_consistency_check", False))
        self.cloud_consistency_check.setToolTip(
            "上传后核对云端文件大小是否与源快照相符，能发现断电导致的半截加密包。")
        misc_layout.addWidget(self.cloud_consistency_check, 0, 1)

        self.crash_alert_check = QCheckBox("DeepSeek 崩溃时弹窗告警")
        self.crash_alert_check.setChecked(
            getattr(self.config, "crash_alert_enabled", True))
        self.crash_alert_check.setToolTip(
            "关闭后崩溃仍记录日志并保留崩溃快照，只是不弹窗打断你。")
        misc_layout.addWidget(self.crash_alert_check, 1, 0)

        self.auto_start_check = QCheckBox("开机自动启动 ZeroFall")
        self.auto_start_check.setChecked(
            getattr(self.config, "auto_start_enabled", True))
        self.auto_start_check.setToolTip(
            "写入当前用户注册表 Run 项，保存后立即生效。")
        misc_layout.addWidget(self.auto_start_check, 1, 1)

        misc_layout.addWidget(QLabel(""), 2, 0)
        self.export_diag_btn = QPushButton("导出诊断包（提 Issue 用）")
        self.export_diag_btn.setToolTip(
            "打包日志、配置（密钥已脱敏）、插件清单、环境校验结果。\n"
            "不包含插件源码、会话文件、快照内容或任何明文凭据。")
        self.export_diag_btn.clicked.connect(self._on_export_diagnostics)
        misc_layout.addWidget(self.export_diag_btn, 2, 1)

        misc_group.setLayout(misc_layout)
        layout.addWidget(misc_group)

        save_btn = QPushButton("保存设置")
        save_btn.clicked.connect(self._on_save_settings)
        layout.addWidget(save_btn)
        
        layout.addStretch()
    
    def _on_export_diagnostics(self):
        """导出诊断包（给 GitHub Issue 用）

        开源分发不做"自动上报崩溃"：那会把隐私责任和 API 成本都压到
        维护者身上。改成让用户自己导出、自己贴 Issue，边界清楚。
        """
        self.export_diag_btn.setEnabled(False)
        _orig = self.export_diag_btn.text()
        self.export_diag_btn.setText("正在打包…")
        try:
            from modules.diagnostics import build_diagnostic_bundle
            from pathlib import Path as _P
            snap_dir = getattr(self.config, 'snapshot_dir', '') or ''
            log_dir = _P(snap_dir).parent / 'logs' if snap_dir else None
            dest = build_diagnostic_bundle(self.config, log_dir=log_dir)
            size_kb = dest.stat().st_size / 1024
            QMessageBox.information(
                self, "诊断包已导出",
                "已保存到：\n{}\n\n大小 {:.1f} KB\n\n"
                "提 Issue 时附上这个文件即可。\n"
                "发送前建议先解压看一眼，确认没有不想外传的路径信息。".format(
                    dest, size_kb))
        except Exception as e:
            QMessageBox.critical(self, "导出失败", "生成诊断包出错：\n{}".format(e))
        finally:
            try:
                self.export_diag_btn.setEnabled(True)
                self.export_diag_btn.setText(_orig)
            except Exception:
                pass

    def _refresh_full_hint(self):
        """把"间隔 × 上限"换算成实际覆盖时长和磁盘占用，直接显示出来

        完整快照单个 150~300 MB，用户很容易把间隔调到 10 分钟却不改上限，
        结果一小时滚一圈、磁盘一直被重写。这里把账算给他看。
        """
        try:
            interval = self.full_interval_spin.value()
            # 读界面上的值，而不是 config —— 用户刚改完还没点保存时，
            # config 还是旧值，提示会显示错的账
            limit = int(self.full_max_spin.value())
            if interval <= 0:
                self.full_limit_label.setText(
                    "定时触发已关闭。当前保留上限 {} 个完整快照。".format(limit))
                return
            cover_min = interval * limit
            cover_txt = ("{:.1f} 小时".format(cover_min / 60.0) if cover_min >= 60
                         else "{} 分钟".format(cover_min))
            self.full_limit_label.setText(
                "每 {} 分钟一个 × 保留 {} 个 ≈ 覆盖最近 {}，"
                "磁盘占用约 {:.1f}~{:.1f} GB。".format(
                    interval, limit, cover_txt,
                    limit * 150 / 1024.0, limit * 300 / 1024.0))
        except Exception:
            pass

    def _on_save_settings(self):
        """保存设置"""
        # 校验与自启这组
        try:
            self.config.pre_launch_check_enabled = self.pre_launch_check.isChecked()
            self.config.cloud_consistency_check = self.cloud_consistency_check.isChecked()
            self.config.crash_alert_enabled = self.crash_alert_check.isChecked()
            self.config.auto_start_enabled = self.auto_start_check.isChecked()
        except Exception:
            pass
        self.config.deepseek_exe_path = self.exe_path_edit.text()
        self.config.deepseek_plugins_dir = self.plugins_dir_edit.text()
        
        self.config.backup_plugins = self.backup_plugins_check.isChecked()
        self.config.backup_plugin_config = self.backup_config_check.isChecked()
        self.config.backup_session_data = self.backup_session_check.isChecked()
        self.config.backup_keys = self.backup_keys_check.isChecked()
        
        self.config.md5_check_enabled = self.md5_check.isChecked()
        self.config.session_integrity_check = self.session_check.isChecked()
        
        self.config.auto_launch_deepseek = self.auto_launch_check.isChecked()
        self.config.auto_exit_after_deepseek_close = self.auto_exit_check.isChecked()
        self.config.auto_rollback_on_crash = self.auto_rollback_check.isChecked()
        try:
            self.config.crash_restore_countdown = int(self.crash_countdown_spin.value())
        except Exception:
            self.config.crash_restore_countdown = 600
        
        self.config.silent_snapshot_interval_minutes = self.interval_spin.value()
        self.config.plugin_drift_snapshot = self.drift_check.isChecked()
        self.config.max_light_snapshots = self.max_snap_spin.value()
        try:
            self.config.max_full_snapshots = self.full_max_spin.value()
        except Exception:
            pass
        self.config.daily_full_snapshot = self.daily_full_check.isChecked()
        try:
            self.config.full_snapshot_interval_minutes = int(self.full_interval_spin.value())
        except Exception:
            pass
        
        self.config.cloud_sync_enabled = self.cloud_sync_check.isChecked()
        self.config.cloud_sync_dir = self.cloud_dir_edit.text().strip()
        self.config.cloud_sync_interval_minutes = self.cloud_interval_spin.value()
        self.config.cloud_history_keep_days = self.cloud_history_spin.value()
        self.config.cloud_sync_encrypt = self.cloud_encrypt_check.isChecked()
        
        # 主控 / 子控
        _idx = self.role_combo.currentIndex()
        _mode = ('standalone', 'master', 'agent')[_idx] if 0 <= _idx <= 2 else 'standalone'
        self.config.deployment_mode = _mode
        # role 是历史字段，保持与 mode 一致，避免老代码读到矛盾值
        self.config.role = 'master' if _mode == 'master' else 'agent'
        self.config.control_channel_dir = self.channel_edit.text().strip()
        self.config.agent_id = self.agent_id_edit.text().strip()
        self.config.report_to_master = self.report_master_check.isChecked()
        self.config.report_zf_error = self.report_zf_check.isChecked()
        self.config.report_dsh_crash = self.report_crash_check.isChecked()
        self.config.auto_apply_update = self.auto_update_check.isChecked()
        self.config.auto_fix_enabled = self.auto_fix_check.isChecked()
        self.config.master_self_update = self.self_update_check.isChecked()
        self.config.http_server_enabled = self.http_server_check.isChecked()
        self.config.http_port = int(self.http_port_spin.value())
        self.config.http_token = self.http_token_edit.text().strip()
        self.config.master_url = self.master_url_edit.text().strip()
        self.config.relay_enabled = self.relay_check.isChecked()
        _kinds = ['webdav', 's3', 'ftp']
        self.config.relay_kind = _kinds[self.relay_kind_combo.currentIndex()]
        self.config.relay_url = self.relay_url_edit.text().strip()
        self.config.relay_user = self.relay_user_edit.text().strip()
        self.config.relay_password = self.relay_pass_edit.text()
        self.config.relay_root = self.relay_root_edit.text().strip() or '/zerofall-ctl'
        self.config.s3_endpoint = self.s3_endpoint_edit.text().strip()
        self.config.s3_bucket = self.s3_bucket_edit.text().strip()
        self.config.s3_access_key = self.s3_ak_edit.text().strip()
        self.config.s3_secret_key = self.s3_sk_edit.text().strip()
        self.config.s3_region = self.s3_region_edit.text().strip() or 'auto'
        self.config.ftp_host = self.ftp_host_edit.text().strip()
        self.config.ftp_port = int(self.ftp_port_spin.value())
        self.config.ftp_user = self.ftp_user_edit.text().strip()
        self.config.ftp_password = self.ftp_pass_edit.text()
        self.config.ftp_tls = self.ftp_tls_check.isChecked()
        self.config.relay_agent_user = self.relay_agent_user_edit.text().strip()
        self.config.relay_agent_password = self.relay_agent_pass_edit.text()
        self.config.s3_agent_access_key = self.s3_agent_ak_edit.text().strip()
        self.config.s3_agent_secret_key = self.s3_agent_sk_edit.text()
        self.config.ftp_agent_user = self.relay_agent_user_edit.text().strip()
        self.config.ftp_agent_password = self.relay_agent_pass_edit.text()
        # API Key 存进自持密钥库，不写进配置 JSON
        try:
            _manual_key = self.api_key_edit.text().strip()
            if _manual_key:
                from modules import secret_store as _ss
                _ss.put('OPENCODE_GO_API_KEY', _manual_key)
                self.api_key_edit.clear()
        except Exception:
            pass
        
        saved = self.config_manager.save()

        # 开机自启是"系统状态"，光存配置不生效 —— 保存后立刻同步注册表
        try:
            from modules import autostart
            want = bool(getattr(self.config, 'auto_start_enabled', True))
            ok = autostart.apply_setting(want)
            if not ok:
                QMessageBox.warning(
                    self, "开机自启未生效",
                    "配置已保存，但写注册表失败。\n"
                    "请确认当前用户有权限修改 HKCU\\...\\Run。")
        except Exception as e:
            ZeroFallLogger = None
            try:
                print('开机自启应用失败:', e)
            except Exception:
                pass

        if saved:
            QMessageBox.information(
                self, "保存成功",
                "设置已保存。\n\n角色变更需要重启 ZeroFall 后生效。")
        else:
            QMessageBox.critical(self, "保存失败", "设置保存失败")
    
    def _browse_file(self, line_edit: QLineEdit):
        """浏览选择文件"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择文件", "", "可执行文件 (*.exe);;所有文件 (*.*)"
        )
        if file_path:
            line_edit.setText(file_path)
    
    def _browse_dir(self, line_edit: QLineEdit):
        """浏览选择目录"""
        dir_path = QFileDialog.getExistingDirectory(self, "选择目录")
        if dir_path:
            line_edit.setText(dir_path)
    
    def _on_relay_kind_changed(self, *args):
        """中转类型切换：只启用对应那组输入框，避免填错地方"""
        enabled = self.relay_check.isChecked()
        kind = ['webdav', 's3', 'ftp'][self.relay_kind_combo.currentIndex()
                                       if hasattr(self, 'relay_kind_combo') else 0]
        for attr in ('relay_kind_combo', 'relay_root_edit'):
            w = getattr(self, attr, None)
            if w is not None:
                w.setEnabled(enabled)
        dav_w = (self.relay_url_edit, self.relay_user_edit, self.relay_pass_edit)
        s3_w = (self.s3_endpoint_edit, self.s3_bucket_edit, self.s3_ak_edit,
                self.s3_sk_edit, self.s3_region_edit)
        ftp_w = (self.ftp_host_edit, self.ftp_port_spin, self.ftp_user_edit,
                 self.ftp_pass_edit, self.ftp_tls_check)
        for grp, active in ((dav_w, kind == 'webdav'),
                            (s3_w, kind == 's3'),
                            (ftp_w, kind == 'ftp')):
            for w in grp:
                w.setEnabled(enabled and active)

    def _on_role_changed(self, index):
        """部署形态切换：整块面板显隐

        索引语义（三选一）：0=standalone、1=master、2=agent。
        注意旧版只有两选（0=master、1=agent），改三选一后必须同步这里，
        否则"选主控却显示子控面板"这种错位很难一眼看出来。
        """
        mode = ('standalone', 'master', 'agent')[index] if 0 <= index <= 2 else 'standalone'
        is_agent = (mode == 'agent')
        # 面板级显隐 —— 比逐个置灰更清楚，避免误填到不属于自己的项
        try:
            self.master_box.setVisible(mode == 'master')
            self.agent_box.setVisible(mode == 'agent')
        except Exception:
            pass
        # 子控侧的细项：跟着上报总开关走
        try:
            self._sync_report_children()
        except Exception:
            pass
        # 中转凭据只有子控用得上
        for w in (getattr(self, 'relay_agent_user_edit', None),
                  getattr(self, 'relay_agent_pass_edit', None),
                  getattr(self, 's3_agent_ak_edit', None),
                  getattr(self, 's3_agent_sk_edit', None)):
            if w is not None:
                w.setEnabled(is_agent)
        try:
            self._on_relay_kind_changed()
        except Exception:
            pass

    def _on_probe_auth(self):
        """实测一次模型调用，直接给出人话原因

        这个按钮存在的意义：认证出问题时，ZeroFall 平时要等到子控真的
        报了错、且跑完一轮 ACP 才会暴露，动辄几分钟。点一下这里，
        几十秒内就能知道是没 key、余额不足，还是别的问题。
        """
        btn = self.probe_auth_btn
        lbl = self.probe_result_label
        btn.setEnabled(False)
        lbl.setStyleSheet("color: gray; font-size: 11px;")
        lbl.setText("测试中…（最多约 1 分钟）")

        def _work():
            try:
                from modules.acp_client import probe_model, reason_text
                profile = getattr(self.config, 'acp_profile', 'zf-acp') or 'zf-acp'
                res = probe_model(profile=profile, timeout=60)
            except Exception as e:
                res = {'ok': False, 'error': str(e), 'kind': 'other',
                       'reason': '自检过程异常'}
            try:
                from PyQt6.QtCore import QTimer
                QTimer.singleShot(0, lambda r=res: self._show_probe_result(r))
            except Exception:
                pass

        import threading
        threading.Thread(target=_work, daemon=True,
                         name="ZeroFall-AuthProbe").start()

    def _show_probe_result(self, res):
        try:
            self.probe_auth_btn.setEnabled(True)
        except Exception:
            return
        ok = bool(res.get('ok'))
        if ok:
            txt = "可用 ✓ 模型: {} | 回复: {}".format(
                res.get('model', '?'), res.get('reply', '')[:20] or '(空)')
            self.probe_result_label.setStyleSheet(
                "color: #2e7d32; font-size: 11px; font-weight: bold;")
        else:
            kind = res.get('kind') or 'other'
            reason = res.get('reason') or res.get('error') or '未知'
            txt = "不可用 ✗ [{}] {}".format(kind, reason)
            if res.get('tried'):
                txt += " | 已试: {}".format(', '.join(res['tried']))
            self.probe_result_label.setStyleSheet(
                "color: #c62828; font-size: 11px; font-weight: bold;")
        self.probe_result_label.setText(txt)
        try:
            from logger import ZeroFallLogger
            ZeroFallLogger.info("[认证自检] {}".format(txt))
        except Exception:
            pass

    def _sync_report_children(self):
        """上报总开关关掉时，子项一起灰掉，避免开了子项但没生效"""
        try:
            on = self.report_master_check.isChecked()
            self.report_zf_check.setEnabled(on)
            self.report_crash_check.setEnabled(on)
        except Exception:
            pass
    
    def _on_theme_changed(self, theme_name):
        """主题切换回调"""
        theme = "dark" if theme_name == "深色" else "light"
        self.config.theme = theme
        # 通知主窗口切换主题
        if self.parent_window and hasattr(self.parent_window, '_apply_theme'):
            self.parent_window._apply_theme(theme)
