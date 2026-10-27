#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Claude Science 对话文章导出器

把 Claude Science 每个对话（root frame）的成品文章 + 全部产物，
按「一对话一文件夹」导出到指定目录。

输出结构：
    <out_dir>/<日期>_<对话标题>/
        <对话标题>.md | .html | .docx    文章正文（自动挑选该对话的成稿）
        attachments/                     该对话其余全部产物（图表/CSV/代码/数据...）
    <out_dir>/INDEX.md                   全部导出对话的索引

用法：
    python cs_export.py            增量导出（重复运行不会重复拷贝）
    python cs_export.py --dry-run  只打印导出计划，不写文件
    python cs_export.py --force    忽略清单，全部重导
    python cs_export.py --list     列出对话与选中的文章
    python cs_export.py --out DIR  临时指定输出目录
    python cs_export.py --data-dir DIR  临时指定数据目录（含 operon-cli.db 或其上层）
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

MANIFEST_NAME = ".cs_export_manifest.json"
INDEX_NAME = "INDEX.md"

# 文章候选优先级：越靠前越像"成稿"
TIER_RULES = (
    (4, ("draft", "manuscript", "paper", "article", "thesis", "review", "论文", "文章", "综述")),
    (3, ("report", "报告")),
    (2, ("summary", "摘要")),
    (1, ("readme", "cover_letter", "protocol", "plan", "roadmap", "design", "journal", "playbook")),
)
PRIMARY_TYPES = ("text/markdown", "text/html")
FALLBACK_TYPES = ("text/x-latex", "application/x-tex", "text/x-tex",
                  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                  "application/msword", "application/pdf")
SKIP_PREFIXES = ("plan_",)
BAD_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t\x00-\x1f]')


# ---------------------------------------------------------------- 基础工具
def log(msg: str = "") -> None:
    print(msg, flush=True)


