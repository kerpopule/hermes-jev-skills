#!/usr/bin/env python3
"""Install Hermes Jev Skills for whichever agents live on this machine.

    python3 install.py                 # detect Hermes / Claude Code / Codex and install for each
    python3 install.py --check         # show what would happen, change nothing
    python3 install.py --uninstall

On Hermes it installs every plugin under hermes/plugin, the skills, and the scripts in
hermes/scripts; --uninstall removes those and nothing else.

The `jev` command is a link: one in ~/.local/bin for the person, one in the bin folder of
every Hermes home for the agents. A file called `jev` that this installer did not make is
never replaced and never removed; it is reported instead.

It never asks for, reads or prints an API key. Connecting the key is a separate,
private step: `jev setup-key`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Dict, List, Sequence

REPO = Path(__file__).resolve().parent
JEV = REPO / "bin" / "jev"
PLUGIN_SOURCE = REPO / "hermes" / "plugin"
SCRIPT_SOURCE = REPO / "hermes" / "scripts"
# Discovered, never listed. The installer used to name one plugin, so hermes-handoff shipped
# for weeks in a state where nobody who followed the docs could install it.
PLUGINS = sorted(p.name for p in PLUGIN_SOURCE.iterdir() if (p / "plugin.yaml").is_file())
SCRIPTS = sorted(p.name for p in SCRIPT_SOURCE.iterdir() if p.is_file() and not p.name.startswith("."))
SKILLS = sorted(p.name for p in (REPO / "skills").iterdir() if (p / "SKILL.md").is_file())


def _copytree(src: Path, dst: Path) -> None:
    if dst.is_symlink() or dst.is_file():
        dst.unlink()
    elif dst.is_dir():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))


def _copyfile(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink() or dst.is_dir():
        _remove(dst)
    shutil.copy2(src, dst)  # copy2 keeps the executable bit: these scripts are run from cron or launchd


def _link(target: Path, link: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() or link.is_file():
        link.unlink()
    elif link.is_dir():
        shutil.rmtree(link)
    link.symlink_to(target)


def _remove(path: Path) -> bool:
    if path.is_symlink() or path.is_file():
        path.unlink()
        return True
    if path.is_dir():
        shutil.rmtree(path)
        return True
    return False


def _is_ours(link: Path) -> bool:
    """True only for a symlink that resolves into this checkout, whether or not its target still exists."""
    if not link.is_symlink():
        return False
    try:
        target = link.resolve()
    except (OSError, RuntimeError):            # a symlink loop: RuntimeError before Python 3.13
        return False
    return REPO in target.parents


def _can_write(folder: Path) -> bool:
    """Whether a link could be made in ``folder``, judged from the nearest folder that exists."""
    # A dangling symlink called bin does not "exist", yet mkdir cannot go through it: without
    # the second test --check promised a link the install then refused.
    while not folder.exists() and not folder.is_symlink() and folder != folder.parent:
        folder = folder.parent
    return folder.is_dir() and os.access(folder, os.W_OK | os.X_OK)


def _link_command(link: Path, check: bool) -> "str | None":
    """Point ``link`` at this checkout's bin/jev. Returns why it was left alone, or None when it is ours.

    Only a missing path or a link into this checkout is ever replaced. ``_link`` unlinks
    whatever it finds, which is right inside plugins/, where the name is ours, and wrong
    for a file called ``jev`` on somebody's PATH: a person's own script of that name went
    with no backup. Every failure comes back as a reason, never as an exception, so one
    read-only lane costs that lane its link and not the other forty theirs.
    """
    try:
        if link.is_symlink():
            if not _is_ours(link):
                return f"is a symlink to {os.readlink(link)}, which is outside this checkout"
            if os.readlink(link) == str(JEV):
                return None                    # already right, so a second install rewrites nothing
        elif link.exists():
            return ("is a folder" if link.is_dir() else "is a file") + " this installer did not put there"
        if check:
            return None if _can_write(link.parent) else f"cannot be linked: {link.parent} is not a writable folder"
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink():
            link.unlink()                      # ours: a stale or dangling link into this checkout
        link.symlink_to(JEV)
    except OSError as exc:
        return f"could not be linked: {exc.strerror or exc}"
    return None


# ── Hermes ───────────────────────────────────────────────────────────────────

def _has_config(home: Path) -> bool:
    try:
        return (home / "config.yaml").is_file()
    except OSError:                            # a profile folder we may not read: is_file raises on EACCES before 3.14
        return False


def hermes_homes(root: Path) -> List[Path]:
    homes = [root]
    profiles = root / "profiles"
    if profiles.is_dir():
        homes += sorted(p for p in profiles.iterdir() if _has_config(p))
    return homes


def hermes_root(home: Path) -> Path:
    """The fleet root for a home that may be one profile: <root>/profiles/<name>. Same rule as jevkit.catalog.

    Two things the bare rule got wrong here, where it decides where links are WRITTEN.
    ``--hermes-home .`` from inside a lane has no parent called profiles until it is made
    absolute, so that lane alone was linked. And a root that merely lives in a folder
    called profiles (/srv/profiles/hermes) sent a `jev` link to /srv/bin, outside any
    Hermes home, so the folder two up has to look like a Hermes home itself.
    """
    lexical = Path(os.path.abspath(home))      # abspath, not resolve: a symlinked ~/.hermes keeps its name
    if lexical.parent.name == "profiles" and _has_config(lexical.parent.parent):
        return lexical.parent.parent
    return home


def command_homes(home: Path, every: bool = False) -> List[Path]:
    """Every Hermes home whose agent shell looks for `jev` in its own bin folder.

    An agent shell's PATH carries <HERMES_HOME>/bin, and HERMES_HOME is the PROFILE's
    home, so a link at the root alone left every profile lane at "command not found":
    on a forty-profile fleet, none of the forty got one.

    Counted from the fleet root even when ``home`` is a single profile, which is what an
    agent running the installer hands over. The set is then the same whoever asks, and
    --uninstall from an ordinary shell finds every link an agent's install made.

    ``every`` drops the config.yaml test, for --uninstall: removal only ever takes a link
    into this checkout, so looking in more places is safe, and a lane whose config went
    away after the install would otherwise keep a link nothing can find again.
    """
    root = hermes_root(home)
    try:
        if not every:
            return hermes_homes(root)
        profiles = root / "profiles"
        return [root] + (sorted(p for p in profiles.iterdir() if p.is_dir()) if profiles.is_dir() else [])
    except OSError:                            # an unreadable profiles folder: keep the one home we were given
        return [home]


def enable_plugins(config: Path, names: Sequence[str], enable: bool) -> Dict[str, str]:
    """Add or remove each plugin under plugins.enabled by editing only that list.

    A text edit, not a YAML round-trip: comments, ordering and every other setting survive.
    One pass for all of them, so a two-plugin install leaves one backup rather than two.
    """
    text = config.read_text(encoding="utf-8")
    lines = text.split("\n")
    status: Dict[str, str] = {}
    try:
        start = next(i for i, line in enumerate(lines) if line.rstrip() == "plugins:")
    except StopIteration:
        if not enable:
            return {name: "no plugins section" for name in names}
        if lines and lines[-1] == "":
            lines.pop()                        # the file's own final newline, put back below
        lines += ["plugins:", "  enabled:"] + [f"  - {name}" for name in sorted(names)] + [""]
        status = {name: "enabled" for name in names}
        start = None
    if start is not None:
        end = next((i for i in range(start + 1, len(lines)) if lines[i] and not lines[i].startswith((" ", "#"))), len(lines))
        block = lines[start + 1:end]
        key = next((i for i, line in enumerate(block) if re.match(r"^\s*enabled:\s*(\[\s*\])?\s*$", line)), None)
        # ── 2026-09-21 修 #8: 缩进必须跟随文件已有格式 ──
        # 原实现硬编码 2 空格（"  enabled:" / "  - name"），遇到 Hermes 默认 4 空格
        # 列表项（`    - hermes-lcm`）时，新项以 2 空格插入在它前面，YAML 把后一行
        # 解析成前一字符串的续行 → `- hermes-jev - hermes-lcm`，原插件静默失效。
        _key_line = block[key] if key is not None else None
        _items = [l for l in block if re.match(r"^\s*-\s*\S", l)]
        key_indent = "  "
        item_indent = "    "
        # ── 2026-09-21 修 flow-style: `enabled: ['a', 'b']` 不匹配上面的正则，
        # key 会是 None → 走插入分支 → 造出重复 key（新项静默失效）。
        # 先把它规范化成 block list（保留原项），再走统一逻辑。
        if key is None:
            _flow = next((i for i, line in enumerate(block)
                          if re.match(r"^\s*enabled:\s*\[.+\]\s*$", line)), None)
            if _flow is not None:
                _fl = re.match(r"^(\s*)enabled:", block[_flow]).group(1)
                key_indent = _fl
                item_indent = _fl + "  "
                _raw = re.match(r"^\s*enabled:\s*\[(.*)\]\s*$", block[_flow]).group(1)
                _vals = [x.strip().strip("'\"") for x in _raw.split(",") if x.strip()]
                block[_flow] = f"{key_indent}enabled:"
                for _j, _x in enumerate(_vals):
                    block.insert(_flow + 1 + _j, f"{item_indent}- {_x}")
                key = _flow
        else:
            key_indent = re.match(r"^(\s*)", _key_line).group(1)
            item_indent = (re.match(r"^(\s*)", _items[0]).group(1) if _items else key_indent + "  ")
        # Every plugin goes in at the same spot, so walking the names backwards leaves
        # them alphabetical in the file.
        for name in sorted(names, reverse=True):
            item = re.compile(rf"^\s*-\s*['\"]?{re.escape(name)}['\"]?\s*$")
            present = [i for i, line in enumerate(block) if item.match(line)]
            if enable:
                if present:
                    status[name] = "already enabled"
                    continue
                if key is None:
                    block.insert(0, f"{key_indent}enabled:")
                    key = 0
                block[key] = f"{key_indent}enabled:"          # turns `enabled: []` into a block list
                block.insert(key + 1, f"{item_indent}- {name}")
                status[name] = "enabled"
            else:
                if not present:
                    status[name] = "was not enabled"
                    continue
                for i in reversed(present):
                    del block[i]
                status[name] = "disabled"
        lines[start + 1:end] = block
        status = {name: status[name] for name in sorted(status)}  # report reads like the file
    if not any(state in ("enabled", "disabled") for state in status.values()):
        return status                          # nothing moved, so no backup and no rewrite
    backup = config.with_name(f"{config.name}.bak-jev-{time.strftime('%Y%m%dT%H%M%S')}")
    shutil.copy2(config, backup)
    temp = config.with_name(config.name + ".jev-tmp")
    temp.write_text("\n".join(lines), encoding="utf-8")
    os.replace(temp, config)
    return status


def install_hermes(root: Path, enable: str, check: bool) -> Dict[str, object]:
    homes = hermes_homes(root)
    if enable == "all":
        wanted = {"default"} | {h.name for h in homes[1:]}
    elif enable == "none":
        wanted = set()
    else:
        wanted = set(filter(None, enable.split(",")))
    report: Dict[str, object] = {
        "home": str(root),
        "profiles": len(homes) - 1,
        "plugins": {name: str(root / "plugins" / name) for name in PLUGINS},
        "scripts": [str(root / "scripts" / name) for name in SCRIPTS],
        "enabled_in": {},
    }
    if check:
        report["would_enable_in"] = sorted(wanted)
        return report
    for name in PLUGINS:
        plugin_dir = root / "plugins" / name
        _copytree(PLUGIN_SOURCE / name, plugin_dir)
        # A copy of jevkit per plugin, because a plugin can be loaded alone: nightly-handoff.py
        # puts only the hermes-handoff directory on sys.path and imports jevkit from there.
        _copytree(REPO / "jevkit", plugin_dir / "jevkit")
    skills_dir = root / "skills" / "jev"
    skills_dir.mkdir(parents=True, exist_ok=True)
    for name in SKILLS:
        _copytree(REPO / "skills" / name, skills_dir / name)
    for name in SCRIPTS:
        _copyfile(SCRIPT_SOURCE / name, root / "scripts" / name)
    # One lane that cannot be written costs that lane its install, not the run. It used to
    # raise from here: the lanes after it got nothing, and the report, with every warning
    # already collected in it, was never printed.
    failed: Dict[str, str] = {}
    for home in homes[1:]:
        try:
            for name in PLUGINS:
                _link(root / "plugins" / name, home / "plugins" / name)   # every lane scans its OWN plugins folder
            _link(skills_dir, home / "skills" / "jev")
        except OSError as exc:
            failed[str(home)] = f"could not be installed into: {exc.strerror or exc}"
    for home in homes:
        label = "default" if home == root else home.name
        if label in wanted and str(home) not in failed and (home / "config.yaml").is_file():
            try:
                report["enabled_in"][label] = enable_plugins(home / "config.yaml", PLUGINS, True)  # type: ignore[index]
            except OSError as exc:
                failed[str(home)] = f"plugins are in place but could not be enabled: {exc.strerror or exc}"
    if failed:
        report["not_installed_in"] = failed
    return report


def uninstall_hermes(root: Path) -> Dict[str, object]:
    removed = []
    failed: Dict[str, str] = {}
    for home in hermes_homes(root):
        try:                                   # a read-only lane raised here, and every `jev` link stayed behind
            if (home / "config.yaml").is_file():
                enable_plugins(home / "config.yaml", PLUGINS, False)
            for path in [home / "plugins" / name for name in PLUGINS] + [home / "skills" / "jev"]:
                if _remove(path):
                    removed.append(str(path))
        except OSError as exc:
            failed[str(home)] = f"could not be cleaned up: {exc.strerror or exc}"
    try:
        for name in SCRIPTS:                   # installed at the root only, so removed there only
            if _remove(root / "scripts" / name):
                removed.append(str(root / "scripts" / name))
    except OSError as exc:
        failed.setdefault(str(root), f"could not be cleaned up: {exc.strerror or exc}")
    try:
        (root / "scripts").rmdir()             # goes only if empty: a Hermes home often keeps its own scripts here
    except OSError:
        pass
    return {"removed": removed, **({"not_removed_from": failed} if failed else {})}


# ── skill folders (Claude Code, Codex, generic) ──────────────────────────────

def install_skills(folder: Path, check: bool) -> Dict[str, object]:
    if not check:
        folder.mkdir(parents=True, exist_ok=True)
        for name in SKILLS:
            _copytree(REPO / "skills" / name, folder / name)
    return {"folder": str(folder), "skills": SKILLS}


def install_cli(check: bool, hermes_home: "Path | None" = None) -> Dict[str, object]:
    target = Path.home() / ".local" / "bin" / "jev"
    not_linked: Dict[str, str] = {}
    reason = _link_command(target, check)
    if reason:
        not_linked[str(target)] = reason
    on_path = str(target.parent) in os.environ.get("PATH", "").split(os.pathsep)
    # A Hermes agent shell does not carry ~/.local/bin, but it does carry its own Hermes
    # home's bin directory. Without these links every skill that says `jev choose` dies
    # with command not found in exactly the place those skills run.
    shims: List[str] = []
    if hermes_home is not None and (hermes_home / "config.yaml").is_file():
        for home in command_homes(hermes_home):
            shim = home / "bin" / "jev"
            reason = _link_command(shim, check)
            if reason:
                not_linked[str(shim)] = reason
            else:
                shims.append(str(shim))
    return {"command": str(target), "on_path": on_path,
            **({"agent_shell_commands": shims} if shims else {}),
            **({"not_linked": not_linked} if not_linked else {})}


def uninstall_cli(hermes_home: "Path | None" = None) -> Dict[str, object]:
    """Remove every `jev` link that resolves into this checkout, and nothing else.

    This deleted whatever sat at bin/jev without looking, so a person's own `jev`, or the
    link another checkout had made, went out with ours.
    """
    places = [Path.home() / ".local" / "bin" / "jev"]
    if hermes_home is not None:
        places += [home / "bin" / "jev" for home in command_homes(hermes_home, every=True)]
    removed: List[str] = []
    left_alone: Dict[str, str] = {}
    for link in places:
        try:
            if _is_ours(link):
                link.unlink()
                removed.append(str(link))
            elif link.is_symlink() or link.exists():
                left_alone[str(link)] = "is not a link into this checkout"
        except OSError as exc:
            left_alone[str(link)] = f"could not be removed: {exc.strerror or exc}"
    return {"removed": removed, **({"left_alone": left_alone} if left_alone else {})}


def path_warning(cli: Dict[str, object]) -> str | None:
    """Warn when ~/.local/bin is not on PATH, which it is not by default on macOS or most Linux.

    This lived inside the ``cli`` object, where nobody read it, and the very next command
    the docs give a person — ``jev setup-key`` — died with "command not found".

    It looks at PATH and nothing else. For one release it also went quiet whenever a
    Hermes home had its own link, but that link serves the agent's shell, and ``jev
    setup-key`` is the one command the person has to run in their own terminal, so that
    the key never passes through an agent. Every Hermes install lost the warning.
    """
    if cli["on_path"]:
        return None
    command = Path(str(cli["command"]))
    text = (f"The `jev` command goes to {command}, but {command.parent} is not on PATH, so "
            f"`jev setup-key` and every other `jev` command will fail with command not found. "
            f"Either add {command.parent} to PATH in your shell profile, or run "
            f"{JEV} everywhere the docs say `jev`.")
    shims = [str(shim) for shim in cli.get("agent_shell_commands") or ()]  # type: ignore[attr-defined]
    if shims:
        more = f" and {len(shims) - 1} more" if len(shims) > 1 else ""
        text += (f" This is about your own terminal, which is where `jev setup-key` has to be run. "
                 f"Hermes agents are not affected: their shells carry <HERMES_HOME>/bin, and `jev` "
                 f"is linked there too ({shims[0]}{more}).")
    return text


def link_warning(cli: Dict[str, object], mode: str) -> str | None:
    """Name every place a `jev` of somebody else's was left where it was, and why.

    The refusal is the safe half; saying nothing would be the other failure, a lane whose
    agent still gets command not found after an install that reported success.
    """
    skipped = dict(cli.get("left_alone" if mode == "uninstall" else "not_linked") or {})  # type: ignore[call-overload]
    if not skipped:
        return None
    lines = "\n".join(f"  {path} {reason}" for path, reason in skipped.items())
    if mode == "uninstall":
        return f"Not removed, so a `jev` may still be found there:\n{lines}"
    tense = "would not be linked" if mode == "check" else "was not linked"
    return (f"`jev` {tense} in {len(skipped)} place(s). Nothing there was replaced or deleted, and "
            f"the rest of the install carried on. If `jev` should live there, move what is in "
            f"the way aside and run the installer again:\n{lines}")


def lane_warning(hermes: Dict[str, object]) -> str | None:
    """Name every lane the plugins and skills did not reach, and why."""
    left = dict(hermes.get("not_removed_from") or {})  # type: ignore[call-overload]
    if left:
        lines = "\n".join(f"  {home} {reason}" for home, reason in left.items())
        return f"{len(left)} Hermes lane(s) may still hold the plugins and skills:\n{lines}"
    failed = dict(hermes.get("not_installed_in") or {})  # type: ignore[call-overload]
    if not failed:
        return None
    lines = "\n".join(f"  {home} {reason}" for home, reason in failed.items())
    return (f"{len(failed)} Hermes lane(s) did not get the plugins and skills; every other lane did. "
            f"Fix what is in the way and run the installer again:\n{lines}")


def nothing_installed_warning(hermes: Path, check: bool) -> str:
    """Say plainly that a machine with no agent on it got nothing but the CLI.

    Without this the run looked like every successful one: exit 0, success-shaped JSON,
    and a next-steps list for a Hermes that is not there.
    """
    tense = "would be installed" if check else "was installed"
    return (f"No agent was found on this machine: no Hermes home at {hermes}, and no Claude "
            f"Code, Codex or generic skills folder, so nothing {tense} except the `jev` command "
            f"itself. If your agent reads skills from somewhere else, install them there with "
            f"--skills-dir <path>.")


def home_warning(hermes: Path) -> str | None:
    """Warn when the resolved Hermes home is a single profile, not the fleet root.

    Agents run with ``HERMES_HOME`` set to their own profile directory, so a bare
    ``python3 install.py`` from an agent shell installs for that lane only and every
    other lane keeps the old copy. The `jev` link is the exception: that one is counted
    from the fleet root whoever runs the installer.
    """
    if any(part == "profiles" for part in hermes.parts):
        return (f"HERMES_HOME resolved to a profile home ({hermes}), so apart from the `jev` "
                f"command, which is linked for every lane, this install "
                f"covers that lane only. For the whole fleet pass "
                f"--hermes-home {Path.home() / '.hermes'}")
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--hermes-home", default=None,
                        help="Hermes root (default: $HERMES_HOME, else ~/.hermes)")
    parser.add_argument("--enable", default="all", help="Hermes profiles to enable the plugins in: all, none, or a,b,c")
    parser.add_argument("--skills-dir", action="append", default=[], help="extra skill folder to install into")
    args = parser.parse_args()

    home = Path.home()
    hermes = Path(args.hermes_home).expanduser() if args.hermes_home else Path(
        os.environ.get("HERMES_HOME") or str(home / ".hermes")).expanduser()
    folders = [Path(p).expanduser() for p in args.skills_dir]
    folders += [p for p in (home / ".claude" / "skills", home / ".codex" / "skills", home / ".agents" / "skills") if p.parent.is_dir()]

    report: Dict[str, object] = {"repo": str(REPO), "mode": "uninstall" if args.uninstall else "check" if args.check else "install"}
    warnings: List[str] = [w for w in (home_warning(hermes),) if w]
    if args.uninstall:
        if hermes.is_dir():
            report["hermes"] = uninstall_hermes(hermes)
            warnings += [w for w in (lane_warning(report["hermes"]),) if w]  # type: ignore[arg-type]
        report["skills_removed"] = [str(f / n) for f in folders for n in SKILLS if _remove(f / n)]
        # Not gated on hermes.is_dir(): a lane that was deleted is still a way of naming the
        # fleet root, and its install had linked every other lane.
        cli = uninstall_cli(hermes)
        report["cli"] = cli
        warnings += [w for w in (link_warning(cli, "uninstall"),) if w]
    else:
        cli = install_cli(args.check, hermes)
        report["cli"] = cli
        warnings += [w for w in (path_warning(cli), link_warning(cli, str(report["mode"]))) if w]
        if (hermes / "config.yaml").is_file():
            report["hermes"] = install_hermes(hermes, args.enable, args.check)
            warnings += [w for w in (lane_warning(report["hermes"]),) if w]  # type: ignore[arg-type]
        report["skill_folders"] = [install_skills(f, args.check) for f in folders]
        steps = ["jev doctor", "jev setup-key   (only if the key is missing; the person pastes it in a private page)",
                 "jev models suggest --write   (only if no routing pools exist yet)"]
        if "hermes" in report:
            steps.append("Hermes: restart the gateway when convenient, then /jev routing shadow")
        elif not folders:
            warnings.append(nothing_installed_warning(hermes, args.check))
        report["next"] = steps
    # Warning first, so it is read before the wall of paths underneath it.
    print(json.dumps({**({"warning": "\n".join(warnings)} if warnings else {}), **report}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
