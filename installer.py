#!/usr/bin/env python3
"""
RDR2 Mod Installer - extracts mod archives into the game folder safely.

Why this exists
---------------
Nexus gates every file download behind a logged-in browser session: the API key
can read mod metadata and file listings (v1 /mods/<id>/files.json) but
/mods/<id>/files/<fid>/download_link.json returns 404, and the v2 GraphQL
ModFile type has no download URL field at all. So the bytes have to arrive on
disk first (downloaded by hand, or by a logged-in browser), and this script
does the extraction part.

Safety model
------------
Every install is reversible:
  1. Files that would be overwritten are copied into a timestamped backup dir.
  2. A manifest records added / replaced / backed-up paths.
  3. `undo` restores the backups and deletes what this install added.

Nothing is ever deleted from the game folder except files this installer itself
added in that same install, and only when undoing it.

Run:  python installer.py install <archive> --game "E:\\rdr2\\Red Dead Redemption 2"
      python installer.py undo <install-id>
      python installer.py list
      python installer.py dry-run <archive>
"""

import argparse
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

try:
    import rarfile  # optional, only for .rar archives
except ImportError:
    rarfile = None

# External extractors for .rar, tried in order. Nexus ships many mods as RAR and
# Python cannot read that format on its own.
RAR_TOOLS = [
    ["7z", "x", "-y", "-o{out}", "{archive}"],
    ["7za", "x", "-y", "-o{out}", "{archive}"],
    ["7zz", "x", "-y", "-o{out}", "{archive}"],
    ["unrar", "x", "-y", "{archive}", "{out}"],
    ["UnRAR", "x", "-y", "{archive}", "{out}"],   # console tool, unlike WinRAR.exe
    ["rar", "x", "-y", "-ep", "{archive}", "{out}\\"],
]
# Note: no tar fallback on purpose. Git-for-Windows ships GNU tar, which cannot
# read RAR at all and mangles Windows paths. bsdtar could, but it is not reliably
# on PATH, so we only advertise 7-Zip / unrar.

RAR_HELP = """\
Cannot read .rar archives yet. Pick one:

  pip install rarfile      # then you also need unrar on PATH
  install 7-Zip            # simplest; 7z.exe then gets picked up automatically

Or just grab the .zip version of the mod from Nexus instead."""

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(ROOT, "installs")
DEFAULT_GAME = r"E:\rdr2\Red Dead Redemption 2"

# Files that live in every mod zip and should not be copied into the game dir.
JUNK_NAMES = {"readme.txt", "readme.md", "mods.txt", "credits.txt", "changelog.txt",
              "license.txt", "installation.txt", "install.txt", ".gitkeep", "thumbs.db"}

# Paths inside an archive that are never safe to write outside the game dir.
UNSAFE_PREFIXES = ("..", "/", "\\")


def _now():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def _safe_relpath(name):
    """Return a cleaned relative path, or None if the entry is unsafe."""
    name = name.replace("\\", "/").strip()
    if not name or name.endswith("/"):
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    if os.path.isabs(name) or ":" in parts[0]:
        return None
    return os.path.join(*parts)


def _single_root(entries):
    """
    Many mod zips wrap everything in one top-level folder ("MyMod/...") and RDR2
    mods need those files flat in the game dir. Return that wrapper folder if
    one clearly dominates the archive, otherwise None.

    A stray root-level file (a stray readme, a licence txt) must not defeat the
    detection, so we pick the most common first path component rather than
    demanding every entry share it.
    """
    counts = {}
    for e in entries:
        parts = [p for p in e.replace("\\", "/").split("/") if p]
        if len(parts) > 1 and "." not in parts[0]:
            counts[parts[0]] = counts.get(parts[0], 0) + 1
    if not counts:
        return None
    root, hits = max(counts.items(), key=lambda kv: kv[1])
    # Must be the majority, otherwise the archive really is flat at the top.
    if hits * 2 < len(entries):
        return None
    return root


def _rel_under_root(name, root):
    """Drop the archive's single wrapper folder, then make the path safe."""
    parts = [p for p in name.replace("\\", "/").split("/") if p]
    if root and len(parts) > 1 and parts[0] == root:
        parts = parts[1:]
    return _safe_relpath("/".join(parts))


def _is_junk(rel):
    base = os.path.basename(rel).lower()
    if base in JUNK_NAMES:
        return True
    return base.startswith(".") and base not in (".gitkeep",)


