# -*- coding: utf-8 -*-
"""ACP 客户端：用 Agent Client Protocol 驱动 DeepSeek Harness 执行自动化任务

用途：主控 ZeroFall 收到子控上报的错误后，自动开一个 DSH 会话去修复源码。

协议要点（实测自 dsh-acp 0.2.1-rc.5）：
  initialize              -> 返回能力与配置选项
  session/new             -> {cwd, mcpServers:[...]} -> {sessionId, configOptions:[...]}
  session/set_config_option-> {sessionId, configId:"model", value:'["provider","model"]'}
  session/prompt          -> {sessionId, prompt:[{type:"text",text:"..."}]}
  session/update          -> 通知流（agent_message_chunk / tool_call 等）
"""
import json
import os
import subprocess
import threading
import time
from pathlib import Path


class AcpError(RuntimeError):
    """ACP 调用失败"""


def _mini_yaml(text: str) -> dict:
    """极简 YAML 解析（只在 PyYAML 不可用时兜底）

    为什么需要：凭据解析以前直接 `import yaml`，一旦运行环境里没有 PyYAML，
    整块解析就被静默跳过 —— 表现为"可用key=无"，且没有任何报错提示。
    凭据文件结构很简单（缩进映射 + 标量），自己解析足够可靠，
    也顺带让测试能在无 yaml 的环境里跑。
    """
    root = {}
    stack = [(-1, root)]

    def _scalar(v: str):
        v = v.strip()
        if not v or v in ('|', '>', '|-', '>-'):
            return None
        if (v.startswith('"') and v.endswith('"') and len(v) >= 2) or \
           (v.startswith("'") and v.endswith("'") and len(v) >= 2):
            return v[1:-1]
        low = v.lower()
        if low in ('null', '~'):
            return None
        if low == 'true':
            return True
        if low == 'false':
            return False
        if v.isdigit():
            try:
                return int(v)
            except Exception:
                return v
        return v

    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith('#'):
            continue
        indent = len(raw) - len(raw.lstrip(' '))
        line = raw.strip()
        if ':' not in line:
            continue
        key, _, val = line.partition(':')
        key = key.strip().strip('"').strip("'")
        if not key:
            continue
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1] if stack else root
        if isinstance(parent, dict):
            if val.strip():
                parent[key] = _scalar(val)
            else:
                parent[key] = {}
                stack.append((indent, parent[key]))
    return root


def _parse_yaml(text: str) -> dict:
    try:
        import yaml
        data = yaml.safe_load(text) or {}
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    try:
        return _mini_yaml(text) or {}
    except Exception:
        return {}


def classify_acp_error(msg: str) -> str:
    """把 ACP 的报错归类成人能看懂的原因

    为什么要分类：以前所有失败都是一个字符串，用户看到
    "turn failed: Insufficient Balance" 也未必知道该去充值还是填 key。
    分开之后，界面和日志都能直接给出可执行的下一步。
    """
    low = (msg or '').lower()
    # 已弃用（410 ModelDeprecated）：换路由也没用，但换个模型名有用
    if 'deprecated' in low or '410' in low:
        return 'deprecated'
    # 地区限制（403 unsupported_country_region_territory）：永久不可用
    if 'region' in low or 'not available in your country' in low \
            or 'country' in low:
        return 'region'
    if 'insufficient balance' in low or 'balance' in low or 'quota' in low:
        return 'balance'          # 账号余额/额度不足
    if 'model is unavailable' in low or 'model is not available' in low:
        return 'unavailable'
    if 'invalid api key' in low or 'unauthorized' in low or '401' in low \
            or 'authentication' in low or 'no api key' in low:
        return 'auth'             # 密钥缺失或无效
    if '403' in low or 'forbidden' in low:
        return 'forbidden'
    if 'rate limit' in low or '429' in low or 'too many' in low:
        return 'ratelimit'
    if 'timeout' in low or 'timed out' in low:
        return 'timeout'
    return 'other'


