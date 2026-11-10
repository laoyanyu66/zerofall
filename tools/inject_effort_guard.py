# -*- coding: utf-8 -*-
"""向 dsh-custom-header 注入全局 effort 校验中间件"""
import shutil
from pathlib import Path

TARGET = Path.home() / '.dsh' / 'pkgs' / 'dsh-custom-header' / 'lib' / 'index.js'
BACKUP = TARGET.with_suffix('.js.bak-before-effort-guard')

GUARD_CODE = r'''
  // ===== [effort-guard] 全局强制 reasoning effort 参数校验 =====
  // 在任何请求发出前校验 effort 是否被目标模型支持；不支持则修正或移除，
  // 避免等到 provider 返回 UNSUPPORTED_REASONING_EFFORT 才失败。
  var EFFORT_LEVELS = ["off", "minimal", "low", "medium", "high", "xhigh", "max"];
  // 由 opencode-go 的 thinkingLevelMap 推导出的真实支持档位
  var MODEL_EFFORT_SUPPORT = {
    "deepseek-v4.1-flash": ["low", "high", "max"],
    "deepseek-v4-pro": ["off", "high", "max"],
    "deepseek-flash": ["low", "high", "max"]
  };
  // 未知模型保守默认（不冒进）
  var DEFAULT_EFFORT_SUPPORT = ["low", "high", "max"];

  function clampEffortSafe(model, requested) {
    var supported = MODEL_EFFORT_SUPPORT[model] || DEFAULT_EFFORT_SUPPORT;
    if (!supported || supported.length === 0) return undefined;
    if (requested === undefined || requested === null) return undefined;
    if (supported.indexOf(requested) >= 0) return requested;
    var idx = EFFORT_LEVELS.indexOf(requested);
    if (idx < 0) return supported[0];
    for (var i = idx; i < EFFORT_LEVELS.length; i++) {
      if (supported.indexOf(EFFORT_LEVELS[i]) >= 0) return EFFORT_LEVELS[i];
    }
    for (var j = idx - 1; j >= 0; j--) {
      if (supported.indexOf(EFFORT_LEVELS[j]) >= 0) return EFFORT_LEVELS[j];
    }
    return supported[0];
  }

  function createEffortGuardMiddleware() {
    return async ({ input, init, next }) => {
      try {
        var body = init && init.body;
        if (typeof body === "string" && body.length > 1) {
          var parsed = JSON.parse(body);
          var model = String(parsed && parsed.model || "");
          var hasSnake = Object.prototype.hasOwnProperty.call(parsed, "reasoning_effort");
          var hasCamel = Object.prototype.hasOwnProperty.call(parsed, "reasoningEffort");
          if (hasSnake || hasCamel) {
            var original = hasSnake ? parsed.reasoning_effort : parsed.reasoningEffort;
            var fixed = clampEffortSafe(model, original);
            if (fixed === undefined) {
              delete parsed.reasoning_effort;
              delete parsed.reasoningEffort;
              console.log("[参数校验] 模型：" + model + "｜移除effort参数（原值 " + String(original) + "）");
            } else if (String(fixed) !== String(original)) {
              if (hasSnake) parsed.reasoning_effort = fixed;
              if (hasCamel) parsed.reasoningEffort = fixed;
              console.log("[参数校验] 模型：" + model + "｜原始effort：" + String(original) + " → 修正后effort：" + String(fixed));
            } else {
              console.log("[参数校验] 模型：" + model + "｜原始effort：" + String(original) + " → 校验通过");
            }
            init = Object.assign({}, init, { body: JSON.stringify(parsed) });
          }
        }
      } catch (e) {
        // 校验异常不阻塞请求
      }
      return next(input, init);
    };
  }

  registerFetchMiddleware({
    name: "effort-guard",
    priority: 1,
    middleware: createEffortGuardMiddleware()
  });
  console.log("[effort-guard] 全局 reasoning effort 校验已挂载（priority=1）");
  // ===== [/effort-guard] =====
'''

src = TARGET.read_text(encoding='utf-8')
if 'effort-guard' in src:
    print('已存在 effort-guard，跳过注入')
    raise SystemExit(0)

# 插入点：body-patch 注册块之后、const logger = ctx.logger; 之前
anchor = '  const logger = ctx.logger;'
if anchor not in src:
    print('未找到插入锚点，中止')
    raise SystemExit(1)

if not BACKUP.exists():
    shutil.copy2(TARGET, BACKUP)
    print('已备份:', BACKUP.name)

src = src.replace(anchor, GUARD_CODE + '\n' + anchor, 1)
TARGET.write_text(src, encoding='utf-8')
print('已注入 effort-guard 中间件')
print('文件大小:', TARGET.stat().st_size, 'bytes')