def _rar_reader(archive):
    """Return a callable (member_name) -> extracted temp path, or None."""
    if rarfile is not None:
        def with_rarfile(member):
            with rarfile.RarFile(archive) as rf:
                target = os.path.join(_RAR_TMP[0], os.path.basename(member))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with rf.open(member) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                return target
        return with_rarfile

    tool = _find_rar_tool()
    if not tool:
        raise SystemExit(RAR_HELP)
    print(f"[..] {os.path.basename(archive)}: unpacking with {os.path.basename(tool[0])} ...")

    tmp = tempfile.mkdtemp(prefix="rdr2rar_")
    _RAR_TMP[0] = tmp
    cmd = [part.format(archive=archive, out=tmp) for part in tool]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not os.path.isdir(tmp) or not os.listdir(tmp):
        detail = (proc.stderr or proc.stdout or "").strip()[:400]
        raise SystemExit(
            f"{os.path.basename(tool[0])} could not unpack {os.path.basename(archive)}"
            + (f":\n{detail}" if detail else " (it extracted nothing - the archive may "
               "be password protected or corrupt)"))

    def via_tool(member):
        return os.path.join(tmp, member.replace("\\", "/"))
    return via_tool


def _find_rar_tool():
    for tool in RAR_TOOLS:
        exe = shutil.which(tool[0])
        if exe:
            return [exe if i == 0 else p for i, p in enumerate(tool)]
    # Common install locations the tools are not on PATH for. Note we look for
    # the console UnRAR/Rar exes, not WinRAR.exe, which is a GUI app that
    # returns success without extracting anything.
    known = [r"C:\Program Files\7-Zip\7z.exe", r"C:\Program Files (x86)\7-Zip\7z.exe",
             r"C:\Program Files\WinRAR\UnRAR.exe", r"C:\Program Files\WinRAR\Rar.exe",
             r"C:\Program Files (x86)\WinRAR\UnRAR.exe", r"C:\Program Files (x86)\WinRAR\Rar.exe"]
    def _stem(path):
        return os.path.splitext(os.path.basename(path))[0].lower()

    for cand in known:
        if not os.path.isfile(cand):
            continue
        for tmpl in RAR_TOOLS:
            if _stem(cand) == _stem(tmpl[0]):
                return [cand if i == 0 else p for i, p in enumerate(tmpl)]
    return None


_RAR_TMP = [None]


def read_entries(archive):
    """Yield (abs_member_name, relpath) for every file in the archive."""
    lower = archive.lower()
    if lower.endswith(".rar"):
        reader = _rar_reader(archive)
        tmp = _RAR_TMP[0]
        raw = []
        for root, _dirs, files in os.walk(tmp):
            for f in files:
                full = os.path.join(root, f)
                raw.append((os.path.relpath(full, tmp).replace("\\", "/"), full))
        root_name = _single_root([n for n, _ in raw])
        for name, full in raw:
            rel = _rel_under_root(name, root_name)
            if rel and not _is_junk(rel):
                yield full, rel
        return

    with zipfile.ZipFile(archive) as zf:
        raw = [i.filename for i in zf.infolist() if not i.is_dir()]
        root = _single_root(raw)
        for name in raw:
            rel = _rel_under_root(name, root)
            if rel and not _is_junk(rel):
                yield name, rel


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def install(archive, game, dry_run=False, force=False):
    if not os.path.isdir(game):
        raise SystemExit(f"Game folder not found: {game}")

    entries = list(read_entries(archive))
    if not entries:
        shutil.rmtree(_RAR_TMP[0], ignore_errors=True) if _RAR_TMP[0] else None
        _RAR_TMP[0] = None
        raise SystemExit("Nothing installable in that archive (only readme/junk files).")

    install_id = _now()
    added, replaced = [], []

    for _member, rel in entries:
        dest = os.path.join(game, rel)
        if os.path.exists(dest):
            replaced.append(rel)
        else:
            added.append(rel)

    if dry_run:
        print(f"archive : {archive}")
        print(f"game    : {game}")
        print(f"add     : {len(added)}")
        for r in added[:40]:
            print(f"    + {r}")
        if len(added) > 40:
            print(f"    ... and {len(added) - 40} more")
        print(f"replace : {len(replaced)}")
        for r in replaced[:40]:
            print(f"    ~ {r}")
        if len(replaced) > 40:
            print(f"    ... and {len(replaced) - 40} more")
        _cleanup_rar_tmp()
        return {"dry_run": True, "added": added, "replaced": replaced}

    backup_root = os.path.join(STATE_DIR, install_id, "backup")
    os.makedirs(backup_root, exist_ok=True)
    backed_up = []

    for member, rel in entries:
        dest = os.path.join(game, rel)
        if os.path.exists(dest):
            bak = os.path.join(backup_root, rel)
            os.makedirs(os.path.dirname(bak), exist_ok=True)
            shutil.copy2(dest, bak)
            backed_up.append(rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        _extract_member(archive, member, dest)

    _cleanup_rar_tmp()

    manifest = {
        "id": install_id,
        "archive": os.path.abspath(archive),
        "game": game,
        "added": added,
        "replaced": replaced,
        "backed_up": backed_up,
        "installed_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    with open(os.path.join(STATE_DIR, install_id, "manifest.json"), "w",
              encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)

    print(f"[ok] installed {len(added)} new, replaced {len(replaced)}, "
          f"backed up {len(backed_up)}")
    print(f"     undo with:  python installer.py undo {install_id}")
    if backed_up:
        print("     backed up (was overwritten):")
        for r in backed_up[:20]:
            print(f"       ~ {r}")
    return manifest


def _cleanup_rar_tmp():
    if _RAR_TMP[0]:
        shutil.rmtree(_RAR_TMP[0], ignore_errors=True)
        _RAR_TMP[0] = None


def _extract_member(archive, member, dest):
    lower = archive.lower()
    if lower.endswith(".rar"):
        # The archive was already unpacked to a temp dir by the console tool, so
        # `member` is a real path on disk. Copy it into place.
        if os.path.isfile(member):
            shutil.copy2(member, dest)
            return
        if rarfile is None:
            raise SystemExit("RAR temp files vanished before install; re-run the command.")
        with rarfile.RarFile(archive) as rf, rf.open(member) as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out)
        return
    with zipfile.ZipFile(archive) as zf:
        with zf.open(member) as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out)