def reason_text(kind: str) -> str:
    return {
        'deprecated': '该模型已下架 —— 换一个模型即可，不是配置问题',
        'region': '该模型在当前地区不可用 —— 换模型，充钱也没用',
        'unavailable': '该模型暂时不可用 —— 换模型重试',
        'balance': '账号余额/额度不足 —— 需要充值，或改用有额度的另一条路由',
        'auth': 'API Key 缺失或无效 —— 请在 设置→主控设置→API Key 填写，'
                '或把 key 写进 exe 同级的 OPENCODE_GO_API_KEY.txt',
        'forbidden': '该密钥无权访问此模型 —— 换一条路由或换 key',
        'ratelimit': '触发限流 —— 稍后重试即可，不是配置问题',
        'timeout': '请求超时 —— 检查网络或稍后重试',
        'other': '其它错误，详见错误原文',
    }.get(kind, '未知原因')


def _error_rank(kind: str) -> int:
    """错误信息价值排序：越靠前越该被报给用户"""
    return {'balance': 0, 'auth': 1, 'ratelimit': 2, 'forbidden': 3,
            'timeout': 4, 'unavailable': 5, 'deprecated': 6,
            'region': 7, 'other': 8}.get(kind, 9)


def pick_informative_error(pairs):
    """从多条尝试错误里挑最有用的一条

    实测踩过的坑：三条路由依次失败，最后一条是
    `unknown model option: ["opencode-go", ...]`（这个路由其实已经不可用），
    它把前面 `Insufficient Balance` 这条真正该让用户看到的错误盖掉了，
    界面最终显示"其它错误" —— 等于把最有价值的线索丢了。
    """
    if not pairs:
        return ('', 'other')
    real = [(m, k) for m, k in pairs
            if 'unknown model option' not in (m or '').lower()
            and 'invalid params' not in (m or '').lower()]
    pool = list(real or pairs)
    pool.sort(key=lambda x: _error_rank(x[1]))
    return pool[0]


def probe_model(profile: str = 'zf-acp', cwd: str = None, timeout: int = 60) -> dict:
    """用一句话实测认证与模型是否可用

    返回 {ok, route, model, reply, error, kind, reason, tried}
    这个自检不该被用来"修东西"，只回答一个问题：
    现在这台机器能不能真的调用模型。
    """
    import tempfile
    work = cwd or tempfile.gettempdir()
    out = {'ok': False, 'route': '', 'model': '', 'reply': '',
           'error': '', 'kind': '', 'reason': '', 'tried': []}
    try:
        with AcpClient(profile=profile, cwd=work, timeout=max(timeout, 30)) as cli:
            cli.initialize()
            sid, opts = cli.new_session(work)
            cands = pick_models(opts)
            if not cands:
                out['kind'] = 'auth'
                out['error'] = 'ACP 没有暴露任何可用模型路由'
                out['reason'] = reason_text('auth')
                return out
            last = None
            errors = []
            for value, name in cands:
                out['tried'].append(name or value)
                route = value.split('"')[1] if '"' in value else ''
                try:
                    cli.set_model(sid, value)
                    _res, text = cli.prompt(sid, '只回复两个字：就绪', timeout=timeout)
                    out.update(ok=True, model=name or value,
                               route=route,
                               reply=(text or '').strip()[:80])
                    try:
                        from modules import model_memory
                        model_memory.record_ok(route, name or value)
                    except Exception:
                        pass
                    return out
                except Exception as e:
                    last = str(e)
                    kind = classify_acp_error(last)
                    errors.append((last, kind))
                    try:
                        from modules import model_memory
                        model_memory.record_fail(route, name or value, kind)
                    except Exception:
                        pass
                    # 结构性失效（弃用/地区限制/不可用）继续试下一个模型；
                    # 认证/余额也继续；其它错误（提示词本身有问题）没必要再试
                    if kind not in ('auth', 'balance', 'forbidden', 'deprecated',
                                    'region', 'unavailable') \
                            and 'unknown model option' not in last.lower() \
                            and 'invalid params' not in last.lower():
                        break
            if not out['ok']:
                msg, kind = pick_informative_error(errors)
                if not msg:
                    msg = last or '无可用路由'
                out.update(kind=kind, error=msg, reason=reason_text(kind))
    except Exception as e:
        msg = str(e)
        kind = classify_acp_error(msg)
        out.update(kind=kind, error=msg, reason=reason_text(kind))
    return out



