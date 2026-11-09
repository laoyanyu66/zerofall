# ZeroFall 快速启动指南

## 安装步骤

### 1. 安装依赖
```bash
cd ZeroFall
pip install -r requirements.txt
```

### 2. 运行程序
```bash
python main.py
```

### 3. 首次配置
启动后在"设置"标签页配置：
- DeepSeek可执行文件路径（如：`C:\Program Files\DeepSeek\DeepSeek.exe`）
- DeepSeek插件目录路径
- 根据需要调整其他设置
- 点击"保存设置"

## 主要功能

### 启动DeepSeek
点击主界面"启动 DeepSeek"按钮，ZeroFall会自动执行环境校验后启动。

### 创建快照
在"快照管理"标签页点击"创建快照"。

### 恢复快照
选择快照后点击"恢复快照"，选择恢复模式。

### 云端同步
在"云端同步"标签页完成百度网盘授权后，可上传/下载快照。

## 注意事项

1. 首次运行需要配置DeepSeek路径
2. 建议创建初始健康快照作为基线
3. 修改插件前会自动创建快照
4. 崩溃后建议回退到健康快照

## 项目结构

```
ZeroFall/
├── main.py                    # 主程序入口
├── config.py                  # 配置管理
├── logger.py                  # 日志系统
├── utils.py                   # 工具函数
├── requirements.txt           # 依赖列表
├── README.md                  # 详细说明文档
├── modules/
│   ├── snapshot_manager.py    # 快照管理核心
│   ├── environment_checker.py # 环境校验
│   ├── guardian.py            # 前置守护
│   └── baidu_pan.py           # 百度网盘API
└── ui/
    └── main_window.py         # 主界面GUI
```

## 技术栈

- Python 3.8+
- PyQt6（GUI框架）
- requests（HTTP请求）
- cryptography（AES加密）
- psutil（进程管理）

---

**守护DeepSeek，稳定运行每一天**
