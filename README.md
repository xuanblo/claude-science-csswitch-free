# CSswitch-Win

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)
[![自测](https://img.shields.io/badge/tests-offline-success.svg)](tests/selftest.py)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-blue.svg)](#macos--linux-安装与快速开始)

Windows / macOS / Linux 版 Claude Science 第三方模型切换工具。
直接代理并切换本机已安装的原生 Claude Science，把推理请求导流到你自己的第三方
模型 API（DeepSeek / 通义千问 / 智谱 GLM / Kimi / MiniMax / OpenRouter /
任意 OpenAI·Anthropic 兼容端点）。

纯 Python 实现（代理零第三方依赖，虚拟登录仅需 `cryptography`），CLI 形态。

---

## ⚠️ 免责声明

**请先读这一节，再决定是否使用。**

1. **本项目非 Anthropic 官方产品**，与 Anthropic 无隶属、赞助或合作关系。
   「Claude」「Claude Science」「Anthropic」为其商标，本项目仅作兼容性研究。

2. **使用本工具不改变 Claude Science 的许可条款**。你须自行确保使用方式
   不违反 [Anthropic 服务条款](https://www.anthropic.com/legal/commercial-terms)；
   相关责任由使用者自行承担。

3. **虚拟登录是纯本地行为**：仅在隔离沙箱目录内生成一个假账号
   （`virtual@localhost.invalid`）的本地令牌，**零 Anthropic 网络交互**，
   不涉及伪造他人账号、不绕过付费墙、不共享任何凭证。

4. **费用**：所有上游推理均由**你自己的第三方 Key** 计费，作者不承担任何费用。

5. **可能失效**：本项目依赖对客户端私有格式的逆向。Claude Science 升级后
   项目可能整体失效，这是被逆向对象的固有属性，非缺陷。

6. 请遵守所在地法律法规。

---

## 兼容性

| Claude Science | 状态 | 备注 |
|---|---|---|
| **0.1.61** | ✅ 实测通过 | 2026-10 验证基准 |
| 其他版本 | ❓ 未验证 | 欢迎提 issue 反馈 |

失效时的表现是**显式的**：`oauth_forge` 写入后有自校验（解密回读比对），
格式变更会以「自校验失败」的形式报错，不会静默损坏。
跑 `tests/selftest.py` 可确认你的环境是否仍正常（不依赖真实客户端；
便携版用内置运行时执行：`runtime\python\python.exe tests\selftest.py`）。

---

## macOS / Linux 安装与快速开始

网页控制台可以跨平台复用，但 **Claude Science 后台仍是本地原生程序**：macOS、
Linux 必须分别安装对应系统的官方 Science，仓库里的 Windows `.exe` 不能通用。
本工具会依次查找项目内 Unix 二进制、`settings.science_bin`、`PATH` 和常见安装路径。

### 1. 准备环境

- macOS 或 Linux 的原生 Claude Science，安装后确认 `claude-science --version` 可用；
- Python 3.9 或更新版本，也可把便携 Python 放在 `runtime/python/bin/`；
- Linux 安全沙箱还需要 `bubblewrap`（命令名 `bwrap`）与 `socat`。
  Ubuntu/Debian 可一次安装系统依赖：

```bash
sudo apt install python3 python3-venv bubblewrap socat
```

macOS 不需安装上述两个 Linux 沙箱工具。Python 依赖 `cryptography` 会在首次启动时
自动安装到项目 `.venv`，不会污染系统 Python；可用 `CSSWITCH_PYTHON` 指定解释器。

### 2. 启动控制台

在本目录执行一次：

```bash
chmod +x 启动.sh
```

macOS 与 Linux 均在终端运行 `./启动.sh`。

启动器默认打开 Web 控制台。首次使用在面板中添加 Provider 与 API Key，再启动
Science 即可。命令行示例：

```bash
./启动.sh doctor       # 检查 Science、Python 与 Linux 沙箱依赖
./启动.sh status
./启动.sh start
./启动.sh stop
./启动.sh panel --no-open  # 无图形界面时只打印控制台地址
```

macOS / Linux 数据默认位于 `~/.csswitch-win`（保留历史目录名以兼容旧配置），
可用 `export CSSWITCH_HOME=/你的/数据目录` 改址。配置、日志与沙箱都在该目录下。
`启动.sh` 会优先使用 `runtime/python/bin/python3` 或 `runtime/python/bin/python`；
没有便携 Python 时才回退到 `CSSWITCH_PYTHON` 或系统 `python3`。Windows
`runtime/python` 与 `claude-science.exe` 不会被 Unix 启动器误用。

---

## `runtime/` 运行时目录

源码仓库会跟踪以下占位说明，但不会把 Python、Claude Science 和平台二进制提交到
Git：

```text
runtime/README.md
runtime/claude-science/README.md
runtime/python/README.md
```

如果你拿到的是**完整便携发行包**，直接双击 `便携启动.bat`（Windows）或运行
`./启动.sh`（macOS / Linux）即可。

如果你只拿到**Git 源码仓库**，请按 `runtime/README.md` 下载当前平台的原生
Claude Science，并准备便携 Python：

```text
Windows：
  runtime\python\python.exe
  runtime\claude-science\claude-science.exe

macOS / Linux：
  runtime/python/bin/python3
  runtime/claude-science/claude-science
```

Claude Science 放入 `runtime/claude-science/` 后，本工具会以最高优先级自动发现它；
不需要再安装系统级 Science，也不需要手动修改 `science_bin`。

---

## Windows 安装与快速开始

**完整便携包无需安装任何东西**：整个文件夹拷到任意 Windows 电脑（U 盘、网盘、微信传文件夹
都行），双击 **`便携启动.bat`** 即可。完整便携包内置 `runtime\python`（Python 3.11 +
cryptography）与 `runtime\claude-science\claude-science.exe`，不依赖系统 Python，
也不依赖系统装过 Claude Science。Git 源码仓库只保留这些目录的占位说明，二进制需按
上文放入或通过完整便携发行包获得。

### 方式一：图形界面（推荐）

双击 **`便携启动.bat`**，浏览器会自动打开控制台。里面可以：

- 一键启动 / 停止沙箱 Claude Science（带进度提示，首次需 30~90 秒装分析环境）
  - 点「打开 Science」会**自动完成登录**，直接进主界面，不再停在 Sign in 页
  - 登录链接是**一次性密码（3 分钟失效）**，所以面板不显示固定链接，
    每次打开都现取一条新的
- 新增 / 编辑 / 切换 / 校验 / 删除第三方模型配置
- 查看代理与沙箱的实时状态、设置端口
- **宿主文件夹授权**：把 `D:\Paper` 这类目录登记进来，之后 Claude Science
  **每个对话**的目录选择器都能选到它，可直接读写（见下方「让对话读你本机的项目」）
- **安全沙箱开关**：默认启用（Claude 看不到你的真实文件）；关闭后
  Claude 可直接读写本机文件、不再逐项弹权限审批

> ⚠️ 关闭安全沙箱意味着 Claude 对**整个用户目录**有完全读写与网络访问权。
> 即便如此，本工具的**虚拟登录仍始终写在沙箱目录**
> （`D:\CSSwitch-Win-Data\sandbox\home\.claude-science`），
> 不会碰你真实的 `~/.claude-science` 登录态。
>
> 改动沙箱开关需重启沙箱才生效（面板会提示，或 `便携启动.bat stop` →
> `便携启动.bat start`）；
> **授权文件夹立即生效，不用重启**。

#### 让对话读你本机的项目

Claude Science 的**每个对话**都会在它自己新建的沙箱工作区里运行
（`D:\CSSwitch-Win-Data\sandbox\data\orgs\<org>\workspaces\<uuid>\`），
**它看不见**你本机的项目文件夹 —— 无论安全沙箱开关怎么切都一样。

入口是 daemon 的 **host grants**（偏好设置）：把宿主目录登记为授权根，
授权后对话里的 Claude 就能直接读写它。

**在对话里怎么用**：直接说「读一下 `D:\Paper\报告.md`」。Science 内置了
`list_host_grants` / `request_host_access` 两个工具 —— 它会先查已有授权，
没有就弹窗向你申请（可选只读或可读写）。面板里预先授权就是为了让它
不用每次都问。

> ⚠️ **授权 ≠ 换工作目录。** 每个对话的**工作目录**始终是Science 自建的
> `orgs/<org>/workspaces/<uuid>/`，这一点无法通过本工具改变（daemon 的
> `serve` 没有 `--workspace` / `--cwd` 参数，工作目录由每个对话自己决定）。
> 授权解决的是「**能不能访问**」——授权后 Claude 可以在对话里用绝对路径
> 直接读写你的项目文件，效果上等同于在该项目里工作。
>
> 如果你要的是「对话的落盘位置就在 `D:\Paper`」，那是 Science 界面侧的
> 项目目录选择，不在本工具范围内。

> ⚠️ **授权只在「安全沙箱」启用时才有意义。**
> 关闭安全沙箱后（本工具传了 `--dangerously-no-sandbox`），整个文件系统
> 本来就可访问 —— daemon 会直接回
> `The filesystem sandbox is not active, so this path is already accessible
> to compute — no grant was needed or recorded`，**不落库、列表恒为空**。
> 面板会明确显示这一点，不会谎报成功。此时目录访问不受限，
> 但同样没有授权列表可维护。

面板「宿主文件夹授权」卡片可以：

| 操作 | 说明 |
|---|---|
| 授权文件夹 | 填绝对路径 + 选「可读写」或「只读」，点「授权文件夹」 |
| 浏览… | 看已授权目录下的内容，确认路径没填错 |
| 撤销 | 只让 Claude 看不到该目录，**不会删除任何文件** |

已知约束：

- **不能降级权限**：已有「可读写」授权的路径无法再改成「只读」——daemon 担心
  共享的 live bind 变成只读而授权行仍是 rw，状态自相矛盾。要改就先撤销再重新授权。
- **只能浏览授权根与 `$HOME`**：`host-browse` 对其它路径一律
  `Directory is not under $HOME or a granted root`。

命令行等价操作（需要 `admin_token`，见 `D:\CSSwitch-Win-Data\state.json`）：

```bash
curl -s -X POST http://127.0.0.1:<proxy_port>/__admin/host-grant/add \
  -H "x-csx-admin: <admin_token>" -H "Content-Type: application/json" \
  -d "{\"path\": \"D:\\\\Paper\", \"mode\": \"rw\"}"
```

返回里 `recorded` 字段如实说明有没有真的落库（沙箱关闭时为 `false`）。

底层调用的是 daemon 的 `/api/preferences/host-grants`，需要走一次
nonce 登录换会话（`operon_auth` cookie），写操作还要求 `Origin` 与
`x-operon-csrf` 头 —— 这些都由 `csswitch/hostgrant.py` 自动处理，
调用方无需关心。

### 「打开 Science」为什么不用点 Sign in

Science 的登录 nonce 落地页**不是一个自动登录的入口，而是一个需要点击的
表单**（`<form id="signin-form">`），所以直接 `window.open(nonce链接)`
只会停在 "Sign in" 页；nonce 又是，一次性、3 分钟过期，点晚了还会报
`link has expired`。

本工具的解法是加一层**代理侧中转**：点「打开 Science」时打开
`/__admin/science-open`，由代理在**服务端**先把一次性 nonce 用掉、换出
`operon_auth` 会话 cookie，再用 `Set-Cookie` 注入浏览器，最后 `302` 跳到
daemon 首页 —— 所以落地即主界面。

两个容易踩的细节：

- **`operon_csrf` 不能加 `HttpOnly`**：前端要读它来填 `x-operon-csrf` 请求头，
  加了之后界面上的所有写操作都会 403。只有 `operon_auth` 才设 `HttpOnly`
- **cookie 域要跟主机名一致**：cookie 不区分端口、只认主机名。若你从
  `127.0.0.1` 打开控制台，中转页会先跳到自己的 `localhost` 地址再登录，
  这样设的 cookie Science 才收得到

安全边界没有放松：未登录时直接访问 Science 首页仍然返回 401，登录仍走
daemon 自己的 nonce 校验。


首次运行会自动检查 `cryptography` 依赖，缺失时补装到内置运行时（需要联网）。

### 方式二：命令行

`便携启动.bat <命令>` 等价于 `python csx.py <命令>`：

```bat
:: 1. 诊断环境（确认内置运行时、claude-science.exe 与版本）
便携启动.bat doctor

:: 2. 新建第三方配置（交互式，或全参数非交互）
便携启动.bat add
便携启动.bat add --name ds-pro --kind deepseek --key sk-你的KEY

:: 3. 一键开始：起代理 → 写虚拟登录 → 起沙箱 Science → 打开浏览器
便携启动.bat start
```

浏览器打开的标签页就是**沙箱 Science**。

> **关于选择器里的 “Opus 5 / Sonnet 5”**：那是 Science 客户端内置的档位
> 文字（它按 `claude-opus-*` / `claude-sonnet-*` 这类壳 id 命名），**不是**
> 实际在跑的模型。真正发给上游的模型名由代理改写：选择 Opus 档时 Science
> 发 `claude-opus-5`，代理按 `model_map` / `models` / `model` 换成你配置的
> 真实模型。想知道这一刻到底用了什么，看 `logs\proxy.log` 里的
> `POST /v1/messages  claude-opus-5-><你的模型>` 那一行。
>
> 如果面板里某一行的状态是红点「空闲」而它又带「默认」标签，说明代理进程
> **没有装载**这条配置，Science 的请求会被直接回绝（表现为
> “Claude is temporarily unavailable”）。点该行的「切换」即可装载，无需重启。

> `便携启动.bat` 默认不带参数 = 打开 Web 控制台；加 `--nopause` 可在脚本里调用。
> 程序整目录拷走即可；数据默认落在 `D:\CSSwitch-Win-Data`（可用 `CSSWITCH_HOME` 覆盖）。

**切换模型**（Science 不重启）：

```bat
便携启动.bat use glm          :: 热切换，进行中的会话下一条请求即走新模型
```

**切回官方 Claude**（真实登录，不走代理）：

```bat
便携启动.bat official
```

---

### 便携版目录

```
CSswitch-Win\              ← 程序文件夹，可整目录拷走
├─ 便携启动.bat          ← 双击这个
├─ csx.py / csswitch\    ← 程序本体（纯 Python，随包分发）
├─ runtime\              ← 便携运行时（约 236 MB）
│  ├─ python\              Python 3.11.15 + cryptography 46.0.7
│  └─ claude-science\      Claude Science 0.1.61
└─ tests\ / tools\        ← 自测与运维脚本，运行时不依赖

D:\CSSwitch-Win-Data\      ← 数据目录（默认，首次运行后自动生成）
├─ config.json             第三方配置与 API Key
├─ encryption.key          本机密钥
├─ sandbox\                沙箱用户目录与会话数据（占 10GB+）
└─ logs\                   运行日志
```

**首次启动会自动完成两件事**（只在新电脑上发生，各几秒到几十秒）：

- 生成虚拟登录令牌并存到 `D:\CSSwitch-Win-Data\encryption.key`
- 首次连接时向 Claude 官方申请 OAuth 授权（需要你在弹出的浏览器里点一次确认）

之后再次启动都是秒开。

**`D:\CSSwitch-Win-Data` 里是你的 API Key 和登录凭证，等同于密码文件**。
数据不在程序目录内，分享程序文件夹时天然不会带上它。

#### 便携版常见问题

**双击后一闪而过 / 报错？** 按任意键会停在错误画面上，把提示发给我即可；也可以
在命令行里跑 `便携启动.bat doctor` 看完整输出。

**一键启动报「配置不存在或未设置当前项: None」？** 说明
`D:\CSSwitch-Win-Data\config.json` 里还没有配置，或当前项为空。在面板里添加
一条 API Key 配置并设为当前即可。

**一定要拷整个 runtime 吗？** 是。`claude-science.exe` 单文件就 155 MB，缺了没法运行。

**能放在 D 盘 / U 盘 / 网盘同步目录里吗？** 可以，路径不限（含中文、空格也支持）。
程序目录里不含凭证（数据在 `D:\CSSwitch-Win-Data`，需另行备份）。

**怎么更新 Claude Science？** 替换 `runtime\claude-science\claude-science.exe`
为新版本即可。改之前建议先备份 `D:\CSSwitch-Win-Data`（升级后如遇异常，把旧 exe 换回来即可）。

#### 导出对话

双击 `一键导出ClaudeScience对话.bat`，弹窗选择导出目录即可。按对话导出为
`<导出目录>\<日期>_<标题>\`（正文 + `attachments\` 全部产物），并生成 `INDEX.md` 索引；
重复运行是增量的，不会重复拷贝。

```bat
:: 直接指定目录（不弹窗）
一键导出ClaudeScience对话.bat --out "E:\我的导出"

:: 只看计划，不写文件
一键导出ClaudeScience对话.bat --dry-run

:: 导出旧版（WSL）数据
一键导出ClaudeScience对话.bat --out "E:\我的导出" ^
  --data-dir "\\wsl$\Ubuntu-22.04\home\<用户名>\.csswitch\sandbox\home\.claude-science"
```

默认数据源是当前沙箱（`D:\CSSwitch-Win-Data\sandbox\data`）；对话在旧 WSL 数据里时
用 `--data-dir` 指定。

---

## 命令参考

> **命名对应**：仓库与安装包名 `cswitch-win`、Python 包名 `csswitch`、
> 命令名 `csx`。三者指同一个东西，`csx.py` 是 `csswitch.cli:main` 的入口。

| 命令 | 作用 |
|---|---|
| `csx --version` | 版本号 |
| `csx doctor` | 环境诊断：Python/依赖/Science 安装/端口/路径/日志目录 |
| `csx list` | 列出全部配置（▶ 标记当前项，Key 掩码显示） |
| `csx add [--name --kind --key --model --base-url]` | 新建配置（缺参交互；非交互自动取默认） |
| `csx remove <name>` | 删除配置 |
| `csx use <name>` | 切换当前配置；代理在跑则**热切换**（先校验 Key，不过不切） |
| `csx verify <name>` | 只校验 Key（最小请求，花费≈0），不切换 |
| `csx start [--sandbox-port N]` | 一键开始（代理 + 沙箱 + 浏览器） |
| `csx where` | 诊断数据落盘位置（新电脑查环境用） |
| `csx stop` | 停止沙箱与代理 |
| `csx official [--port N]` | 官方 Claude 模式 |
| `csx status` | 总览：代理/沙箱/当前配置/Science 路径 |
| `csx proxy-start` / `csx proxy-stop` | 单独控制代理（调试） |
| `csx panel` | 打开 Web 控制台（图形化配置 + 一键启停沙箱） |

内置类型 `--kind`：`deepseek`（默认）、`qwen`、`glm`、`kimi`、`minimax`、
`openrouter`、`custom-anthropic`、`custom-openai`。

> 排障时设 `CSSWITCH_DEBUG=1` 可看到 debug 级日志（预期内失败的细节）。

---

## 工作原理

### 1. 推理劫持：`ANTHROPIC_BASE_URL`

Science 启动后，推理请求发往哪里**只由环境变量 `ANTHROPIC_BASE_URL` 决定**。
把它指向本地代理，代理就能：

- **剥离** Science 附带的 OAuth 凭证（`Authorization` / `x-api-key`），绝不外泄；
- **注入** 第三方 API Key（只驻内存，不落命令行、不入日志）；
- **按需转换协议**：Anthropic 兼容端点原生透传（thinking/tool_use 字节级保真）；
  OpenAI 兼容端点做双向协议翻译。

### 2. 虚拟登录：沙箱内伪造 OAuth 令牌

Science 的凭证解析器**只认 OAuth**（手动 API Key 路径被写死拒绝），不登录就无法启动。
解法是在沙箱 auth_dir 里**本地自造**一套登录状态：

```
<auth_dir>/
├── encryption.key            # 换行分隔 KEY=base64，四把钥匙（≥16B）
├── active-org.json           # {"org_uuid": <uuid>}
└── .oauth-tokens/
    └── <account_uuid>.enc    # 目录内【恰好一个】.enc
```

`.enc` 内容为 v2 格式：

```
"v2:" + base64( IV(12) ‖ AES-256-GCM(密文‖tag(16)) )
derivedKey = HKDF-SHA256(ikm=base64(OAUTH_ENCRYPTION_KEY),
                         salt=空, info="operon:aes-256-gcm:oauth", 32)
AAD = "v2:oauth"
明文 = JSON{ access_token, email=virtual@localhost.invalid,
             token_expires_at=2099-01-01（远期 → 绝不联网刷新）, ... }
```

### 3. 隔离沙箱 + 防卡死

- 独立 `USERPROFILE`/`HOME`/`APPDATA`/`LOCALAPPDATA`/`TEMP`、独立 data-dir、
  独立端口；真实 `~/.claude-science` **零读取、零修改、零删除**；
- 启动时 Science 会阻塞式访问 `api.anthropic.com` 的 profile 接口，到不了就卡在
  "Switching organization"。解法：`HTTPS_PROXY` 指向本地代理，对 Anthropic 域名的
  `CONNECT` 立即返回 **401（未登录）** 让其秒过，而非 403（会当成权限问题反复重试）；
  `NO_PROXY` 放行 127.0.0.1 保证推理直连本地。

### 4. Windows 原生适配（与 macOS 参考实现的差异）

| 维度 | macOS 参考实现 | 本工具（Windows 原生） |
|---|---|---|
| 家目录隔离 | `HOME=$SANDBOX_HOME` | `USERPROFILE` + `HOME` + `APPDATA` + `LOCALAPPDATA` + `TEMP` 全覆盖 |
| 钥匙串弹窗 | 需建沙箱 login.keychain-db 消除弹窗 | **不存在此问题**（Windows 无 keychain） |
| 运行时资产 | APFS clone `bin/conda/runtime` | 不需要，直接跑官方安装的 exe |
| 二进制 | `/Applications/Claude Science.app/.../bin/claude-science` | 自动发现：PATH → `%LOCALAPPDATA%\Programs\` → 开始菜单快捷方式 |
| 数据目录 | `~/.claude-science` | `%USERPROFILE%\.claude-science` |
| 官方端口 | 8765 | 8000（`serve` 默认），沙箱默认 8990，占用自动顺延 |
| 实现语言 | Rust(Tauri) + Python 代理 | 纯 Python，CLI |
| 切换体验 | 改配置后需重启代理 | **热切换管理 API**：Science 不重启 |

---

## 架构

```
┌─────────────────────────────────────────────────────────────┐
│  csx start（CLI 编排）                                        │
│                                                             │
│  ① csswitch/proxy.py + server.py  127.0.0.1:18991（回环）   │
│     · path-secret 鉴权（ANTHROPIC_BASE_URL 路径段）           │
│     · 64MB 请求体上限（超出 413）                            │
│     · /v1/messages  剥离 OAuth → 注入第三方 Key               │
│         ├─ Anthropic 兼容端点 → 原生透传（SSE 字节级）         │
│         └─ OpenAI 兼容端点   → translate 协议翻译            │
│     · /v1/models     壳 id 平铺 + 真实模型名显示              │
│     · CONNECT 隧道    Anthropic 域名 401 fast-fail；           │
│                       其余目标默认全部放行                   │
│     · /__admin/*     管理 API（热切换，admin token）          │
│                                                             │
│  ② csswitch/oauth_forge.py  沙箱 auth_dir 写虚拟登录          │
│     encryption.key + .oauth-tokens/<uuid>.enc + active-org   │
│                                                             │
│  ③ csswitch/sandbox.py     隔离环境启动 Science               │
│     USERPROFILE/APPDATA → 沙箱；--data-dir → 沙箱            │
│     ANTHROPIC_BASE_URL=http://127.0.0.1:18991/<secret>       │
│     HTTPS_PROXY=http://127.0.0.1:18991  NO_PROXY=127.0.0.1   │
│     claude-science serve --port 8990 --no-browser --detached │
└─────────────────────────────────────────────────────────────┘
          │ 推理请求（Bearer 虚拟 OAuth，入站即剥）
          ▼
   本地代理（127.0.0.1:18991）
          │ 换 Key / 按需转协议
          ▼
   DeepSeek 原生端点 / DashScope OpenAI 兼容 / 任意兼容端点
```

## 项目结构

```
cswitch-win/
├── LICENSE                   # MIT + 致谢两个参考项目
├── README.md                 # 唯一文档：使用说明 + 原理 + 架构
├── 便携启动.bat               # Windows 启动器（双击=打开控制台，自带运行时）
├── 启动.sh                     # macOS / Linux 启动器
├── 一键导出ClaudeScience对话.bat  # Windows 导出对话（双击弹窗选目录）
├── csx.py                    # CLI 入口
├── runtime/                  # 便携运行时：Python 3.11 + Claude Science（不入库）
├── pyproject.toml            # 包元数据 + csx 入口 + ruff 配置
├── requirements.txt          # cryptography>=41,<47
├── requirements-lock.txt     # 精确版本（可重现安装）
├── .github/workflows/ci.yml  # Windows CI × py3.9/3.11/3.13
├── csswitch/
│   ├── __init__.py           # 版本元信息
│   ├── constants.py          # 全部硬编码常量（端口/超时/壳 id/上限/隧道策略）
│   ├── state.py              # 运行态（LOCK / PROV / KEY / secret / token）
│   ├── logging_setup.py      # 统一日志（轮转 + 脱敏）
│   ├── providers.py          # 内置 Provider 注册表
│   ├── config.py             # 配置管理（原子写 + ACL 收紧 + 官方配置护栏）
│   ├── oauth_forge.py        # 虚拟登录（HKDF + AES-256-GCM + 四道护栏）
│   ├── modelmap.py           # 模型名解析 / thinking 归一化 / /v1/models
│   ├── translate.py          # Anthropic ⇄ OpenAI 协议翻译（纯函数）
│   ├── upstream.py           # 上游 HTTP 客户端 / 重试 / Key 校验
│   ├── lifecycle.py          # 沙箱异步启停（状态机 + 后台线程）
│   ├── server.py             # HTTP 层（class H）+ 管理 API
│   ├── hostgrant.py          # 宿主文件夹授权（daemon host-grants 客户端）
│   ├── sandbox.py            # 沙箱启动/停止/端口解析/二进制发现
│   ├── cli.py                # 命令界面
│   └── web/panel.html        # Web 控制台（单文件，无外部依赖）
├── tools/                    # 本机运维与验证脚本（非运行时依赖）
│   ├── cs_export.py          # 导出 Science 对话与产物（被导出 .bat 调用）
│   ├── cs_export_config.json # 导出配置（跳过项目/附件子目录等）
│   ├── setup_local.py        # 批量配置端口与 provider
│   ├── verify_link.py        # 推理链路端到端验证
│   ├── check_tunnel.py       # CONNECT 隧道策略验证
│   ├── check_admin_api.py    # 管理 API 验证
│   ├── check_sandbox_ui.py   # 面板一键启停闭环验证
│   ├── check_fresh_url.py    # 登录链接新鲜度验证（nonce 一次性）
│   ├── check_grants_api.py   # 宿主文件夹授权管理 API 端到端验证
│   ├── check_edit_key.py     # 编辑 Key 闭环 + OpenAI 兼容端点探测器
│   ├── check_auto_login.py   # 自动跳过 Sign in 的中转链路验证
│   ├── check_science_location.py  # 诊断环境装在哪个盘（新电脑用）
│   ├── launch_detached.py    # 脱离会话启动（进程常驻）
│   └── diag_status.py        # 状态诊断
└── tests/
    ├── selftest.py           # 离线自测（零依赖，无需真实 API）
    └── test_all.py           # pytest 适配层（CI 用）
```

运行时数据默认落在 `D:\CSSwitch-Win-Data\`（`便携启动.bat` 设置
`CSSWITCH_HOME=D:\CSSwitch-Win-Data`，见下方「数据目录」）：

```
D:\CSSwitch-Win-Data\           # 即 CSSWITCH_HOME 指向的目录
├── config.json                 # 全部配置 + 当前项（Key 明文，ACL 仅当前用户）
├── state.json                  # 运行态（secret/admin token/pid，每次启动重新生成）
├── logs/                       # 轮转日志（2MB × 3）
└── sandbox/
    ├── home\.claude-science\   # 沙箱 auth_dir（虚拟登录写这里）
    └── data\                   # 沙箱 data-dir（--data-dir）
```

### 数据目录

沙箱里的 conda 分析环境会长到 **10GB 以上**（实测 10.86 GB，含
`claude-science-mcp` / `python` / `r` 三个 env），所以默认落在 D 盘而不是
程序目录。`config.py` 原生支持 `CSSWITCH_HOME` 改址，**代码零改动**：

- `便携启动.bat` 内置 `CSSWITCH_HOME=D:\CSSwitch-Win-Data`（仅在你没自己设过
  时才生效，所以仍可用系统环境变量覆盖到别处）
- 控制台「端口与运行时」里显示的**配置路径**就是当前实际路径，可直接确认

**要换位置的正确做法**（别直接剪切，conda 环境里有硬编码的绝对路径）：

```bat
:: 1. 先停
便携启动.bat stop

:: 2. 复制（robocopy 保留属性、可断点续传，比 Explorer 拖 11GB 稳）
robocopy "D:\CSSwitch-Win-Data" "E:\CSswitch-Data" /E /COPY:DAT /R:1 /W:1 /MT:8

:: 3. 设 CSSWITCH_HOME=E:\CSswitch-Data 后再启动，确认面板路径正确
便携启动.bat

:: 4. 确认无误后再删旧目录，释放空间
rmdir /s /q "D:\CSSwitch-Win-Data"
```

> ⚠️ 第 4 步务必等第 3 步验证通过再执行。conda 环境里有硬编码的绝对路径，
> 直接剪切到 D 盘会导致 Science 的分析环境失效；**复制**则没问题，因为
> 它整体由新的 `HOME` 推导。

> ⚠️ `config.json` 含**明文 API Key**。`.gitignore` 已覆盖全部运行时数据；
> 若你把 `CSSWITCH_HOME` 指向仓库内，请**格外小心**不要 `git add -f`。

> ℹ️ **与官方 CSwitch 隔离**：Claude Science 官方自带的 CSwitch 使用
> `~/.csswitch`，配置格式与本工具完全不同，**同样存有真实 API Key**。
> 本工具使用独立目录（默认 `D:\CSSwitch-Win-Data`），
> 并内置格式护栏：一旦检测到官方格式的配置，直接报错拒绝读写，绝不混写
> （见 `config.ForeignConfigError`）。

---

## 配置方式

### 配置文件 `D:\CSSwitch-Win-Data\config.json`

```jsonc
{
  "current": "ds-pro",
  "settings": {
    "proxy_port": 18991,      // 本地代理端口（回环）
    "sandbox_port": 8990,     // 沙箱 Science 端口（占用自动顺延）
    "science_bin": ""         // claude-science.exe 路径，空=自动发现
  },
  "providers": [
    {
      "name": "ds-pro",
      "kind": "deepseek",
      "mode": "anthropic",            // anthropic=原生透传 / openai=协议翻译
      "base_url": "https://api.deepseek.com/anthropic",
      "key": "sk-...",                // 仅本机，ACL 收紧，日志绝不出现
      "model": "deepseek-v4-pro",     // 默认模型（选择器当前项）
      "models": ["deepseek-v4-pro", "deepseek-v4-flash"],
      "model_map": { "claude-opus-4-8": "deepseek-v4-pro" },
      "model_caps": { "deepseek-v4-pro": 65536 },
      "auth_style": "x-api-key",      // x-api-key / bearer / both
      "thinking": "adaptive",         // adaptive / enabled / null
      "force_tool_disables_thinking": true
    }
  ]
}
```

同一家可存多套（不同 Key / 不同模型），`csx use` 随时切。

### 环境变量

| 变量 | 作用 |
|---|---|
| `CSSWITCH_HOME` | 改数据目录（测试隔离 / 便携模式） |
| `CSSWITCH_DEBUG` | `=1` 开启 debug 级日志 |
| `sandbox_enabled` | 安全沙箱开关（面板/配置）。`true`=Claude 看不到真实文件；`false`=可直接读写本机 |

| `CSSWITCH_ALLOW_TUNNEL` | 可选。设为 `host1,host2` 可收紧 CONNECT 隧道；**默认留空 = 全部放行** |

> ℹ️ **隧道为什么默认全放行**：曾尝试用白名单收紧（避免本机附带通用正向
> 代理），结果 Science 的分析环境装不上 conda 包 —— Environments 面板报
> "Your organization's network didn't allow the package download"，并连锁引发
> Agent 报"Your Claude session is no longer valid"。因为 Science 需要访问的
> 包索引/CDN/文献 API 太多，逐个补白名单不现实。代理本身只监听
> `127.0.0.1`，风险面等同于"本机装了个系统代理"。Anthropic 系域名的
> **401 fast-fail 仍然保留**（那是防卡死的必需功能，不是安全限制）。

### 注入给沙箱 Science 的变量（`csx start` 自动完成）

| 变量 | 值 | 作用 |
|---|---|---|
| `ANTHROPIC_BASE_URL` | `http://127.0.0.1:18991/<一次性secret>` | 推理劫持到本地代理 |
| `HTTPS_PROXY` / `https_proxy` | `http://127.0.0.1:18991` | Anthropic 域名 CONNECT 401 秒过 |
| `NO_PROXY` / `no_proxy` | `127.0.0.1,localhost,::1` | 推理直连本地，不进隧道 |
| `USERPROFILE` / `HOME` | 沙箱 home | 登录态与数据目录隔离 |
| `APPDATA` / `LOCALAPPDATA` / `TEMP` | 沙箱对应目录 | 缓存/临时文件隔离 |

> 刻意**不设** `http_proxy`：代理不实现普通 HTTP 转发，设了会让装包/MCP 的普通
> HTTP 请求撞 404。父进程已有的代理变量会先被剥离，避免继承意外配置。

### 自定义端点

```bat
:: 任意 Anthropic 兼容端点（透传、不重映射模型名）
便携启动.bat add --name relay1 --kind custom-anthropic --key sk-xxx ^
    --base-url https://your-relay.example.com/v1 --model claude-sonnet-4-5

:: 任意 OpenAI 兼容端点（协议翻译）
便携启动.bat add --name my-oai --kind custom-openai --key sk-xxx ^
    --base-url https://api.example.com/v1 --model gpt-4o-mini
```

---

## 安全与隔离设计

1. **真实凭证零触碰**：虚拟登录只写沙箱 auth_dir；`oauth_forge` 四道护栏
   （拒解析到真实目录 / 拒沙箱外 / 拒非假账号 email / 拒符号链接）
   + `O_EXCL` 临时文件原子写。
2. **Key 只驻本机**：`config.json` ACL 收紧到当前用户；经环境变量传子进程，
   **不进命令行参数、不进日志**；界面只显示掩码；日志有正则兜底脱敏。
3. **代理只收本机请求**：仅绑 `127.0.0.1`；推理端点带 path-secret；
   管理 API 带 admin token（`hmac.compare_digest` 常量时间比较）；
   secret/token 每次启动重新生成；入站请求体 64MB 上限。
4. **CONNECT 隧道默认封闭**：只放行本地回环，远端目标需显式白名单。
5. **入站凭证必剥**：`Authorization` / `x-api-key` 一律移除后才向上游发。
6. **进程精确杀伤**：停止沙箱只匹配本沙箱 data-dir 命令行；
   官方端口 8000 被硬编码拒绝作为沙箱端口。

Key 泄露处置：立即在服务商后台吊销该 Key、删除 `config.json` 中对应条目，
再用 `便携启动.bat doctor` 确认日志目录没有被同步到公开位置。

---

## 测试

```bat
runtime\python\python.exe tests\selftest.py   # 离线断言（零依赖，无需真实 API）
pytest tests/                # CI 入口
```

覆盖：

- **HKDF**：RFC 5869 官方向量 + 手写实现对拍 `cryptography`
- **v2 令牌**：帧结构、roundtrip、篡改/错密钥必败
- **虚拟登录**：文件布局、blob 字段、幂等 REUSED / 修复 REPAIRED / 四道护栏
- **模型解析与协议翻译**：壳 id 平铺、日期后缀、thinking 归一化、
  max_tokens 夹取、Anthropic↔OpenAI 双向（含 tool_use/tool_result）
- **代理整链（假上游）**：secret 鉴权、剥离注入、流式 SSE、CONNECT 401 fast-fail、
  管理 API 热切换、坏 token 拒绝
- **CLI**：add/list/use/remove/doctor 子进程全流程
- **面板两栏布局**：栏位归属、分界位置、窄屏回退、按钮不折行、表格横滚
- **面板编辑闭环**：`editProvider` 必须走 `api()`（裸 `fetch` 不带
  `x-csx-admin`，管理端点 403 → 表单静默失败）
- **仓库卫生**：无硬编码凭证、运行时数据未被跟踪

### 判断一个端点到底能不能用

**不要只看连通性。** 不少服务 `GET /v1/models` 返回 200 但列表为空，
`POST` 推理时才拒绝。实测踩过的例子：OpenCode free tier
（`oc/space-bunny-free`）`/v1/models` 返回 `{"object":"list","data":[]}`，
看起来正常，真实推理却是：

```
HTTP 403 insufficient_quota
FreeTierError: OpenCode's free tier can only be used from within OpenCode.
```

用现成的探测器（会发一次真实推理请求）：

```bash
python tools/check_edit_key.py --endpoint http://127.0.0.1:20128/v1 \
    --key sk-xxx --model oc/space-bunny-free
```

注意：`/v1/models` 返回 200 **不能证明可用**，必须以推理请求的 200 +
响应体里含 `"content"` 为准。

---

## 已知边界

- **第三方模式下 Anthropic 托管 MCP**（`*.mcp.claude.com`：pubmed 等）不可用
  ——需要真实 Anthropic 授权，Science 加载失败后自动跳过（日志
  `load failed (skipped)` 正常）；本地内置 bio-tools MCP 不受影响。
- **OpenAI 兼容端点的流式**为「非流式取全 + SSE 回放」（首 token 稍慢，
  tool_use 保真）；Anthropic 兼容端点为字节级流式透传。
- **Science 升级后**若登录令牌格式变化，需同步更新 `oauth_forge.py`。
- **CONNECT 隧道默认只放行本地回环**；若你的 Science 版本需要连其它主机，
  设 `CSSWITCH_ALLOW_TUNNEL=host1,host2`。

## 致谢

实现逻辑参考并移植自两个 MIT 许可的逆向研究项目：

| 参考项目 | 贡献的核心逻辑 |
|---|---|
| [SuperJJ007/CSSwitch](https://github.com/SuperJJ007/CSSwitch) | 产品形态、隔离沙箱铁律、Provider 契约、Rust Gateway 参考 |
| [bfzha/CSswitch-wsl_linux](https://github.com/bfzha/CSswitch-wsl_linux) | Python 代理、虚拟 OAuth 逆向结论（v2 格式）、整链验证记录 |

本项目为独立的纯 Python 重写，不含上述任一项目的源代码文件。

## 许可

[MIT](LICENSE) — 详见 LICENSE 文件末尾的致谢与署名声明。