def load_credential_env(dsh_home: Path, cache: bool = True) -> dict:
    """解析出 ACP 子进程要用的 API key 环境变量

    认证态说明：
      * 桌面版 DSH 用 `keyRef:` 直接引用凭据服务（tiangong 账号池）。
      * ZeroFall 的 ACP 会话走 llm-pi-ai 直连路由，而 llm-pi-ai 只认
        `apiKeyEnv:`（环境变量名），没有 keyRef 机制，因此必须由我们
        把凭据物化成环境变量再交给子进程。

    解析顺序（前者优先）：
      1. ZeroFall 自持密钥库（secrets.json）—— DSH 改格式/轮换也炸不到它
      2. .credentials.yaml 的 refs 段（旧格式，明文 key）
      3. .credentials.yaml 的 records 段（新格式，从 record 名或 payload 推断）
      4. 进程环境里已有的同名变量（用户手工 export 的情况）

    副作用：只要从 2/3/4 发现可用 key，就自动存一份到自持库（last-known-good），
    下次凭据文件被 DSH 重写时仍能继续工作。
    """
    env = dict(os.environ)
    cred = dsh_home / '.credentials.yaml' if isinstance(dsh_home, Path) \
        else Path(str(dsh_home)) / '.credentials.yaml'
    found = {}

    def _put(name, secret):
        if not isinstance(secret, str) or not secret:
            return
        if name.startswith('DSH_'):
            return                      # 账号路由引用，非 API key
        if name in found:
            return
        found[name] = secret
        env[name] = secret

    # ── 1) 自持密钥库（最可靠，DSH 动不了）──
    try:
        from modules import secret_store
        for name in secret_store.KNOWN_KEYS:
            v = secret_store.get(name)
            if v:
                _put(name, v)
    except Exception:
        pass

    file_found = {}
    try:
        data = _parse_yaml(cred.read_text(encoding='utf-8'))
        known = secret_store_known()

        def _accept(name, val):
            """只接受明确的 key 名 + 像样的值

            绝不按"记录名里含 opencode"之类的猜测取值。
            凭据文件里 client-connection/browser-session、
            deepseek-account-platform/* 这些都是 DSH 内部凭据
            （浏览器会话密钥、设备 id、账号平台 token），
            当成 API key 注入只会换来一串 401，
            还会把垃圾值写进自持库 —— 比没有 key 更难排查。
            """
            if name not in known or not isinstance(val, str):
                return
            v = val.strip()
            if len(v) < 20 or any(c.isspace() for c in v):
                return
            try:
                v.encode('ascii')
            except Exception:
                return
            file_found.setdefault(name, v)

        # refs 段：名字就是 key 名
        for name, secret in (data.get('refs') or {}).items():
            _accept(name, secret)

        # records 段：只认「记录名本身就是 key 名」或显式带 key/name 的情况
        recs = data.get('records') or {}
        if isinstance(recs, dict):
            for rname, rval in recs.items():
                if isinstance(rval, str):
                    _accept(rname, rval)
                    continue
                if not isinstance(rval, dict):
                    continue
                # 记录名就是 key 名：值可能埋在 payload 里
                if str(rname) in known:
                    for src in (rval, rval.get('payload') or {}):
                        if isinstance(src, dict):
                            for fld in ('secret', 'token', 'value', 'apiKey'):
                                if isinstance(src.get(fld), str):
                                    _accept(str(rname), src.get(fld))
                named = rval.get('key') or rval.get('name')
                if not named:
                    continue
                for src in (rval, rval.get('payload') or {}):
                    if isinstance(src, dict):
                        for fld in ('secret', 'token', 'value', 'apiKey'):
                            _accept(str(named), src.get(fld))
    except Exception:
        pass

    for name, secret in file_found.items():
        _put(name, secret)

    # 4) 环境变量兜底
    for name in ('OPENCODE_GO_API_KEY', 'OPENCODE_PERSONAL_API_KEY',
                 'DEEPSEEK_API_KEY', 'OPENAI_API_KEY'):
        if env.get(name) and name not in found:
            found[name] = env[name]

    # 缓存 last-known-good：DSH 下次重写凭据文件也炸不到
    # cache=False 供测试使用 —— 否则测试里的假 key 会被写进真实自持库
    if cache:
        try:
            from modules import secret_store
            secret_store.merge({k: v for k, v in found.items()})
        except Exception:
            pass

    return env