def safe_name(name: str, max_len: int = 80) -> str:
    s = BAD_CHARS.sub(" ", name or "").strip()
    s = re.sub(r"\s+", " ", s).strip(" .")
    if s.upper().split(".")[0] in {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        s = f"_{s}"
    if len(s) > max_len:
        s = s[:max_len].rstrip(" .")
    return s or "untitled"


def _clean_title_text(raw: str | None) -> str:
    s = (raw or "").strip()
    if not s or s.startswith(("{", "[")) or len(s) > 120:
        return ""
    return s


def title_of(conv: dict, fallback_stem: str = "") -> str:
    """对话标题：跳过 JSON 垃圾名（拖附件时会出现），退回任务摘要 / 文章名 / id。"""
    name = _clean_title_text(conv.get("name"))
    if not name:
        name = _clean_title_text((conv.get("task_summary") or "").splitlines()[0]
                                if conv.get("task_summary") else "")
    if not name:
        name = _clean_title_text(fallback_stem)
    return name or f"对话_{conv['id'][:8]}"


def fmt_date(ms: int | None) -> str:
    if not ms:
        return "undated"
    try:
        sec = ms / 1000 if ms > 1e11 else ms
        return _dt.datetime.fromtimestamp(sec).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return "undated"


def load_config(path: Path) -> dict:
    cfg = {
        "data_dir": "",
        "out_dir": "D:/Paper/articles",
        "skip_projects": ["proj_example"],
        "attachments_subdir": "attachments",
        "include_all_artifacts": True,
        "rewrite_media_links": True,
        "write_index": True,
        "max_title_len": 80,
    }
    if path.exists():
        try:
            cfg.update(json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001
            log(f"[warn] 配置文件解析失败，使用默认值：{exc}")
    return cfg


def resolve_db(data_dir_cfg: str) -> tuple[Path, Path] | None:
    """返回 (db 路径, artifacts 根目录)。兼容两种布局：
        <data>/operon-cli.db  +  <data>/artifacts
        <data>/orgs/<org>/operon-cli.db  +  <data>/orgs/<org>/artifacts
    """
    candidates = [
        data_dir_cfg,
        os.environ.get("CLAUDE_SCIENCE_DATA", ""),
        r"\\wsl$\Ubuntu-22.04\home\zhangxuan\.csswitch\sandbox\home\.claude-science",
        r"D:\CSSwitch-Win-Data\sandbox\data",
        str(Path.home() / ".claude-science"),
    ]
    for cand in candidates:
        if not cand:
            continue
        base = Path(cand)
        if not base.exists():
            continue
        # orgs 布局优先：根目录里的 operon-cli.db 往往是初始/示例库，
        # 真正在用的对话写在 orgs/<org>/ 下（daemon 当前活动 org）。
        orgs = base / "orgs"
        if orgs.is_dir():
            org_dirs = [d for d in orgs.iterdir() if (d / "operon-cli.db").is_file()]
            if org_dirs:
                active = base / "active-org.json"
                chosen = None
                if active.is_file():
                    try:
                        oid = json.loads(active.read_text(encoding="utf-8")).get("orgId") or \
                              json.loads(active.read_text(encoding="utf-8")).get("id")
                        chosen = next((d for d in org_dirs if d.name == oid), None)
                    except Exception:  # noqa: BLE001
                        chosen = None
                chosen = chosen or max(org_dirs, key=lambda d: (d / "operon-cli.db").stat().st_mtime)
                return chosen / "operon-cli.db", chosen / "artifacts"
        direct = base / "operon-cli.db"
        if direct.is_file():
            return direct, base / "artifacts"
    return None


# ---------------------------------------------------------------- 数据读取
def snapshot_db(db_path: Path) -> tuple[sqlite3.Connection, tempfile.TemporaryDirectory]:
    """复制 db(+wal/shm) 到本地临时目录再读，避免和运行中的 daemon 抢锁。"""
    tmp = tempfile.TemporaryDirectory(prefix="cs_export_")
    dst = Path(tmp.name) / "db.sqlite"
    shutil.copy2(db_path, dst)
    for suffix in ("-wal", "-shm"):
        side = db_path.parent / f"{db_path.name}{suffix}"
        if side.exists():
            shutil.copy2(side, Path(tmp.name) / f"db.sqlite{suffix}")
    conn = sqlite3.connect(str(dst))
    conn.row_factory = sqlite3.Row
    return conn, tmp


def fetch_conversations(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT f.id, f.name, f.created_at, f.status, f.task_summary,
               f.project_id, COALESCE(p.name, f.project_id, '') AS project_name
        FROM frames f LEFT JOIN projects p ON p.id = f.project_id
        WHERE f.id = f.root_frame_id AND f.conversation_type = 'agent'
        ORDER BY f.created_at ASC
        """
    ).fetchall()
    convs = []
    for r in rows:
        arts = conn.execute(
            """
            SELECT a.id, a.filename, a.created_at, a.is_user_upload,
                   av.id AS version_id, av.content_type, av.size_bytes, av.storage_path
            FROM artifacts a
            LEFT JOIN artifact_versions av ON av.id = a.latest_version_id
            WHERE a.root_frame_id = ?
            ORDER BY a.created_at ASC
            """,
            (r["id"],),
        ).fetchall()
        convs.append(
            {
                "id": r["id"], "name": r["name"], "created_at": r["created_at"],
                "status": r["status"], "task_summary": r["task_summary"],
                "project_id": r["project_id"] or "", "project_name": r["project_name"] or "",
                "artifacts": [dict(a) for a in arts],
            }
        )
    return convs


def frame_fallback_text(conn: sqlite3.Connection, frame_id: str) -> str:
    """对话没有成稿产物时，用对话最终回复正文兜底。"""
    row = conn.execute("SELECT output_data FROM frames WHERE id = ?", (frame_id,)).fetchone()
    if not row or not row["output_data"]:
        return ""
    raw = row["output_data"]
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw.strip()
    if isinstance(obj, dict):
        for key in ("response", "text", "content", "message"):
            val = obj.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


# ---------------------------------------------------------------- 文章挑选
def _tier(filename: str) -> int:
    n = (filename or "").lower()
    for level, keys in TIER_RULES:
        if any(k in n for k in keys):
            return level
    return 0


def pick_article(artifacts: list[dict]) -> tuple[dict | None, list[dict]]:
    usable = [
        a for a in artifacts
        if a.get("storage_path")
        and not a.get("is_user_upload")
        and not (a.get("filename") or "").lower().startswith(SKIP_PREFIXES)
    ]
    pool = [a for a in usable if a.get("content_type") in PRIMARY_TYPES]
    if not pool:
        pool = [a for a in usable if a.get("content_type") in FALLBACK_TYPES]
    if not pool:
        return None, artifacts

    def score(a: dict) -> tuple:
        return (_tier(a["filename"]), a.get("size_bytes") or 0, a.get("created_at") or 0)

    best = max(pool, key=score)
    rest = [a for a in artifacts if a.get("id") != best.get("id")]
    return best, rest


# ---------------------------------------------------------------- 引用修复
MD_LINK = re.compile(r"(!?\[[^\]]*\]\()(\s*<?)([^)>\s]+)(>?\s*(?:\"[^\"]*\")?\))")
HTML_ATTR = re.compile(r'(src|href)=(["\'])([^"\']+)\2', re.IGNORECASE)
ARTIFACT_REF = re.compile(r"\{\{artifact:(?:art_)?([0-9a-fA-F-]{8,})\}\}")


def resolve_artifact_refs(text: str, mapping: dict[str, str]) -> str:
    """把 {{artifact:<版本ID>}} 占位符替换为导出后的相对路径；指向已丢失产物的降级成文字标记。"""
    if not mapping and "{{artifact:" not in text:
        return text

    def repl(m: re.Match) -> str:
        hit = mapping.get(m.group(1).lower())
        if hit:
            return hit
        short = m.group(1).lower().replace("art_", "")[:8]
        return f"〔图表缺失: {short}〕"

    return ARTIFACT_REF.sub(repl, text)


def _remap(target: str, avail: set[str], sub: str) -> str | None:
    t = target.strip()
    if not t or t.startswith(("#", "/", "http://", "https://", "mailto:", "data:")):
        return None
    base = Path(t.replace("\\", "/")).name
    return f"{sub}/{base}" if base in avail else None


def rewrite_links(text: str, avail: set[str], sub: str) -> str:
    def md_sub(m: re.Match) -> str:
        new = _remap(m.group(3), avail, sub)
        return f"{m.group(1)}{m.group(2)}{new}{m.group(4)}" if new else m.group(0)

    def html_sub(m: re.Match) -> str:
        new = _remap(m.group(3), avail, sub)
        return f"{m.group(1)}={m.group(2)}{new}{m.group(2)}" if new else m.group(0)

    return HTML_ATTR.sub(html_sub, MD_LINK.sub(md_sub, text))


# ---------------------------------------------------------------- 计划构建
def build_plan(art_root: Path, out_dir: Path, cfg: dict, conn: sqlite3.Connection) -> list[dict]:
    convs = fetch_conversations(conn)
    sub = cfg.get("attachments_subdir") or "attachments"
    max_title = int(cfg.get("max_title_len") or 80)
    skips = tuple(cfg.get("skip_projects") or ())

    plans: list[dict] = []
    used_dirs: dict[str, str] = {}

    for c in convs:
        if skips and c["project_id"].startswith(skips):
            continue

        # 纯附件上传 / 空聊不产文章
        generated = [a for a in c["artifacts"] if not a.get("is_user_upload")]
        if not generated:
            txt = frame_fallback_text(conn, c["id"]) if not c["artifacts"] else ""
            if len(txt) < 200:
                continue

        primary, rest = pick_article(c["artifacts"])

        title = title_of(c, Path(primary["filename"]).stem if primary else "")
        date = fmt_date(c["created_at"])
        dirname = f"{date}_{safe_name(title, max_title)}"
        if dirname in used_dirs and used_dirs[dirname] != c["id"]:
            dirname = f"{dirname}_{c['id'][:6]}"
        used_dirs[dirname] = c["id"]

        files: list[dict] = []
        article_rel = ""

        if primary:
            src = art_root / primary["storage_path"]
            ext = Path(primary["filename"]).suffix or ".md"
            article_rel = f"{safe_name(title, max_title)}{ext}"
            if src.is_file():
                st = src.stat()
                files.append({"src": src, "rel": article_rel, "size": st.st_size, "mtime": st.st_mtime,
                              "rewrite": bool(cfg.get("rewrite_media_links")), "ctype": primary["content_type"]})
        else:
            text = frame_fallback_text(conn, c["id"])
            if text:
                article_rel = f"{safe_name(title, max_title)}.md"
                files.append({"text": text, "rel": article_rel, "rewrite": False, "ctype": "text/markdown"})

        amap: dict[str, str] = {}
        by_name: dict[str, str] = {}
        if cfg.get("include_all_artifacts", True):
            for a in rest:
                sp = a.get("storage_path")
                if not sp:
                    continue
                src = art_root / sp
                if not src.is_file():
                    continue
                name = safe_name(Path(a["filename"]).name or src.name, 120)
                rel = f"{sub}/{name}"
                amap[str(a["id"]).lower()] = rel
                by_name[name] = rel
                st = src.stat()
                files.append({"src": src, "rel": rel, "size": st.st_size, "mtime": st.st_mtime,
                              "rewrite": False, "ctype": a.get("content_type")})

        # {{artifact:<id>}} 里通常是版本 ID，按文件名把每个版本都映射到导出路径
        if by_name:
            version_rows = conn.execute(
                """
                SELECT av.id AS vid, a.filename AS fn
                FROM artifact_versions av JOIN artifacts a ON a.id = av.artifact_id
                WHERE a.root_frame_id = ?
                """,
                (c["id"],),
            ).fetchall()
            for v in version_rows:
                rel = by_name.get(safe_name(Path(v["fn"]).name, 120))
                if rel:
                    amap.setdefault(str(v["vid"]).lower(), rel)

        if not files:
            continue

        plans.append({"id": c["id"], "title": title, "date": date, "dirname": dirname,
                      "status": c["status"], "project": c["project_id"],
                      "article": article_rel,
                      "article_source": (primary or {}).get("filename", ""),
                      "artifact_map": amap, "files": files})
    return plans


# ---------------------------------------------------------------- 同步
def file_signature(f: dict) -> list:
    """源侧签名：源内容变了才重拷（重写链接后的成品大小与源不同，不能比目标大小）。"""
    if "src" in f:
        return [int(f["size"]), int(f["mtime"])]
    return ["text", hashlib.md5(f["text"].encode("utf-8")).hexdigest()]


def sync_plan(plan: dict, out_dir: Path, prev: dict, force: bool, dry: bool,
              sub: str = "attachments") -> tuple[int, int, list[str], dict]:
    pdir = out_dir / plan["dirname"]
    all_names = {Path(f["rel"]).name for f in plan["files"]}
    prev_files = set(prev.get("files") or [])
    prev_sigs = prev.get("sources") or {}
    copied = skipped = 0
    new_files: list[str] = []
    sigs: dict[str, list] = {}

    for f in plan["files"]:
        dest = pdir / f["rel"]
        new_files.append(f["rel"])
        sig = file_signature(f)
        sigs[f["rel"]] = sig
        if not force and dest.is_file() and prev_sigs.get(f["rel"]) == sig:
            skipped += 1
            continue
        if dry:
            copied += 1
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if "src" in f:
            if f.get("rewrite") and f["rel"].endswith((".md", ".markdown", ".html", ".htm")):
                text = f["src"].read_text(encoding="utf-8", errors="replace")
                text = resolve_artifact_refs(text, plan.get("artifact_map") or {})
                dest.write_text(rewrite_links(text, all_names, sub), encoding="utf-8")
            else:
                shutil.copy2(f["src"], dest)
        else:
            dest.write_text(f["text"], encoding="utf-8")
        copied += 1

    # 清理本轮不再存在的旧文件（仅限本工具写过的路径）
    stale = [p for p in prev_files if p not in set(new_files)]
    if stale and not dry:
        for rel in stale:
            try:
                (pdir / rel).unlink()
            except (OSError, FileNotFoundError):
                pass
        sub_path = pdir / sub
        try:
            if sub_path.is_dir() and not any(sub_path.iterdir()):
                sub_path.rmdir()
        except OSError:
            pass
    return copied, skipped, new_files, sigs


def write_index(out_dir: Path, plans: list[dict]) -> None:
    lines = ["# Claude Science 文章索引", "",
             f"_导出时间：{_dt.datetime.now():%Y-%m-%d %H:%M} · 共 {len(plans)} 个对话_", "",
             "| 日期 | 对话 | 文章 | 产物数 |", "| --- | --- | --- | --- |"]
    for p in sorted(plans, key=lambda x: (x["date"], x["title"]), reverse=True):
        if p["article"]:
            target = "./" + "/".join(quote(seg) for seg in (p["dirname"], p["article"]))
            link = f"[{p['article']}]({target})"
        else:
            link = "—"
        n = max(len(p["files"]) - (1 if p["article"] else 0), 0)
        lines.append(f"| {p['date']} | {p['title']} | {link} | {n} |")
    (out_dir / INDEX_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- 入口
def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(description="导出 Claude Science 每个对话的文章与产物")
    ap.add_argument("--config", default=str(Path(__file__).with_name("cs_export_config.json")))
    ap.add_argument("--out", help="临时覆盖输出目录")
    ap.add_argument("--data-dir", help="临时覆盖 Claude Science 数据目录")
    ap.add_argument("--force", action="store_true", help="忽略增量清单，全部重导")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不写文件")
    ap.add_argument("--list", action="store_true", help="列出对话与选中的文章")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    if args.out:
        cfg["out_dir"] = args.out

    found = resolve_db(args.data_dir or cfg.get("data_dir") or "")
    if not found:
        log("[error] 找不到 Claude Science 数据（operon-cli.db）。")
        log("        请先在 cs_export_config.json 的 data_dir 里填数据目录。")
        return 2
    db_path, art_root = found

    out_dir = Path(cfg["out_dir"]).expanduser()
    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    log(f"数据源：{db_path.parent}")
    log(f"输出：  {out_dir}")

    conn, tmp = snapshot_db(db_path)
    try:
        plans = build_plan(art_root, out_dir, cfg, conn)
    finally:
        conn.close()
        tmp.cleanup()

    if args.list:
        log("")
        for p in plans:
            log(f"- [{p['status']}] {p['title']}（{p['project']}）")
            log(f"    文章：{p['article'] or '（无成稿，用对话正文兜底）'}  ← {p['article_source'] or 'output_data'}")
            log(f"    产物：{len(p['files'])} 个")
        if not plans:
            log("（没有找到任何对话）")
        return 0

    manifest_path = out_dir / MANIFEST_NAME
    manifest: dict = {"version": 2, "conversations": {}}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.setdefault("conversations", {})
        except Exception:  # noqa: BLE001
            manifest = {"version": 2, "conversations": {}}

    total_copied = total_skipped = 0
    sub = cfg.get("attachments_subdir") or "attachments"
    errors: list[tuple[str, str]] = []
    for p in plans:
        prev = manifest["conversations"].get(p["id"], {})
        try:
            copied, skipped, files, sigs = sync_plan(p, out_dir, prev, args.force, args.dry_run, sub)
        except OSError as exc:
            errors.append((p["dirname"], str(exc)))
            log(f"  [跳过] {p['dirname']}：{exc}")
            continue
        total_copied += copied
        total_skipped += skipped
        manifest["conversations"][p["id"]] = {
            "dir": p["dirname"], "article": p["article"], "files": files, "sources": sigs,
            "title": p["title"], "date": p["date"],
        }
        flag = "写入" if copied else "已是最新"
        log(f"  {p['dirname']}  →  {p['article'] or '无文章'}  [{flag} {copied}/{len(p['files'])}]")

    if not args.dry_run:
        manifest["last_run"] = _dt.datetime.now().isoformat(timespec="seconds")
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        if cfg.get("write_index", True):
            write_index(out_dir, [p for p in plans if p["id"] in manifest["conversations"]])

    log("")
    log(f"{'（演练）' if args.dry_run else ''}完成：{len(plans) - len(errors)} 个对话，"
        f"写入 {total_copied} 个文件，跳过 {total_skipped} 个未变化文件")
    if not plans:
        log(f"[hint] 数据源 {db_path.parent} 里没有可导出的对话。")
        log("       若对话在旧版(WSL)数据里，用 --data-dir 指定其目录。")
    if total_copied == 0 and total_skipped > 0 and not errors:
        log("提示：全部已是最新。需要强制重导请加 --force。")
    if errors:
        log(f"[warn] {len(errors)} 个对话导出失败（权限/占用），其余已正常完成。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
