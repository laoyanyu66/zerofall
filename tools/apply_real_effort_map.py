# -*- coding: utf-8 -*-
"""用检测到的真实能力表替换 effort-guard 的硬编码映射"""
import json
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).parent
MAP_FILE = ROOT / 'model_effort_map.json'
TARGET = Path.home() / '.dsh' / 'pkgs' / 'dsh-custom-header' / 'lib' / 'index.js'
BACKUP2 = TARGET.with_suffix('.js.bak-before-realmap')

table = json.loads(MAP_FILE.read_text(encoding='utf-8'))
print('检测到的模型数:', len(table))

# 生成 JS 映射表字面量
entries = []
for k in sorted(table.keys()):
    vals = table[k]
    entries.append('    "%s": [%s]' % (k, ', '.join('"%s"' % v for v in vals)))
table_js = ',\n'.join(entries)

new_block = (
    '  // ===== [effort-guard] 全局强制 reasoning effort 参数校验 =====\n'
    '  // 模型能力表由 detect_model_effort.js 从 opencode-go 的 thinkingLevelMap 实测生成\n'
    '  // 生成时间: ' + __import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S') + '\n'
    '  var EFFORT_LEVELS = ["off", "minimal", "low", "medium", "high", "xhigh", "max"];\n'
    '  var MODEL_EFFORT_SUPPORT = {\n'
    + table_js + '\n'
    '  };\n'
    '  // 未收录模型：判断不了能力时不做修正，原样放行（避免误伤）\n'
    '  var DEFAULT_EFFORT_SUPPORT = null;\n'
    '\n'
    '  function clampEffortSafe(model, requested) {\n'
    '    var supported = MODEL_EFFORT_SUPPORT[model];\n'
    '    if (!supported) {\n'
    '      // 未知模型：按前缀回落（如 deepseek-v4.1-* / glm-5.3-*）\n'
    '      var keys = Object.keys(MODEL_EFFORT_SUPPORT);\n'
    '      for (var k = 0; k < keys.length; k++) {\n'
    '        if (model.indexOf(keys[k]) === 0) { supported = MODEL_EFFORT_SUPPORT[keys[k]]; break; }\n'
    '      }\n'
    '    }\n'
    '    if (!supported) return requested;   // 完全未知：放行，不与 provider 冲突\n'
    '    if (!supported.length) return undefined;\n'
    '    if (requested === undefined || requested === null) return undefined;\n'
    '    if (supported.indexOf(requested) >= 0) return requested;\n'
    '    var idx = EFFORT_LEVELS.indexOf(requested);\n'
    '    if (idx < 0) return supported[0];\n'
    '    for (var i = idx; i < EFFORT_LEVELS.length; i++) {\n'
    '      if (supported.indexOf(EFFORT_LEVELS[i]) >= 0) return EFFORT_LEVELS[i];\n'
    '    }\n'
    '    for (var j = idx - 1; j >= 0; j--) {\n'
    '      if (supported.indexOf(EFFORT_LEVELS[j]) >= 0) return EFFORT_LEVELS[j];\n'
    '    }\n'
    '    return supported[0];\n'
    '  }\n'
    '\n'
    '  function createEffortGuardMiddleware() {\n'
    '    return async ({ input, init, next }) => {\n'
    '      try {\n'
    '        var body = init && init.body;\n'
    '        if (typeof body === "string" && body.length > 1) {\n'
    '          var parsed = JSON.parse(body);\n'
    '          var model = String(parsed && parsed.model || "");\n'
    '          var hasSnake = Object.prototype.hasOwnProperty.call(parsed, "reasoning_effort");\n'
    '          var hasCamel = Object.prototype.hasOwnProperty.call(parsed, "reasoningEffort");\n'
    '          if (hasSnake || hasCamel) {\n'
    '            var original = hasSnake ? parsed.reasoning_effort : parsed.reasoningEffort;\n'
    '            var fixed = clampEffortSafe(model, original);\n'
    '            if (fixed === undefined) {\n'
    '              delete parsed.reasoning_effort;\n'
    '              delete parsed.reasoningEffort;\n'
    '              console.log("[参数校验] 模型：" + model + "｜移除effort参数（原值 " + String(original) + "）");\n'
    '            } else if (String(fixed) !== String(original)) {\n'
    '              if (hasSnake) parsed.reasoning_effort = fixed;\n'
    '              if (hasCamel) parsed.reasoningEffort = fixed;\n'
    '              console.log("[参数校验] 模型：" + model + "｜原始effort：" + String(original) + " → 修正后effort：" + String(fixed));\n'
    '            } else {\n'
    '              console.log("[参数校验] 模型：" + model + "｜原始effort：" + String(original) + " → 校验通过");\n'
    '            }\n'
    '            init = Object.assign({}, init, { body: JSON.stringify(parsed) });\n'
    '          }\n'
    '        }\n'
    '      } catch (e) {\n'
    '      }\n'
    '      return next(input, init);\n'
    '    };\n'
    '  }\n'
    '\n'
    '  registerFetchMiddleware({\n'
    '    name: "effort-guard",\n'
    '    priority: 1,\n'
    '    middleware: createEffortGuardMiddleware()\n'
    '  });\n'
    '  console.log("[effort-guard] 全局 effort 校验已挂载，模型数=" + Object.keys(MODEL_EFFORT_SUPPORT).length + " (priority=1)");\n'
    '  // ===== [/effort-guard] =====\n'
)

src = TARGET.read_text(encoding='utf-8')

# 替换旧块
pattern = re.compile(r'  // ===== \[effort-guard\].*?// ===== \[/effort-guard\] =====\n', re.S)
if not pattern.search(src):
    print('未找到旧的 effort-guard 块，尝试追加模式')
    raise SystemExit(1)

if not BACKUP2.exists():
    shutil.copy2(TARGET, BACKUP2)
    print('已备份:', BACKUP2.name)

src2 = pattern.sub(new_block, src, count=1)
TARGET.write_text(src2, encoding='utf-8')
print('已用真实映射表替换')
print('文件大小:', TARGET.stat().st_size, 'bytes')
print('映射模型数:', len(table))