def secret_store_known():
    try:
        from modules import secret_store
        return secret_store.KNOWN_KEYS
    except Exception:
        return ('OPENCODE_GO_API_KEY', 'OPENCODE_PERSONAL_API_KEY',
                'DEEPSEEK_API_KEY', 'OPENAI_API_KEY')


def credential_diagnosis(dsh_home: Path) -> str:
    """给排障用的认证态摘要（不含密钥内容）

    明确标出 key 来自哪一层，因为 DSH 会重写凭据文件：
      * 自持库 = 用的是 ZeroFall 自己缓存的 last-known-good（最稳）
      * 凭据文件 = 本次从 .credentials.yaml 现读的
      * 环境变量 = 从进程环境继承的
    """
    try:
        env = load_credential_env(dsh_home)
    except Exception as e:
        return '凭据读取失败: {}'.format(e)
    names = [n for n in ('OPENCODE_GO_API_KEY', 'OPENCODE_PERSONAL_API_KEY',
                         'DEEPSEEK_API_KEY') if env.get(n)]
    cred = Path(str(dsh_home)) / '.credentials.yaml'
    try:
        from modules import secret_store
        cached = secret_store.load()
        cached_names = [n for n in names if cached.get(n)]
    except Exception:
        cached_names = []
    if not names:
        src = '无'
    elif cached_names:
        src = '自持库'
    else:
        src = '凭据文件/环境'
    return '凭据文件={} | 可用key={} | 来源={}'.format(
        'OK' if cred.exists() else '缺失',
        ','.join('{}({})'.format(n, len(env[n])) for n in names) or '无',
        src)


