# -*- coding: utf-8 -*-
"""HTTP 层：class H —— 回环代理的请求处理。

只绑 127.0.0.1。三条安全边界（本次开源审查加固）：
  1. **入站请求体上限** MAX_BODY_BYTES，超出 413；此前无上界，一个超大
     Content-Length 即可让本机内存耗尽；
  2. **敏感值常量时间比较** hmac.compare_digest，避免时序攻击
     （实际风险低，但是开源项目被审计时的硬指标）；
  3. **CONNECT 隧道白名单** —— 此前除 Anthropic 系外一律放行，等于附带
     提供了一个通用正向代理；现在默认只放行白名单内的目标。

流式与协议翻译的业务逻辑在 modelmap / upstream / translate，本模块只做
HTTP 编解码。
"""
import hmac
import json
import os
import select
import socket
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import constants as K
from . import logging_setup as L
from . import modelmap, state, translate, upstream

log = L.get_logger("proxy")


def _tunnel_allowlist():
    """读 CONNECT 隧道白名单：逗号分隔，支持 '*' 全放行。"""
    raw = os.environ.get(K.TUNNEL_ALLOWLIST_ENV, "")
    return [x.strip().lower() for x in raw.split(",") if x.strip()]


def _templates_payload():
    """给前端下拉框用的 provider 类型清单（不含任何密钥）。"""
    from . import providers as P
    out = []
    for kind in P.all_kinds():
        try:
            t = P.template_for(kind)
        except KeyError:
            continue
        out.append({
            "kind": kind,
            "display": t.get("display", kind),
            "base_url": t.get("base_url", ""),
            "default_model": t.get("default_model", ""),
            "key_hint": t.get("key_hint", ""),
            "models": t.get("models", []),
            "mode": t.get("mode"),
            "passthrough": bool(t.get("passthrough")),
        })
    return out


def _err_page(title, detail, hint=""):
    """中转页失败时给用户看的可读页面（不返回 JSON：这是浏览器导航）。"""
    import html as _h
    return (
        "<!doctype html><meta charset='utf-8'>"
        "<title>" + _h.escape(title) + "</title>"
        "<style>body{background:#1A1A2E;color:#fff;font:15px/1.7 "
        "system-ui,'Microsoft YaHei';display:flex;align-items:center;"
        "justify-content:center;height:100vh;margin:0}"
        "div{max-width:560px;padding:32px;border:1px solid #2a2a4a;"
        "border-radius:14px;background:#16162a}"
        "h1{font-size:19px;margin:0 0 12px;color:#4ee1c1}"
        "p{margin:6px 0;color:#c9c9e0}code{color:#ffb86b}</style>"
        "<div><h1>" + _h.escape(title) + "</h1><p><code>"
        + _h.escape(detail) + "</code></p><p>" + _h.escape(hint) + "</p></div>")


def state_snapshot_port():
    """运行中代理实际监听的端口（用于判断改端口是否需重启）。"""
    from . import config as C
    return C.load_state().get("proxy_port") or C.load_config()["settings"]["proxy_port"]