def undo(install_id):
    rec = os.path.join(STATE_DIR, install_id)
    mpath = os.path.join(rec, "manifest.json")
    if not os.path.isfile(mpath):
        raise SystemExit(f"No such install: {install_id}")
    with open(mpath, "r", encoding="utf-8") as fh:
        m = json.load(fh)
    game = m["game"]
    backup_root = os.path.join(rec, "backup")

    restored = 0
    for rel in m.get("backed_up", []):
        src = os.path.join(backup_root, rel)
        dest = os.path.join(game, rel)
        if os.path.isfile(src):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(src, dest)
            restored += 1

    removed = 0
    for rel in m.get("added", []):
        dest = os.path.join(game, rel)
        if os.path.isfile(dest):
            os.remove(dest)
            removed += 1
            # prune now-empty dirs we created
            d = os.path.dirname(dest)
            while os.path.isdir(d) and d.lower() != game.lower():
                try:
                    os.rmdir(d)
                except OSError:
                    break
                d = os.path.dirname(d)

    with open(os.path.join(rec, "undone.json"), "w", encoding="utf-8") as fh:
        json.dump({"undone_at": datetime.datetime.now().isoformat(timespec="seconds"),
                   "restored": restored, "removed": removed}, fh, indent=2)

    print(f"[ok] undo {install_id}: restored {restored}, removed {removed}")


def list_installs():
    if not os.path.isdir(STATE_DIR):
        print("no installs recorded yet")
        return
    rows = []
    for name in sorted(os.listdir(STATE_DIR)):
        mpath = os.path.join(STATE_DIR, name, "manifest.json")
        if os.path.isfile(mpath):
            try:
                with open(mpath, "r", encoding="utf-8") as fh:
                    m = json.load(fh)
                rows.append((m["id"], m["installed_at"], len(m.get("added", [])),
                             len(m.get("replaced", [])),
                             "undone" if os.path.isfile(
                                 os.path.join(STATE_DIR, name, "undone.json")) else "active"))
            except Exception:
                pass
    if not rows:
        print("no installs recorded yet")
        return
    print(f"{'id':<18}{'when':<21}{'added':>7}{'repl':>6}  state")
    for r in rows:
        print(f"{r[0]:<18}{r[1]:<21}{r[2]:>7}{r[3]:>6}  {r[4]}")


def main():
    ap = argparse.ArgumentParser(description="Install RDR2 mods safely.")
    ap.add_argument("action", choices=["install", "dry-run", "undo", "list"])
    ap.add_argument("target", nargs="?", help="archive path, or install id for undo")
    ap.add_argument("--game", default=DEFAULT_GAME, help="game folder")
    ap.add_argument("--force", action="store_true",
                    help="overwrite existing files (they are still backed up)")
    args = ap.parse_args()

    if args.action == "list":
        list_installs()
        return
    if not args.target:
        ap.error("need an archive path or install id")

    if args.action == "undo":
        undo(args.target)
    elif args.action == "dry-run":
        install(args.target, args.game, dry_run=True)
    else:
        install(args.target, args.game, force=args.force)


if __name__ == "__main__":
    main()