class AcpClient:
    """DSH ACP stdio 客户端（一次任务一个实例，用完即弃）"""

    def __init__(self, profile: str = 'zf-acp', cwd: str = None, timeout: int = 900):
        self.profile = profile
        self.cwd = cwd or str(Path.cwd())
        self.timeout = timeout
        self.proc = None
        self._rid = 0
        self._responses = []
        self._notifications = []
        self._chunks = []          # 累积的助手文本
        self._lock = threading.Lock()

        home = Path(os.environ.get('USERPROFILE', '')) / '.dsh'
        self.dsh_home = home
        self.node = home / 'dsh-runtimes' / 'dsh-primary-runtime' / 'dependencies' / 'node' / 'bin' / 'node.exe'
        self.bin = Path(r'D:\DeepSeek\resources\app\dsh\node_modules\@deepseek-ai\dsh\lib\bin.js')

    # ── 生命周期 ────────────────────────────────────────────────
    def start(self):
        if not self.node.exists():
            raise AcpError('找不到 node: {}'.format(self.node))
        if not self.bin.exists():
            raise AcpError('找不到 dsh CLI: {}'.format(self.bin))
        env = load_credential_env(self.dsh_home)
        self.proc = subprocess.Popen(
            [str(self.node), str(self.bin), '--profile', self.profile],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env, cwd=self.cwd, bufsize=0,
        )
        threading.Thread(target=self._drain_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        return self

    def _drain_stdout(self):
        buf = b''
        while True:
            chunk = self.proc.stdout.read(1)
            if not chunk:
                break
            buf += chunk
            if chunk == b'\n':
                line = buf.decode('utf-8', 'replace').strip()
                buf = b''
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                with self._lock:
                    if 'id' in obj:
                        self._responses.append(obj)
                    else:
                        self._notifications.append(obj)
                        self._collect_update(obj)

    def _drain_stderr(self):
        stream = self.proc.stderr if self.proc else None
        if stream is None:
            return
        try:
            for _ in stream:
                pass
        except Exception:
            pass

    def _collect_update(self, note):
        """从 session/update 通知里累积助手文本"""
        upd = (note.get('params') or {}).get('update') or {}
        kind = upd.get('sessionUpdate')
        if kind == 'agent_message_chunk':
            content = upd.get('content') or {}
            text = content.get('text')
            if text:
                self._chunks.append(text)
        elif kind == 'agent_thought_chunk':
            content = upd.get('content') or {}
            if content.get('text'):
                self._chunks.append('[思考] ' + content['text'] + '\n')

    def close(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=8)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self.proc = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()

    # ── JSON-RPC ────────────────────────────────────────────────
    def _send(self, method, params=None):
        if not self.proc:
            raise AcpError('客户端未启动')
        self._rid += 1
        rid = self._rid
        msg = {'jsonrpc': '2.0', 'id': rid, 'method': method}
        if params is not None:
            msg['params'] = params
        try:
            self.proc.stdin.write((json.dumps(msg, ensure_ascii=False) + '\n').encode('utf-8'))
            self.proc.stdin.flush()
        except Exception as e:
            raise AcpError('发送失败: {}'.format(e))
        return rid

    def _wait(self, rid, timeout=None):
        limit = time.time() + (timeout or self.timeout)
        while time.time() < limit:
            with self._lock:
                for r in self._responses:
                    if r.get('id') == rid:
                        return r
            if self.proc and self.proc.poll() is not None:
                raise AcpError('ACP 进程已退出 code={}'.format(self.proc.returncode))
            time.sleep(0.2)
        raise AcpError('等待响应超时({}s)'.format(timeout or self.timeout))

    def _call(self, method, params=None, timeout=None):
        resp = self._wait(self._send(method, params), timeout)
        if 'error' in resp:
            err = resp['error']
            raise AcpError('{} 失败: {}'.format(method, err.get('message', err)))
        return resp.get('result') or {}

    # ── ACP 高层接口 ────────────────────────────────────────────
    def initialize(self):
        return self._call('initialize', {
            'protocolVersion': 1,
            'clientCapabilities': {'fs': {'readTextFile': False, 'writeTextFile': False}},
        }, timeout=120)

    def new_session(self, cwd=None):
        res = self._call('session/new', {
            'cwd': cwd or self.cwd,
            'mcpServers': [],
        }, timeout=180)
        return res.get('sessionId'), res.get('configOptions') or []

    def set_model(self, session_id, value):
        return self._call('session/set_config_option', {
            'sessionId': session_id, 'configId': 'model', 'value': value,
        }, timeout=60)

    def list_models(self, config_options):
        """把 configOptions 里的模型选项摊平成 [(value, name)]"""
        out = []
        for opt in config_options:
            if opt.get('id') != 'model':
                continue
            for grp in opt.get('options', []):
                for o in grp.get('options', []):
                    out.append((o.get('value'), o.get('name')))
        return out

    def prompt(self, session_id, text, timeout=None):
        """发任务，返回 (最终结果dict, 累积文本)"""
        self._chunks = []
        res = self._call('session/prompt', {
            'sessionId': session_id,
            'prompt': [{'type': 'text', 'text': text}],
        }, timeout=timeout)
        return res, ''.join(self._chunks)

    def cancel(self, session_id):
        try:
            self._send('session/cancel', {'sessionId': session_id})
        except Exception:
            pass


def pick_models(config_options,
                prefer=('v4.1-flash', 'v4-pro', 'mimo-v2.6-flash',
                        'mimo-v2.6-pro', 'grok-4.7', 'flash'),
                routes=('["deepseek-account"', '["opencode-go"')):
    """返回**按优先级排序的候选列表** [(value, name), ...]

    用于"一条路由不行就换下一条"：账号余额不足、密钥失效这类故障
    换另一条路由往往就能跑通，而现在的现实是两种认证并存
    （deepseek-account 走账号、opencode-go 走 API key）。
    """
    models = []
    for opt in config_options:
        if opt.get('id') != 'model':
            continue
        for grp in opt.get('options', []):
            for o in grp.get('options', []):
                if o.get('value'):
                    models.append((o.get('value'), o.get('name')))
    if not models:
        return []

    out = []
    seen = set()
    for rp in routes:
        for key in prefer:
            for v, n in models:
                if v.startswith(rp) and key in v and v not in seen:
                    out.append((v, n))
                    seen.add(v)
        for v, n in models:
            if v.startswith(rp) and v not in seen:
                out.append((v, n))
                seen.add(v)
    for key in prefer:
        for v, n in models:
            if key in v and v not in seen:
                out.append((v, n))
                seen.add(v)
    for v, n in models:
        if v not in seen:
            out.append((v, n))
            seen.add(v)

    # 用历史战绩重排：跑通过的模型排最前，已弃用/地区受限的沉底，
    # 免得每次修复都在注定失败的模型上浪费往返
    try:
        from modules import model_memory
        out = model_memory.rank(out)
    except Exception:
        pass
    return out


def pick_model(config_options,
               prefer=('v4.1-flash', 'flash', 'v4-pro', 'qwen3.8-max'),
               route_prefix=None,
               routes=('["deepseek-account"', '["opencode-go"')):
    """从 ACP 的 model 选项里挑一个可用模型，返回 (value, name) 或 (None, None)

    为什么要改成"按路由优先级自动选"：
      以前写死 route_prefix='["opencode-go"'，而认证早就搬到了
      deepseek-account（DSH 桌面版现在默认就是它）。一旦 opencode-go
      的 key 被 DSH 清掉，这里仍会挑 opencode-go，然后认证失败 ——
      要等到跑完一整轮 ACP（几分钟）才暴露。

      现在按 routes 顺序挑：优先 deepseek-account（自持账号认证，不依赖
      环境变量），其次 opencode-go（需要 API key）。两者都在时选前者，
      只有后者时才回退，避免"配了 key 却不用"或"没 key 还硬选"。
    """
    models = []
    for opt in config_options:
        if opt.get('id') != 'model':
            continue
        for grp in opt.get('options', []):
            for o in grp.get('options', []):
                models.append((o.get('value'), o.get('name')))
    if not models:
        return (None, None)

    # 显式指定了 route_prefix 就只认它（老调用方兼容）
    if route_prefix:
        for key in prefer:
            for v, n in models:
                if key in v and v.startswith(route_prefix):
                    return v, n
        for v, n in models:
            if v.startswith(route_prefix):
                return v, n
        return (models[0] if models else (None, None))

    # 按路由优先级 + 模型偏好挑
    for rp in routes:
        for key in prefer:
            for v, n in models:
                if v.startswith(rp) and key in v:
                    return v, n
        for v, n in models:
            if v.startswith(rp):
                return v, n
    for key in prefer:
        for v, n in models:
            if key in v:
                return v, n
    return models[0]
