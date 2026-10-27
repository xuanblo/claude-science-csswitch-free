# -*- coding: utf-8 -*-
"""虚拟 OAuth 伪造器（Python 原生实现，替代参考项目的 Rust/Node 版）。
在【沙箱】auth_dir 里写一套本地自造、绝不联网的登录凭证，让 Claude Science
认为已登录（virtual@localhost.invalid），推理经 ANTHROPIC_BASE_URL 导去本地代理。
全程零 Anthropic 接触、零真实凭证。

令牌格式（与参考项目 oauth_forge.rs / make-virtual-oauth.mjs 字节级兼容）：
  - 令牌文件 <auth_dir>/.oauth-tokens/<sanitized account_uuid>.enc（目录内恰好一个 .enc）
  - 内容 v2 格式："v2:" + base64( IV(12) || AES-256-GCM(密文||tag(16)) )
    derivedKey = HKDF-SHA256(ikm=base64_decode(OAUTH_ENCRYPTION_KEY),
                             salt=b"", info=b"operon:aes-256-gcm:oauth", 32)
    AAD = b"v2:oauth"；明文 = JSON(tokenBlob)
  - encryption.key：换行分隔 KEY=base64(≥16B) 四把钥匙
  - active-org.json：{"org_uuid": <uuid>}（Science 只校验 org_uuid 是合法 UUID）

铁律护栏（写任何东西之前）：
  0. 解析后的写入根绝不落在真实 ~/.claude-science 之内或本身（最高优先）；
  1. 写入根必须落在沙箱根之下（防符号链接重定向）；
  2. email 必须以 localhost.invalid 结尾（确保是假账号）；
  3. 写前拒符号链接；O_EXCL 临时文件 + rename 原子替换。
"""
import base64
import json
import os
import re
import secrets
import sys
import uuid as uuidlib
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

KEY_NAMES = [
    "ANTHROPIC_API_KEY_ENCRYPTION_KEY",
    "OAUTH_ENCRYPTION_KEY",
    "JWT_SIGNING_SECRET",
    "USER_SECRET_ENCRYPTION_KEY",
]
HKDF_INFO = b"operon:aes-256-gcm:oauth"
AAD = b"v2:oauth"
VIRTUAL_EMAIL = "virtual@localhost.invalid"
TOKEN_EXPIRES_FAR = "2099-01-01T00:00:00.000Z"
SCOPES = "user:inference user:file_upload user:profile user:mcp_servers user:plugins"

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
"""合法 UUID 的唯一判据。

原先 config.py 与本模块各有一份等价定义、其中一份从未被引用；
现统一到此处（config.py 已删除自己的副本）。
"""


def looks_like_uuid(s: str) -> bool:
    return bool(_UUID_RE.match(s or ""))


# ---------- 随机与编码 ----------

def b64_32() -> str:
    """base64(32 随机字节)：encryption.key 各项的值。"""
    return base64.b64encode(os.urandom(32)).decode("ascii")


def uuid_v4() -> str:
    return str(uuidlib.uuid4())


# ---------- v2 GCM ----------

def derive_key(oauth_key_b64: str) -> bytes:
    """HKDF-SHA256 派生 32 字节密钥（salt 空，与 Node hkdfSync 对齐）。"""
    ikm = base64.b64decode(oauth_key_b64.strip())
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"",
                info=HKDF_INFO).derive(ikm)


def encrypt_token_v2(plaintext: bytes, oauth_key_b64: str) -> str:
    """加密：返回 "v2:" + base64(IV || 密文||tag)。Python AESGCM 输出即 密文||tag。"""
    key = derive_key(oauth_key_b64)
    iv = os.urandom(12)
    ct = AESGCM(key).encrypt(iv, plaintext, AAD)
    return "v2:" + base64.b64encode(iv + ct).decode("ascii")


def decrypt_token_v2(body: str, oauth_key_b64: str) -> bytes:
    """解密 "v2:..."，校验 tag；失败（含篡改/密钥不符）抛异常。"""
    if not body.startswith("v2:"):
        raise ValueError("缺 v2: 前缀")
    raw = base64.b64decode(body[3:])
    if len(raw) < 12 + 16:
        raise ValueError("v2 密文过短")
    iv, rest = raw[:12], raw[12:]
    key = derive_key(oauth_key_b64)
    return AESGCM(key).decrypt(iv, rest, AAD)


# ---------- 路径护栏与安全写 ----------

def _resolve(p: Path) -> str:
    """解析为绝对 realpath（看穿符号链接），两端用同一函数保证可比较。"""
    return os.path.realpath(str(p))


