import importlib.util, os
from pathlib import Path
ZF = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("rg", ZF / "modules" / "resume_guard.py")
rg = importlib.util.module_from_spec(spec); spec.loader.exec_module(rg)
print("psutil 可用:", end=" ")
try:
    import psutil; print(psutil.__version__)
except Exception as e:
    print("NO", e)
print("psutil 路径给出的端口:", rg._ports_by_psutil())
print("netstat+powershell 路径给出的端口:", rg._ports_by_netstat())
print("powershell 绝对路径:", rg._powershell_exe())
print("netstat 绝对路径:", rg._system32("netstat.exe"))
