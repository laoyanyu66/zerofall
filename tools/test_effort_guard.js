// effort-guard 逻辑单元测试（与注入到 dsh-custom-header 的代码完全一致）
var EFFORT_LEVELS = ["off", "minimal", "low", "medium", "high", "xhigh", "max"];
var MODEL_EFFORT_SUPPORT = {
  "deepseek-v4.1-flash": ["low", "high", "max"],
  "deepseek-v4-pro": ["off", "high", "max"],
  "deepseek-flash": ["low", "high", "max"]
};
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

var cases = [
  ["deepseek-v4.1-flash", "medium", "high", "L2档位 medium 不被支持 -> 升到 high"],
  ["deepseek-v4.1-flash", "low",    "low",  "合法值原样通过"],
  ["deepseek-v4.1-flash", "max",    "max",  "合法值原样通过"],
  ["deepseek-v4.1-flash", "minimal","low",  "minimal 不支持 -> 升到 low"],
  ["deepseek-v4.1-flash", "off",    "low",  "off 不支持 -> 升到 low"],
  ["deepseek-v4.1-flash", "xhigh",  "max",  "xhigh 不支持 -> 升到 max"],
  ["deepseek-v4-pro",     "medium", "high", "pro 不支持 medium -> 升到 high"],
  ["deepseek-v4-pro",     "low",    "high", "pro 不支持 low -> 向上取最近合法档 high"],
  ["some-unknown-model",  "medium", "high", "未知模型走保守默认"],
  ["deepseek-v4.1-flash", "ultra",  "low",  "非法枚举 -> 最低支持档"],
  ["deepseek-v4.1-flash", undefined, undefined, "无 effort -> 不处理"]
];

console.log("=== effort-guard 单元测试 ===");
var pass = 0, fail = 0;
for (var i = 0; i < cases.length; i++) {
  var c = cases[i];
  var got = clampEffortSafe(c[0], c[1]);
  var ok = (String(got) === String(c[2]));
  if (ok) pass++; else fail++;
  console.log(
    (ok ? "  [OK]  " : "  [FAIL]") +
    " " + c[0] + " | " + String(c[1]) + " -> " + String(got) +
    " (期望 " + String(c[2]) + ")  // " + c[3]
  );
}
console.log("");
console.log("通过 " + pass + " / 失败 " + fail);

// 模拟完整中间件对 body 的处理
console.log("");
console.log("=== 模拟请求体修正 ===");
function simulate(rawBody) {
  var parsed = JSON.parse(rawBody);
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
  }
  return JSON.stringify(parsed);
}

var out1 = simulate('{"model":"deepseek-v4.1-flash","reasoning_effort":"medium","messages":[]}');
console.log("  输出body:", out1);
var out2 = simulate('{"model":"deepseek-v4.1-flash","reasoning_effort":"high","messages":[]}');
console.log("  输出body:", out2);
var out3 = simulate('{"model":"deepseek-v4-pro","reasoningEffort":"medium","messages":[]}');
console.log("  输出body:", out3);