def is_symlink(p: Path) -> bool:
    """Windows 上 os.path.islink 对符号链接/junction 均判真（宁枉勿纵）。"""
    try:
        return os.path.islink(str(p))
    except (OSError, ValueError):
        return False


def _assert_not_symlink(p: Path):
    if is_symlink(p):
        raise ValueError(f"拒绝：{p} 是符号链接，绝不跟随写入。")


def safe_write(path: Path, data: bytes):
    """拒符号链接 + O_EXCL 临时文件 + rename 原子替换。"""
    path = Path(path)
    _assert_not_symlink(path)
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    suffix = uuidlib.uuid4().hex[:12]
    tmp = parent / f".tmp-{suffix}"
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(path))
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError as e:
                # 临时文件残留不影响正确性（os.replace 已生效），只提示不阻断
                print(f"[csswitch] 临时文件清理失败 {tmp}: {e}", file=sys.stderr)


def resolve_guarded(auth_dir, email, sandbox_root, real_dir) -> Path:
    """护栏（写任何东西之前）。全过则返回解析后的写入根。"""
    resolved = Path(_resolve(Path(auth_dir)))
    # 护栏 0（最高优先）：绝不落入真实 Science 目录之内或本身
    real_root = Path(_resolve(Path(real_dir)))
    if str(resolved).startswith(str(real_root) + os.sep) or str(resolved) == str(real_root):
        raise ValueError(
            f"拒绝：auth_dir 解析到真实 Science 目录（{real_root}）之内或本身，铁律禁止触碰。")
    # 护栏 1：必须落在沙箱根之下（防符号链接把写入重定向到沙箱外）
    root = Path(_resolve(Path(sandbox_root)))
    if not (str(resolved).startswith(str(root) + os.sep)):
        raise ValueError(
            f"拒绝：auth_dir 解析到沙箱根之外（{resolved} 不在 {root} 下），疑似符号链接重定向。")
    # 护栏 2：假账号
    if not email.endswith("localhost.invalid"):
        raise ValueError(f"拒绝：email 必须以 localhost.invalid 结尾（当前 {email}）。")
    return resolved


# ---------- encryption.key ----------

def parse_encryption_key(resolved: Path):
    """读 encryption.key -> dict；不存在返回 {}。"""
    kf = resolved / "encryption.key"
    if not kf.is_file():
        return {}
    out = {}
    for line in kf.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k and v:
                out[k] = v
    return out


def _oauth_key_usable(v: str) -> bool:
    try:
        return len(base64.b64decode(v.strip())) >= 16
    except Exception:  # noqa: BLE001 - 非法 base64 一律判不可用
        return False


def write_encryption_key(resolved: Path):
    """复用已存在的合法 OAUTH_ENCRYPTION_KEY（保持旧 .enc 可解），缺的补造。"""
    keys = parse_encryption_key(resolved)
    if not _oauth_key_usable(keys.get("OAUTH_ENCRYPTION_KEY", "")):
        keys.pop("OAUTH_ENCRYPTION_KEY", None)
    for k in KEY_NAMES:
        if k not in keys:
            keys[k] = b64_32()
    blob = "\n".join(f"{k}={keys[k]}" for k in KEY_NAMES) + "\n"
    safe_write(resolved / "encryption.key", blob.encode("utf-8"))
    return keys


# ---------- 令牌 blob 与落盘 ----------

def build_token_blob(email: str, account_uuid: str, org_uuid: str) -> dict:
    access = "sk-ant-virtual-" + secrets.token_hex(24)
    return {
        "access_token": access,          # 代理会剥离，值任意
        "refresh_token": "",
        "api_key": None,
        "token_expires_at": TOKEN_EXPIRES_FAR,  # 远期 → 绝不触发联网刷新
        "provider": "claude_ai",
        "scopes": SCOPES,
        "email": email,
        "account_uuid": account_uuid,
        "subscription_type": "max",
        "rate_limit_tier": None,
        "seat_tier": None,
        "org_uuid": org_uuid,
        "billing_type": None,
        "has_extra_usage_enabled": False,
    }


