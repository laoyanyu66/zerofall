# -*- coding: utf-8 -*-
"""把 settings_tab 的控制面板重写为「角色分离」布局"""
import io
import re
import shutil
import sys
from pathlib import Path

TARGET = Path(__file__).parent / 'ui' / 'tabs' / 'settings_tab.py'
src = TARGET.read_text(encoding='utf-8')
lines = src.splitlines(keepends=True)

# 定位区块：从 "#  主控 / 子控 体系" 上面那行框线开始，到 addWidget(ctl_group) 结束
start = None
end = None
for i, ln in enumerate(lines):
    if '主控 / 子控 体系' in ln and start is None:
        # 往前吃掉框线注释
        j = i - 1
        while j >= 0 and lines[j].strip().startswith('#'):
            j -= 1
        start = j + 1
    if 'layout.addWidget(ctl_group)' in ln:
        end = i
if start is None or end is None:
    print('FAIL 定位失败 start={} end={}'.format(start, end))
    sys.exit(1)
print('替换范围: L{} ~ L{} （{} 行）'.format(start + 1, end + 1, end - start + 1))

NEW = '''        # ══════════════════════════════════════════════════════
        #  主控 / 子控 体系
        # ══════════════════════════════════════════════════════
        ctl_group = QGroupBox("主控 / 子控（跨机器协同）")
        ctl_outer = QVBoxLayout()
        
        role_row = QHBoxLayout()
        role_row.addWidget(QLabel("本机角色:"))
        self.role_combo = QComboBox()
        self.role_combo.addItems([
            "主控 —— 收子控报错，自动修复并下发更新",
            "子控 —— 上报报错，接收更新并自动重启",
        ])
        self.role_combo.setCurrentIndex(
            1 if getattr(self.config, 'role', 'master') == 'agent' else 0)
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
        self.channel_edit.setPlaceholderText(r"E:\\BaiduSyncdisk\\zerofall-ctl")
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
            "三种任选一种，主控子控填同一组参数即可跨网络推送（秒级、不需同局域网、子控零安装）。\\n"
            "安全建议：子控机器填「子控专用凭据」，即便配置被翻看也改不了更新包。")
        _relay_hint.setStyleSheet("color: gray; font-size: 11px;")
        _relay_hint.setWordWrap(True)
        cb.addWidget(_relay_hint, _relay_row + 11, 0, 1, 4)
        chan_box.setLayout(cb)
        ctl_outer.addWidget(chan_box)
        
        ctl_group.setLayout(ctl_outer)
        self._on_relay_kind_changed()
        self._on_role_changed(self.role_combo.currentIndex())
        layout.addWidget(ctl_group)'''

new_lines = lines[:start] + [NEW + '\n'] + lines[end + 1:]
shutil.copy2(TARGET, str(TARGET) + '.bak-before-rolesplit')
TARGET.write_text(''.join(new_lines), encoding='utf-8')
print('OK 已写入，备份: settings_tab.py.bak-before-rolesplit')
