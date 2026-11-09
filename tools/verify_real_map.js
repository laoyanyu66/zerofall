// 验证注入到 dsh-custom-header 的真实映射表行为
const fs = require('fs');
const src = fs.readFileSync('C:\\Users\\杰瑞\\.dsh\\pkgs\\dsh-custom-header\\lib\\index.js', 'utf8');

// 从注入代码中提取 MODEL_EFFORT_SUPPORT 字面量
const m = src.match(/var MODEL_EFFORT_SUPPORT = (\{[\s\S]*?\n  \});/);
if (!m) { console.error('未找到映射表'); process.exit(1); }
const MODEL_EFFORT_SUPPORT = JSON.parse(m[1]);
const EFFORT_LEVELS = ['off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'];

function clampEffortSafe(model, requested) {
  let supported = MODEL_EFFORT_SUPPORT[model];
  if (!supported) {
    const keys = Object.keys(MODEL_EFFORT_SUPPORT);
    for (let k = 0; k < keys.length; k++) {
      if (model.indexOf(keys[k]) === 0) { supported = MODEL_EFFORT_SUPPORT[keys[k]]; break; }
    }
  }
  if (!supported) return requested;
  if (!supported.length) return undefined;
  if (requested === undefined || requested === null) return undefined;
  if (supported.indexOf(requested) >= 0) return requested;
  const idx = EFFORT_LEVELS.indexOf(requested);
  if (idx < 0) return supported[0];
  for (let i = idx; i < EFFORT_LEVELS.length; i++) {
    if (supported.indexOf(EFFORT_LEVELS[i]) >= 0) return EFFORT_LEVELS[i];
  }
  for (let j = idx - 1; j >= 0; j--) {
    if (supported.indexOf(EFFORT_LEVELS[j]) >= 0) return EFFORT_LEVELS[j];
  }
  return supported[0];
}

console.log('=== 加载到', Object.keys(MODEL_EFFORT_SUPPORT).length, '个模型的真实能力 ===');
console.log('');

const cases = [
  // [模型, 请求, 期望, 说明]
  ['deepseek-v4.1-flash', 'medium',  'high',   '不支持 medium -> 升 high（原始报错场景）'],
  ['deepseek-v4.1-flash', 'low',     'low',    '合法，不动'],
  ['deepseek-v4.1-flash', 'max',     'max',    '合法，不动'],
  ['qwen3.7-plus',        'medium',  'medium', '支持 medium -> 绝不改动'],
  ['qwen3.7-plus',        'minimal', 'minimal','支持 minimal -> 不动'],
  ['minimax-m3',          'medium',  'medium', '支持 medium -> 不动'],
  ['longcat-2.0',         'medium',  'medium', '支持 medium -> 不动'],
  ['claude-haiku-5-5',    'xhigh',   'xhigh',  '全档支持 -> 不动'],
  ['deepseek-v4-pro',     'medium',  'high',   '不支持 medium -> 升 high'],
  ['deepseek-v4-pro',     'low',     'high',   '不支持 low -> 升 high'],
  ['deepseek-v4-flash',   'medium',  'high',   '支持 off/low/high/max -> medium 升 high'],
  ['glm-5.3',             'medium',  'high',   '不支持 medium -> 升 high'],
  ['glm-5.2',             'medium',  'high',   '只支持 high/max -> 升 high'],
  ['glm-5.2',             'low',     'high',   '只支持 high/max -> 升 high'],
  ['kimi-k3',             'low',     'max',    '只支持 max -> 升 max'],
  ['hy4-preview',         'medium',  'high',   '只支持 off/high -> 升 high'],
  ['hy3',                 'medium',  'high',   '支持 off/low/high -> 升 high'],
  ['grok-4.6',            'max',     'xhigh',  '只到 xhigh -> 降 xhigh'],
  ['gpt-6-luna',          'max',     'xhigh',  '只到 xhigh -> 降 xhigh'],
  ['muse-spark-1.2-contributor', 'off', 'minimal', '不支持 off -> 升 minimal'],
  ['unknown-model-xyz',   'medium',  'medium', '完全未知 -> 放行不改（不误伤）'],
  ['deepseek-v4.1-flash-future', 'medium', 'high', '前缀回落匹配到 flash'],
];

let pass = 0, fail = 0;
for (const c of cases) {
  const got = clampEffortSafe(c[0], c[1]);
  const ok = String(got) === String(c[2]);
  if (ok) pass++; else fail++;
  console.log((ok ? '[OK]  ' : '[FAIL]') + ' ' + c[0].padEnd(28) + ' ' + String(c[1]).padEnd(9) + '-> ' + String(got).padEnd(9) + ' // ' + c[3]);
}
console.log('');
console.log('通过 ' + pass + ' / 失败 ' + fail);

console.log('');
console.log('=== 全部 31 个模型：请求 medium 的实际结果 ===');
const keys = Object.keys(MODEL_EFFORT_SUPPORT).sort();
for (const k of keys) {
  const got = clampEffortSafe(k, 'medium');
  const mark = (got === 'medium') ? '保持' : ('改为 ' + got);
  console.log('  ' + k.padEnd(32) + ' 支持[' + MODEL_EFFORT_SUPPORT[k].join(',') + ']' + '  -> ' + mark);
}