def write_login(resolved: Path, email: str, prefer_org=None, prefer_account=None) -> dict:
    resolved = Path(resolved)
    resolved.mkdir(parents=True, exist_ok=True)

    keys = write_encryption_key(resolved)
    oauth_key = keys["OAUTH_ENCRYPTION_KEY"]

    account_uuid = prefer_account or uuid_v4()
    org_uuid = prefer_org or uuid_v4()

    blob = build_token_blob(email, account_uuid, org_uuid)
    enc_body = encrypt_token_v2(json.dumps(blob).encode("utf-8"), oauth_key)

    # 先清其它 .enc，保证目录内恰好一个（Science 硬性要求）
    tok_dir = resolved / ".oauth-tokens"
    _assert_not_symlink(tok_dir)
    tok_dir.mkdir(parents=True, exist_ok=True)
    for p in tok_dir.glob("*.enc"):
        _assert_not_symlink(p)
        p.unlink()

    sanitized = re.sub(r"[^0-9A-Za-z_\-]", "", account_uuid)
    enc_file = tok_dir / f"{sanitized}.enc"
    safe_write(enc_file, enc_body.encode("utf-8"))

    # 自校验：解密回读，确保 Science 能解开
    rt = json.loads(decrypt_token_v2(enc_body, oauth_key))
    if rt.get("email") != email:
        raise ValueError("自校验失败：解密回读的 email 不符")

    org_json = json.dumps({"org_uuid": org_uuid}, indent=2) + "\n"
    safe_write(resolved / "active-org.json", org_json.encode("utf-8"))

    return {"auth_dir": str(resolved), "account_uuid": account_uuid,
            "org_uuid": org_uuid, "enc_file": str(enc_file)}


# ---------- 幂等：现有登录读取/校验 ----------

def single_enc(resolved: Path):
    """目录下恰好一个 .enc 才返回其路径；零个或多个返回 None。"""
    found = None
    d = resolved / ".oauth-tokens"
    if not d.is_dir():
        return None
    for p in d.glob("*.enc"):
        if found is not None:
            return None
        found = p
    return found


def read_active_org(resolved: Path):
    try:
        v = json.loads((resolved / "active-org.json").read_text(encoding="utf-8"))
        o = v.get("org_uuid")
        return o if isinstance(o, str) and looks_like_uuid(o) else None
    except Exception:
        return None


def _read_blob_field(resolved: Path, field: str):
    """从唯一 .enc 解出 token blob 的某字段（失败无害，返回 None）。"""
    key = parse_encryption_key(resolved).get("OAUTH_ENCRYPTION_KEY")
    if not key:
        return None
    enc = single_enc(resolved)
    if enc is None:
        return None
    try:
        blob = json.loads(decrypt_token_v2(enc.read_text(encoding="utf-8"), key))
    except Exception:
        return None
    v = blob.get(field)
    return v if isinstance(v, str) else None


def read_prior_account(resolved: Path):
    v = _read_blob_field(resolved, "account_uuid")
    return v if v and looks_like_uuid(v) else None


def read_token_org(resolved: Path):
    v = _read_blob_field(resolved, "org_uuid")
    return v if v and looks_like_uuid(v) else None


def scan_org_dirs(resolved: Path):
    out = []
    d = resolved / "orgs"
    if d.is_dir():
        for p in d.iterdir():
            if p.is_dir() and looks_like_uuid(p.name):
                out.append(p.name)
    return out


class LoginAction:
    REUSED = "reused"      # 现有登录完整自洽，未写任何文件
    REPAIRED = "repaired"  # 部分损坏，重写但沿用原 org/account
    CREATED = "created"    # 首次，铸全新 org/account


def ensure_virtual_login(auth_dir, email=VIRTUAL_EMAIL, sandbox_root=None,
                         real_dir=None) -> tuple:
    """幂等确保沙箱虚拟登录存在。返回 (result_dict, LoginAction)。"""
    auth_dir = Path(auth_dir)
    sandbox_root = Path(sandbox_root) if sandbox_root else auth_dir.parent
    real_dir = Path(real_dir) if real_dir else Path.home() / ".claude-science"

    resolved = resolve_guarded(auth_dir, email, sandbox_root, real_dir)

    keys = parse_encryption_key(resolved)
    enc = single_enc(resolved)
    org = read_active_org(resolved)
    account = read_prior_account(resolved)
    token_org = read_token_org(resolved)

    have_everything = (
        bool(keys.get("OAUTH_ENCRYPTION_KEY"))
        and all(k in keys for k in KEY_NAMES)
        and enc is not None and account is not None
        and org is not None
    )
    if have_everything:
        return ({"auth_dir": str(resolved), "account_uuid": account,
                 "org_uuid": org, "enc_file": str(enc)}, LoginAction.REUSED)

    # 修复：沿用可恢复的 org/account，保住旧对话挂得上
    prefer_org = org or token_org or (scan_org_dirs(resolved) or [None])[0]
    prefer_account = account
    action = LoginAction.REPAIRED if (prefer_org or prefer_account) else LoginAction.CREATED
    result = write_login(resolved, email, prefer_org=prefer_org,
                         prefer_account=prefer_account)
    result["action"] = action
    return (result, action)
