# -*- coding: utf-8 -*-
"""提取 opencode-go 模型表的 effort 能力"""
import re
from pathlib import Path

f = Path.home() / '.dsh' / 'pkgs' / 'dsh-opencode-go' / 'lib' / 'index.js'
lines = f.read_text(encoding='utf-8', errors='replace').splitlines()
line = lines[51989]

print('=== 扫描 v4.1-flash / v4-pro 模型条目 ===')
# 匹配 "键": { ... } 形式的模型条目起点
for m in re.finditer(r'"([^"]*(?:v4\.1-flash|v4-pro|workspace)[^"]*)"\s*:\s*\{', line):
    key = m.group(1)
    start = m.end() - 1
    # 粗略配对花括号
    depth = 0
    end = start
    for i in range(start, min(start + 3000, len(line))):
        if line[i] == '{':
            depth += 1
        elif line[i] == '}':
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    body = line[start:end]
    print()
    print('--- 模型:', key, '(条目长度', len(body), ') ---')
    for k in ('reasoning', 'thinkingLevelMap', 'thinkingLevels', 'supportsReasoning'):
        mm = re.search(k + r'\s*:\s*(\{[^{}]*\}|\[[^\[\]]*\]|true|false)', body)
        if mm:
            print('  {}: {}'.format(k, mm.group(1)[:600]))
    if not any(k in body for k in ('reasoning', 'thinkingLevelMap')):
        print('  (该条目无 reasoning/thinking 字段) 片段:', body[:200])