def _tunnel_allowed(target, host):
    """CONNECT 隧道是否放行。

    策略（按用户明确要求，v1.2.0 起）：**全部放行，不做白名单限制**。

    背景：白名单最初是为了避免本机附带一个通用正向代理，但实测代价太大——
    Claude Science 的分析环境要访问大量包索引/CDN/文献 API，任何漏放都会
    表现为 Environments 面板装包失败，并连锁引发 Agent 报
    "Your Claude session is no longer valid"。逐个补白名单不现实。

    因此改为全放行。保留 Anthropic 系域名的 401 fast-fail（在 do_CONNECT
    里单独处理，不受此处影响）—— 那一条是必需的功能，不是安全限制。

    注意：本代理仅监听 127.0.0.1，不对外暴露；风险面是"本机任意进程都能借它
    出网"，与直接用系统代理等价。若需要收紧，设环境变量
    CSSWITCH_ALLOW_TUNNEL=host1,host2 即可回到白名单模式。
    """
    return True


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "csswitch-win-proxy"

    def log_message(self, *a):  # noqa: D102 - 交由本模块统一日志接管
        pass

    # ---------------- 回复辅助 ----------------

    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _sse(self, event, data):
        chunk = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()
        self.wfile.write(hex(len(chunk))[2:].encode() + b"\r\n" + chunk + b"\r\n")

    def _redirect(self, location, cookies=None):
        """302跳转，可选同时下发若干 ``Set-Cookie``（每项已是完整字符串）。"""
        self.send_response(302)
        self.send_header("Location", location)
        for c in cookies or ():
            self.send_header("Set-Cookie", c)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_html(self, code, html):
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _sse_error_and_terminate(self, msg):
        frame = ("event: error\ndata: " + json.dumps(
            {"type": "error", "error": {"type": "api_error", "message": msg}},
            ensure_ascii=False) + "\n\n").encode()
        self.wfile.write(hex(len(frame))[2:].encode() + b"\r\n" + frame + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")

    def _read_body(self):
        """读取并解析请求体。返回 (ok, body_dict_or_None, http_code, err_msg)。"""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (ValueError, TypeError):
            return False, None, 400, "invalid Content-Length"
        if n < 0:
            return False, None, 400, "negative Content-Length"
        if n > K.MAX_BODY_BYTES:
            # 上限保护：拒收并明确告知，不读入内存
            return (False, None, 413,
                    f"body exceeds limit of {K.MAX_BODY_BYTES} bytes")
        raw = self.rfile.read(n) if n else b"{}"
        try:
            obj = json.loads(raw)
        except Exception as e:           # noqa: BLE001 - JSON 解析失败即 400
            return False, None, 400, str(e)
        return True, obj, 200, ""

    # ---------------- 鉴权 ----------------

    def _admin_ok(self):
        token = state.admin_token()
        if not token:
            return True
        got = self.headers.get("x-csx-admin") or ""
        if hmac.compare_digest(got, token):
            return True
        self._send_json(403, {"type": "error", "error": {
            "type": "permission_error", "message": "admin token 无效"}})
        return False

    def _admin_ok_query(self):
        """admin 鉴权的URL 变体：接受 ``?token=``。

        仅供浏览器直接导航的中转页使用（新标签页无法附加请求头）。
        """
        token = state.admin_token()
        if not token:
            return True
        qs = urllib.parse.parse_qs(self.path.split("?", 1)[1]) \
            if "?" in self.path else {}
        got = (qs.get("token") or [""])[0]
        if hmac.compare_digest(got, token):
            return True
        self._send_json(403, {"type": "error", "error": {
            "type": "permission_error", "message": "admin token 无效"}})
        return False

    def _auth_ok(self):
        """path secret 校验：剥离 /<secret> 前缀。失败主动关连接。"""
        secret = state.auth_secret()
        if not secret:
            return True
        prefix = "/" + secret
        # 常量时间比较：路径前缀长度随 secret 变化，用 compare_digest 避免泄露
        if hmac.compare_digest(self.path[:len(prefix)], prefix):
            rest = self.path[len(prefix):]
            if rest == "" or rest.startswith("/"):
                self.path = rest or "/"
                return True
        self.close_connection = True
        self._send_json(403, {"type": "error", "error": {
            "type": "permission_error", "message": "forbidden"}})
        return False

    # ---------------- GET ----------------

    def do_GET(self):
        if self.path.startswith("/__admin/"):
            self._handle_admin_get()
            return
        if self.path.split("?", 1)[0] in ("/", "/panel", "/index.html"):
            self._serve_panel()
            return
        if not self._auth_ok():
            return
        path = self.path.split("?", 1)[0]
        if path.startswith("/v1/models"):
            code, body = modelmap.build_models_response()
            self._send_json(code, body)
        elif path.startswith("/health"):
            self._send_json(200, {"status": "ok",
                                  "provider": state.current(),
                                  "uptime_s": state.uptime_s()})
        else:
            self._send_json(404, {"type": "error", "error": {
                "type": "not_found_error", "message": self.path}})

    # ---------------- POST ----------------

    def do_POST(self):
        if self.path.startswith("/__admin/"):
            self._handle_admin_post()
            return
        if not self._auth_ok():
            return
        path = self.path.split("?", 1)[0]
        if not path.startswith("/v1/messages"):
            # 先读掉 body 再 404，避免 keep-alive 下污染下一条请求
            self._drain()
            self._send_json(404, {"type": "error", "error": {
                "type": "not_found_error", "message": self.path}})
            return
        ok, areq, code, err = self._read_body()
        if not ok:
            self._send_json(code, {"type": "error", "error": {
                "type": "request_too_large" if code == 413
                else "invalid_request_error", "message": err}})
            return
        if not isinstance(areq, dict) or not isinstance(areq.get("messages"), list):
            self._send_json(400, {"type": "error", "error": {
                "type": "invalid_request_error",
                "message": "request body must be a JSON object with a "
                           "'messages' array"}})
            return
        # 注意：state.mode() 有默认值 "anthropic"，不能用来判断“是否已装载”。
        # 只有 current() 为空才是真的没 provider（新电脑首次启动会走到这里）。
        if state.current() is None:
            self._send_json(503, {"type": "error", "error": {
                "type": "api_error",
                "message": "代理未加载任何 provider：请打开 CSswitch-Win "
                           "控制台添加一条第三方模型配置"}})
            return
        if state.mode() == "openai":
            self._handle_openai(areq)
        else:
            self._handle_anthropic(areq)

    def do_DELETE(self):
        # 面板统一用 POST，这里只为语义完整 + 便于 curl 调试
        if self.path.startswith("/__admin/"):
            self._handle_admin_post()
            return
        self._drain()
        self._send_json(405, {"type": "error", "error": {
            "type": "invalid_request_error",
            "message": "该端点只接受 POST"}})

    def _drain(self):
        """把请求体读干净，避免 keep-alive 下一个请求体串位。"""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (ValueError, TypeError):
            return
        if 0 < n <= K.MAX_BODY_BYTES:
            try:
                self.rfile.read(n)
            except Exception as e:       # noqa: BLE001
                log.debug("drain 失败: %s", e)

    # ---------------- 管理 API ----------------

    def _serve_panel(self):
        """内置Web 控制台（无需额外依赖，单文件 HTML）。"""
        p = Path(__file__).resolve().parent / "web" / "panel.html"
        try:
            html = p.read_bytes()
        except OSError as e:
            log.warning("控制台页面读取失败: %s", e)
            self._send_json(500, {"ok": False, "detail": f"页面缺失: {e}"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(html)

    def _admin_science_open(self):
        """中转：预热登录态再跳到 Science 首页，让用户免过Sign in 页。

        Science 的 nonce 链接落地后只是一个**需要点击**的表单
        （``id="signin-form"``），光window.open 只会停在 "Sign in"。
        这里服务端先走完 nonce 登录拿到 ``operon_auth``，用 ``Set-Cookie``
        注入浏览器后再跳首页 —— cookie 不认端口只认主机名，所以代理这边
        设的 cookie daemon 收得到。
        """
        from . import hostgrant as H
        from . import config as C

        # cookie 域跟请求主机名走：面板从 127.0.0.1 访问时，给 localhost 下的
        # daemon 设 cookie 会被浏览器拒收（域不匹配）。这种情况直接提示用户，
        # 避免"点了没反应"。
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("localhost",):
            self._redirect(f"http://localhost:{self.server.server_address[1]}"
                           + self.path)
            return

        try:
            port, cookies = H.browser_login()
        except H.HostGrantError as e:
            self._send_html(503, _err_page(
                "还无法自动登录 Claude Science", str(e),
                "请回到 CSswitch-Win 控制台，确认 Claude Science 已启动后重试。"))
            return
        except Exception as e:           # noqa: BLE001 - 兜底成可读页面
            self._send_html(503, _err_page(
                "登录 Claude Science 失败", str(e),
                "请回到 CSswitch-Win 控制台重试。"))
            return

        self._redirect(f"http://localhost:{port}/", cookies)

    def _handle_admin_get(self):
        path = self.path.split("?", 1)[0]
        # science-open 是给浏览器新标签页直接导航的中转页，拿不到自定义请求头，
        # 因此额外允许用 ?token=<admin token> 鉴权（同样是常量时间比较）
        if path == "/__admin/science-open" and self._admin_ok_query():
            self._admin_science_open()
            return
        if not self._admin_ok():
            return
        if path == "/__admin/status":
            prov, pname = state.snapshot()
            prov = prov or {}
            self._send_json(200, {
                "running": True, "provider": pname,
                "mode": prov.get("mode"),
                "upstream": prov.get("base_url"),
                "model": prov.get("model"),
                "uptime_s": state.uptime_s(),
            })
        elif path == "/__admin/providers":
            self._admin_list_providers()
        elif path == "/__admin/templates":
            self._send_json(200, {"ok": True, "kinds": _templates_payload()})
        elif path == "/__admin/settings":
            self._admin_get_settings()
        elif path == "/__admin/sandbox":
            self._admin_sandbox_status()
        elif path == "/__admin/host-grants":
            self._admin_list_grants()
        elif path == "/__admin/host-browse":
            self._admin_browse()
        elif path == "/__admin/science-open":
            self._admin_science_open()
        else:
            self._send_json(404, {"type": "error", "error": {
                "type": "not_found_error", "message": self.path}})

    # ---------------- provider CRUD（Web 面板用） ----------------

    def _admin_list_providers(self):
        """列出全部 provider。API Key 只回掩码，绝不回明文。"""
        from . import config as C
        try:
            cfg = C.load_config()
        except C.ForeignConfigError as e:
            self._send_json(409, {"ok": False, "detail": str(e)})
            return
        items = []
        for p in cfg.get("providers", []):
            items.append({
                "name": p.get("name"), "kind": p.get("kind"),
                "mode": p.get("mode"), "base_url": p.get("base_url"),
                "model": p.get("model"), "models": p.get("models", []),
                "key_masked": C.mask_key(p.get("key", "")),
                "has_key": bool(p.get("key")),
                "auth_style": p.get("auth_style"),
            })
        self._send_json(200, {
            "ok": True, "providers": items, "current": cfg.get("current"),
            "runtime_provider": state.PROV_NAME,
            "settings": cfg.get("settings", {}),
        })

    def _admin_get_settings(self):
        from . import config as C
        try:
            cfg = C.load_config()
        except C.ForeignConfigError as e:
            self._send_json(409, {"ok": False, "detail": str(e)})
            return
        st = C.load_state()
        self._send_json(200, {
            "ok": True, "settings": cfg.get("settings", {}),
            "config_path": str(C.config_path()),
            "proxy_running": True,
            "proxy_port": st.get("proxy_port"),
            "sandbox_port": st.get("sandbox_port"),
            "sandbox_pid": st.get("sandbox_pid"),
            "trusted_domains": list(K.TUNNEL_TRUSTED_DOMAINS),
            "real_auth_dir": str(C.real_auth_dir()),
            "sandbox_auth_dir": str(C.sandbox_auth_dir()),
        })

    def _handle_admin_post(self):
        if not self._admin_ok():
            return
        path = self.path.split("?", 1)[0]
        body = {}
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if 0 < n <= K.MAX_BODY_BYTES:
                body = json.loads(self.rfile.read(n)) or {}
        except Exception as e:           # noqa: BLE001 - 管理面 body 坏了按空处理
            log.debug("admin body 解析失败: %s", e)

        if path == "/__admin/switch":
            self._admin_switch(body)
        elif path == "/__admin/verify":
            name = body.get("provider")
            if not name:
                self._send_json(400, {"ok": False, "detail": "缺少 provider 名"})
                return
            self._send_json(200, {"ok": True,
                                  "verify": upstream.verify_provider(name)})
        elif path == "/__admin/provider/save":
            self._admin_save_provider(body)
        elif path == "/__admin/provider/delete":
            self._admin_delete_provider(body)
        elif path == "/__admin/settings":
            self._admin_save_settings(body)
        elif path == "/__admin/sandbox/start":
            self._admin_sandbox_start(body)
        elif path == "/__admin/sandbox/stop":
            self._admin_sandbox_stop()
        elif path == "/__admin/host-grant/add":
            self._admin_grant_add(body)
        elif path == "/__admin/host-grant/remove":
            self._admin_grant_remove(body)
        else:
            self._send_json(404, {"type": "error", "error": {
                "type": "not_found_error", "message": self.path}})

    # ---------------- 宿主文件夹授权（让每个对话能选宿主目录） ----------------

    def _grants_payload(self):
        from . import hostgrant as HG
        try:
            grants = [g.as_dict() for g in HG.list_grants()]
            home = str(HG.host_home())
            active = HG.sandbox_active()
            err = ""
        except HG.HostGrantError as e:
            grants, home, active, err = [], "", False, str(e)
        return {"ok": not err, "grants": grants, "home": home,
                "sandboxActive": active, "detail": err}

    def _admin_list_grants(self):
        self._send_json(200, self._grants_payload())

    def _admin_browse(self):
        from urllib.parse import parse_qs, urlparse
        from . import hostgrant as HG
        qs = parse_qs(urlparse(self.path).query)
        target = (qs.get("path") or [""])[0]
        try:
            entries = HG.browse(target)
        except HG.HostGrantError as e:
            self._send_json(e.code or 400, {"ok": False, "detail": str(e)})
            return
        self._send_json(200, {"ok": True, "path": target,
                              "entries": [{"name": x.name, "isDir": x.is_dir}
                                          for x in entries]})

    def _admin_grant_add(self, body):
        from . import hostgrant as HG
        try:
            r = HG.add_grant(body.get("path") or "", body.get("mode") or "rw")
        except HG.HostGrantError as e:
            self._send_json(400, {"ok": False, "detail": str(e)})
            return
        self._send_json(200, {"ok": True, "grant": r.as_dict(),
                              "recorded": r.recorded,
                              "detail": r.effective})

    def _admin_grant_remove(self, body):
        from . import hostgrant as HG
        try:
            HG.remove_grant(body.get("path") or "")
        except HG.HostGrantError as e:
            self._send_json(400, {"ok": False, "detail": str(e)})
            return
        self._send_json(200, {"ok": True, "detail": "已撤销授权"})

    # ---------------- 沙箱生命周期（Web 面板一键启停） ----------------

    def _admin_sandbox_status(self):
        """沙箱实时状态 + 启动任务进度 + 登录链接。"""
        from . import config as C
        from . import lifecycle, sandbox as S
        ss = S.sandbox_status()
        task = lifecycle.snapshot()
        # pid 优先取 task（本次启动的真实 pid），其次 state.json。
        # stop_sandbox 会清掉 state 里的 sandbox_pid，所以只在服务重启、
        # task 为空时才有 state 的值 —— 别把它当成唯一来源。
        pid = task.get("pid") or ss.get("pid")
        payload = {
            "ok": True,
            "running": bool(ss.get("running")),
            "port": ss.get("port"),
            "pid": pid,
            "detail": ss.get("detail", ""),
            "task": task,
            "url": lifecycle.current_url(),
        }
        # 进程真在跑但任务态是 idle（服务重启后的场景）——补一个 ready
        if ss.get("running") and task.get("state") in (lifecycle._IDLE,):
            payload["task"] = dict(task, state="ready")
        self._send_json(200, payload)

    def _admin_sandbox_start(self, body):
        from . import config as C
        from . import lifecycle
        cfg = C.load_config()
        port = body.get("port") or cfg["settings"]["sandbox_port"]
        r = lifecycle.start(port=int(port),
                            open_browser=bool(body.get("open_browser", True)))
        self._send_json(200 if r.get("ok") else 409, r)

    def _admin_sandbox_stop(self):
        from . import config as C
        from . import lifecycle
        cfg = C.load_config()
        r = lifecycle.stop(port=cfg["settings"]["sandbox_port"])
        self._send_json(200 if r.get("ok") else 409, r)

    def _admin_save_provider(self, body):
        """新增或更新一条 provider 配置（Web 面板表单提交）。

        只传 key_masked 不传 key 时视为"沿用原 Key"，避免掩码被误写回。
        """
        from . import config as C
        name = (body.get("name") or "").strip()
        if not name:
            self._send_json(400, {"ok": False, "detail": "配置名称不能为空"})
            return
        try:
            cfg = C.load_config()
        except C.ForeignConfigError as e:
            self._send_json(409, {"ok": False, "detail": str(e)})
            return

        existing = C.find_provider(cfg, name)
        kind = body.get("kind") or (existing or {}).get("kind") or "custom-anthropic"
        key = (body.get("key") or "").strip()
        if not key and existing:
            key = existing.get("key", "")          # 沿用旧 Key
        if not key:
            self._send_json(400, {"ok": False, "detail": "API Key 不能为空"})
            return

        if existing:
            rec = dict(existing)
            rec["kind"] = kind
            rec["base_url"] = (body.get("base_url")
                               or existing.get("base_url", "")).strip()
            rec["key"] = key
            if body.get("model"):
                rec["model"] = body["model"].strip()
            # 模板变化可能改了 mode/passthrough，需要重新取
            tpl = {}
            try:
                from . import providers as P
                tpl = P.template_for(kind)
            except KeyError:
                tpl = {}
            for field in ("mode", "passthrough", "auth_style", "thinking",
                          "force_tool_disables_thinking", "fetch_models"):
                if field in tpl:
                    rec[field] = tpl[field]
            C.update_provider(cfg, rec)
            action = "updated"
        else:
            try:
                rec = C.new_provider_record(
                    name, kind, key,
                    model=(body.get("model") or "").strip(),
                    base_url=(body.get("base_url") or "").strip())
            except KeyError as e:
                self._send_json(400, {"ok": False, "detail": f"未知类型: {e}"})
                return
            C.add_provider(cfg, rec)
            action = "created"

        if body.get("make_current"):
            C.set_current(cfg, name)
        C.save_config(cfg)

        # 运行中代理若正在用同名配置，热重载。
        # 代理空载时（新电脑「先开面板、后加配置」的典型路径）PROV_NAME 为
        # None，老判断会漏掉重载，导致面板显示「空闲」、Science 侧全部 503。
        # 因此空载且本次保存的正是当前配置时，必须装载。
        reloaded = False
        if state.PROV_NAME == name or (state.PROV_NAME is None
                                       and cfg.get("current") == name):
            ok, err = upstream.load_provider(name)
            reloaded = bool(ok)
            if not ok:
                log.warning("保存后热重载失败: %s", err)

        self._send_json(200, {
            "ok": True, "action": action, "name": name,
            "key_masked": C.mask_key(key), "reloaded": reloaded,
            "current": cfg.get("current"),
        })
        log.info("[admin] provider %s: %s (key=%s)", name, action,
                 C.mask_key(key))

    def _admin_delete_provider(self, body):
        from . import config as C
        name = (body.get("provider") or body.get("name") or "").strip()
        if not name:
            self._send_json(400, {"ok": False, "detail": "缺少 provider 名"})
            return
        try:
            cfg = C.load_config()
        except C.ForeignConfigError as e:
            self._send_json(409, {"ok": False, "detail": str(e)})
            return
        if not C.remove_provider(cfg, name):
            self._send_json(404, {"ok": False, "detail": f"配置不存在: {name}"})
            return
        C.save_config(cfg)
        # 删掉的正是运行态在用的配置：清空运行态，否则会继续拿已删除的
        # Key 打上游。清空后请求走 503，直到用户切换/添加配置。
        if state.PROV_NAME == name:
            with state.LOCK:
                state.clear_provider()
            log.info("[admin] 运行中的 provider 已被删除，代理转为空载")
        self._send_json(200, {"ok": True, "deleted": name,
                              "current": cfg.get("current")})
        log.info("[admin] provider 删除: %s", name)

    def _admin_save_settings(self, body):
        from . import config as C
        try:
            cfg = C.load_config()
        except C.ForeignConfigError as e:
            self._send_json(409, {"ok": False, "detail": str(e)})
            return
        s = body.get("settings") or {}
        for k in ("proxy_port", "sandbox_port"):
            if k in s:
                try:
                    v = int(s[k])
                except (TypeError, ValueError):
                    self._send_json(400, {"ok": False,
                                          "detail": f"{k} 必须是整数"})
                    return
                if not (1024 <= v <= 65535):
                    self._send_json(400, {"ok": False,
                                          "detail": f"{k} 超出有效端口范围"})
                    return
                cfg["settings"][k] = v
        if "open_browser" in s:
            cfg["settings"]["open_browser"] = bool(s["open_browser"])
        if "science_bin" in s:
            cfg["settings"]["science_bin"] = str(s["science_bin"] or "")
        if "sandbox_enabled" in s:
            cfg["settings"]["sandbox_enabled"] = bool(s["sandbox_enabled"])
        C.save_config(cfg)
        # 涉及运行环境的字段变更都需要重启沙箱才生效
        env_changed = s.get("sandbox_enabled") is not None
        port_changed = (cfg["settings"]["proxy_port"] != state_snapshot_port())
        need_restart = env_changed or port_changed
        msgs = []
        if port_changed:
            msgs.append("代理端口已变更")
        if env_changed:
            msgs.append("安全沙箱开关已变更")
        self._send_json(200, {
            "ok": True, "settings": cfg["settings"],
            "restart_needed": need_restart,
            "detail": ("；".join(msgs) + " —— 需重启沙箱：csx stop && csx start"
                       if need_restart else "已保存"),
        })

    def _admin_switch(self, body):
        name = body.get("provider")
        if not name:
            self._send_json(400, {"ok": False, "detail": "缺少 provider 名"})
            return
        # 切换前先校验 Key：不过不切，避免把会话打进坏配置
        v = upstream.verify_provider(name)
        if not v["ok"]:
            self._send_json(200, {"ok": False, "switched": False, "verify": v,
                                  "detail": f"校验未通过，已保持当前 provider：{v['detail']}"})
            return
        ok, err = upstream.load_provider(name)
        if not ok:
            self._send_json(500, {"ok": False, "switched": False, "detail": err})
            return
        # 同步 config.current，重启后仍生效
        try:
            from . import config as C
            cfg = C.load_config()
            C.set_current(cfg, name)
        except Exception as e:           # noqa: BLE001 - 同步失败只影响下次启动
            log.warning("同步 config.current 失败（不影响本次运行）: %s", e)
        log.info("[admin] 热切换 provider -> %s（Science 无需重启）", name)
        self._send_json(200, {"ok": True, "switched": True, "verify": v,
                              "provider": name})

    # ---------------- CONNECT 隧道 ----------------

    def do_CONNECT(self):
        target = self.path
        host = target.rsplit(":", 1)[0].strip("[]").lower().rstrip(".")
        if any(host == s or host.endswith("." + s) for s in K.BLOCKED_SUFFIXES):
            # 401（未登录）而非 403：让客户端判 logged-out 秒过，
            # 不当成组织/权限问题反复重试卡 "Switching organization"。
            log.info("CONNECT %s -> 401 未登录（Anthropic 域名 fast-fail）", target)
            self._connect_reply(401)
            return
        if not _tunnel_allowed(target, host):
            # 默认不会走到这里（_tunnel_allowed 恒真），保留作为将来收紧的挂载点
            log.warning("CONNECT %s -> 403 被隧道策略拒绝（设 %s=* 可全放行）",
                        target, K.TUNNEL_ALLOWLIST_ENV)
            self._connect_reply(403)
            return
        try:
            port = int(target.rsplit(":", 1)[1])
        except (ValueError, IndexError):
            self._connect_reply(400)
            return
        try:
            up = socket.create_connection((host, port), timeout=10)
        except Exception as e:           # noqa: BLE001
            log.info("CONNECT %s -> 502 上游连不上: %s", target, e)
            self._connect_reply(502)
            return
        self.send_response(200, "Connection Established")
        self.end_headers()
        try:
            self.wfile.flush()
        except Exception as e:           # noqa: BLE001
            log.debug("CONNECT 头刷新失败: %s", e)
        log.info("CONNECT %s -> 隧道建立，透传", target)
        try:
            self._tunnel(self.connection, up)
        finally:
            try:
                up.close()
            except Exception as e:       # noqa: BLE001
                log.debug("上游套接字关闭失败: %s", e)
        self.close_connection = True

    def _connect_reply(self, code):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    @staticmethod
    def _tunnel(client, upstream_sock):
        socks = [client, upstream_sock]
        while True:
            try:
                r, _, _ = select.select(socks, [], [])
            except Exception as e:       # noqa: BLE001 - select 被中断即收尾
                log.debug("select 异常结束隧道: %s", e)
                return
            for s in r:
                other = upstream_sock if s is client else client
                try:
                    data = s.recv(65536)
                except Exception as e:   # noqa: BLE001
                    log.debug("隧道 recv 失败: %s", e)
                    return
                if not data:
                    return
                try:
                    other.sendall(data)
                except Exception as e:   # noqa: BLE001
                    log.debug("隧道 send 失败: %s", e)
                    return

    # ---------------- Anthropic 原生透传 ----------------

    def _handle_anthropic(self, areq):
        src = areq.get("model", "?")
        target = modelmap.resolve_model(src)
        body = dict(areq)
        body["model"] = target
        if body.get("max_tokens"):
            body["max_tokens"] = modelmap.clamp_max_tokens(
                body["max_tokens"], target)
        modelmap.normalize_thinking(body)
        stream = bool(body.get("stream"))
        n_tools = len(body.get("tools") or [])
        url, _, mode = upstream.upstream_urls()
        pname = state.current()
        log.info("POST /v1/messages  %s->%s  stream=%s tools=%d msgs=%d  "
                 "(入站鉴权已剥离, 直连 %s)",
                 src, target, stream, n_tools,
                 len(body.get("messages") or []), pname)
        headers = {"content-type": "application/json"}
        headers.update(upstream.auth_headers(mode))
        data = json.dumps(body).encode()
        headers_sent = False
        try:
            if stream:
                r, first, ct = upstream.open_stream(url, data, headers)
                with r:
                    self.send_response(200)
                    self.send_header("Content-Type", ct)
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    headers_sent = True
                    self._write_chunk(first)
                    while True:
                        try:
                            chunk = r.read(4096)
                        except Exception as e:   # noqa: BLE001
                            log.warning("流中断（头已发），SSE error 收尾: %s", e)
                            self._sse_error_and_terminate(str(e))
                            return
                        if not chunk:
                            break
                        self._write_chunk(chunk)
                    self.wfile.write(b"0\r\n\r\n")
                log.info("  <- %s 流式透传 OK", pname)
            else:
                body_bytes, ct = upstream.http_post(url, data, headers)
                self.send_response(200)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(body_bytes)))
                self.end_headers()
                headers_sent = True
                self.wfile.write(body_bytes)
                log.info("  <- %s 非流式透传 OK", pname)
        except urllib.error.HTTPError as e:
            detail = upstream._scrub_detail(e.read().decode("utf-8", "replace"))
            log.warning("  !! 上游 HTTP %s: %s", e.code, detail)
            if not headers_sent:
                code = e.code if e.code in (401, 403, 429) else 502
                self._send_json(code, {"type": "error", "error": {
                    "type": "api_error", "message": f"upstream {e.code}: {detail}"}})
        except Exception as e:          # noqa: BLE001
            log.warning("  !! 代理异常: %s", e)
            if headers_sent:
                try:
                    self._sse_error_and_terminate(str(e))
                except Exception:       # noqa: BLE001 - 连接已断则放弃
                    pass
            else:
                self._send_json(502, {"type": "error", "error": {
                    "type": "api_error", "message": str(e)}})

    def _write_chunk(self, b):
        if b:
            self.wfile.write(hex(len(b))[2:].encode() + b"\r\n" + b + b"\r\n")

    # ---------------- OpenAI 翻译路径 ----------------

    def _handle_openai(self, areq):
        model_id = areq.get("model", "claude-sonnet-5")
        stream = bool(areq.get("stream"))
        oreq = translate.anthropic_to_openai(areq)
        url, _, mode = upstream.upstream_urls()
        pname = state.current()
        log.info("POST /v1/messages  %s->%s stream=%s tools=%d msgs=%d  "
                 "(入站鉴权已剥离, 翻译 %s)",
                 model_id, oreq["model"], stream,
                 len(oreq.get("tools", [])), len(oreq["messages"]), pname)
        headers = {"Content-Type": "application/json"}
        headers.update(upstream.auth_headers(mode))
        data = json.dumps(oreq).encode()
        try:
            raw, _ct = upstream.http_post(url, data, headers)
            aresp = translate.openai_to_anthropic(json.loads(raw), model_id)
            if stream:
                self._replay_as_sse(aresp)
            else:
                self._send_json(200, aresp)
            log.info("  <- %s OK (blocks=%d stop=%s)",
                     pname, len(aresp["content"]), aresp["stop_reason"])
        except urllib.error.HTTPError as e:
            detail = upstream._scrub_detail(e.read().decode("utf-8", "replace"))
            log.warning("  !! 上游 HTTP %s: %s", e.code, detail)
            code = e.code if e.code in (401, 403, 429) else 502
            self._send_json(code, {"type": "error", "error": {
                "type": "api_error", "message": f"upstream {e.code}: {detail}"}})
        except Exception as e:          # noqa: BLE001
            log.warning("  !! 代理异常: %s", e)
            self._send_json(502, {"type": "error", "error": {
                "type": "api_error", "message": str(e)}})

    def _replay_as_sse(self, aresp):
        """把一次性响应回放成 Anthropic SSE 事件序列。

        已知边界：非真流式，首 token 延迟等于完整响应时间。
        换真流式需要为每家 OpenAI 兼容上游做增量 tool_call 参数拼接，
        收益不抵复杂度，故保持现状（README 已知边界章节有说明）。
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        blocks = aresp.get("content") or [{"type": "text", "text": ""}]
        self._sse("message_start", {"type": "message_start", "message": {
            "id": aresp.get("id", "msg_proxy"), "type": "message",
            "role": "assistant", "model": aresp.get("model"), "content": [],
            "stop_reason": None, "stop_sequence": None,
            "usage": aresp.get("usage",
                               {"input_tokens": 0, "output_tokens": 0})}})
        self._sse("ping", {"type": "ping"})
        for idx, blk in enumerate(blocks):
            if blk.get("type") == "tool_use":
                self._sse("content_block_start", {
                    "type": "content_block_start", "index": idx,
                    "content_block": {"type": "tool_use", "id": blk.get("id"),
                                      "name": blk.get("name"), "input": {}}})
                self._sse("content_block_delta", {
                    "type": "content_block_delta", "index": idx,
                    "delta": {"type": "input_json_delta",
                              "partial_json": json.dumps(
                                  blk.get("input", {}), ensure_ascii=False)}})
            else:
                self._sse("content_block_start", {
                    "type": "content_block_start", "index": idx,
                    "content_block": {"type": "text", "text": ""}})
                self._sse("content_block_delta", {
                    "type": "content_block_delta", "index": idx,
                    "delta": {"type": "text_delta", "text": blk.get("text", "")}})
            self._sse("content_block_stop",
                      {"type": "content_block_stop", "index": idx})
        self._sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": aresp.get("stop_reason", "end_turn"),
                      "stop_sequence": None},
            "usage": {"output_tokens": (aresp.get("usage") or {})
                      .get("output_tokens", 0)}})
        self._sse("message_stop", {"type": "message_stop"})
        self.wfile.write(b"0\r\n\r\n")


def serve(port, handler=H):
    """创建并返回绑定 127.0.0.1 的服务器（带端口重试）。"""
    import time
    last = None
    for attempt in range(K.PORT_PROBE_TIMEOUT):
        try:
            return ThreadingHTTPServer(("127.0.0.1", port), handler)
        except OSError as e:
            last = e
            if attempt == K.PORT_PROBE_TIMEOUT - 1:
                raise RuntimeError(
                    f"端口 {port} 无法绑定：{e}。可能被占用"
                    f"（结束占用进程，或在配置里换 proxy_port）。") from e
            time.sleep(0.3)
    raise RuntimeError(f"端口 {port} 无法绑定：{last}")
