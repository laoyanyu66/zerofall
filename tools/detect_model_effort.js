// 从 opencode-go 的模型定义中提取每个模型的真实 reasoning effort 能力
const fs = require('fs');
const path = process.argv[2];
const src = fs.readFileSync(path, 'utf8');

// 1. 定位 var opencode_go_default = { ... };
const marker = 'var opencode_go_default = ';
const idx = src.indexOf(marker);
if (idx < 0) {
  console.error('未找到 opencode_go_default');
  process.exit(1);
}
// 2. 从 { 开始配对花括号（跳过字符串内的括号）
let start = src.indexOf('{', idx);
let depth = 0;
let inStr = false;
let quote = '';
let end = -1;
for (let i = start; i < src.length; i++) {
  const ch = src[i];
  if (inStr) {
    if (ch === '\\') { i++; continue; }
    if (ch === quote) inStr = false;
    continue;
  }
  if (ch === '"' || ch === "'") { inStr = true; quote = ch; continue; }
  if (ch === '{') depth++;
  else if (ch === '}') {
    depth--;
    if (depth === 0) { end = i + 1; break; }
  }
}
if (end < 0) {
  console.error('花括号配对失败');
  process.exit(1);
}

const literal = src.slice(start, end);
let modelTable;
try {
  // 对象字面量是纯 JSON 兼容结构（key 带引号）
  modelTable = JSON.parse(literal);
} catch (e) {
  try {
    modelTable = eval('(' + literal + ')');
  } catch (e2) {
    console.error('解析失败:', e.message, '/', e2.message);
    process.exit(1);
  }
}

// 3. 按插件内的 getSupportedThinkingLevels 规则推导
const EXTENDED_THINKING_LEVELS = ['off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'];

function getSupportedThinkingLevels(model) {
  if (!model.reasoning) return ['off'];
  return EXTENDED_THINKING_LEVELS.filter((level) => {
    const mapped = model.thinkingLevelMap && model.thinkingLevelMap[level];
    if (mapped === null) return false;                 // 显式 null = 不支持
    if (level === 'xhigh' || level === 'max') return mapped !== undefined;  // 必须显式映射
    return true;
  });
}

// 4. 输出：{ "模型名": ["low","high","max"], ... }
const result = {};
const detail = [];
for (const proto of Object.keys(modelTable)) {
  const models = modelTable[proto];
  if (!models || typeof models !== 'object') continue;
  for (const key of Object.keys(models)) {
    const m = models[key];
    if (!m || typeof m !== 'object') continue;
    // 模型标识：优先 id，否则去掉 chat: 前缀
    const id = m.id || key.replace(/^chat:/, '');
    const levels = getSupportedThinkingLevels(m);
    if (!result[id]) {
      result[id] = levels;
      detail.push({
        id: id,
        protocol: proto,
        reasoning: !!m.reasoning,
        thinkingLevelMap: m.thinkingLevelMap || null,
        supported: levels
      });
    }
  }
}

// 5. 输出文件供注入使用
const outPath = process.argv[3];
if (outPath) {
  fs.writeFileSync(outPath, JSON.stringify(result, null, 2), 'utf8');
}

console.log('=== 模型真实 effort 能力检测 ===');
console.log('模型总数:', detail.length);
console.log('');
const grouped = {};
for (const d of detail) {
  const k = d.supported.join(',');
  if (!grouped[k]) grouped[k] = [];
  grouped[k].push(d.id);
}
for (const k of Object.keys(grouped)) {
  console.log('支持 [' + k + ']  (' + grouped[k].length + ' 个):');
  const list = grouped[k].slice(0, 12).join(', ');
  console.log('  ' + list + (grouped[k].length > 12 ? ' ...' : ''));
  console.log('');
}
console.log('=== 关注模型明细 ===');
for (const d of detail) {
  if (/deepseek|v4/i.test(d.id)) {
    console.log('  ' + d.id);
    console.log('    reasoning=' + d.reasoning + '  supported=' + JSON.stringify(d.supported));
  }
}
if (outPath) console.log('\n已写入映射表:', outPath);
