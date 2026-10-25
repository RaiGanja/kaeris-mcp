"""KAERIS i18n MCP server.

Exposes AI-native localization tools over the Model Context Protocol so agents
(Claude Desktop, Cursor, Claude Code, …) can translate app strings on demand.

Auth (optional — the free anonymous tier works up to 10k chars):
  KAERIS_API_KEY         Pro/Scale key (kaerisp_…)
  KAERIS_OPENROUTER_KEY  OpenRouter key for Lifetime/BYOK
  KAERIS_API_URL         override the API base URL (default https://kaeris.dev). Must be
                         https — over http the key and the strings being translated travel
                         in the clear; http is accepted for localhost only.
"""

import json
import os
import re
import sys

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # MCP SDK >= 2.0 renamed FastMCP to MCPServer and moved it
    # Same decorator/run surface (.tool(), .run(), .list_tools(), .call_tool()), so the
    # rest of this file is version-agnostic. Without this, `pip install kaeris-mcp` in a
    # fresh environment resolves to the newest SDK and dies on this import.
    from mcp.server.mcpserver import MCPServer as FastMCP

from kaeris.client import KaerisClient, KaerisError, KaerisTimeout, safe_member_name
from kaeris import check as chk
from kaeris import detectors as det
from kaeris import incremental as inc
from kaeris import xcstrings as xc

from . import __version__

SERVER_NAME = "kaeris-i18n"  # how configs and registries identify us — never rename lightly


def _build_server():
    """A server that tells clients which version it is.

    Both SDKs report `serverInfo` in the initialize handshake and we filled in neither, so
    Claude Desktop and every registry probe saw an empty version — no way to tell a broken
    old install from a current one. The two SDKs carry the field differently: 2.x takes
    `version=` on the constructor, 1.x doesn't accept it at all and holds it on the
    low-level server underneath. Setting the attribute is enough there because the handshake
    reads it when it builds the response, not at construction time.
    """
    try:
        return FastMCP(SERVER_NAME, version=__version__)  # SDK >= 2.0
    except TypeError:
        pass
    server = FastMCP(SERVER_NAME)  # SDK 1.x
    low_level = getattr(server, "_mcp_server", None)
    if low_level is not None:
        low_level.version = __version__
    return server


mcp = _build_server()

# ---------------------------------------------------------------------------
# Shared helpers for the local (no-API) repo-aware tools: kaeris_scan_repo,
# kaeris_status, kaeris_list_missing_keys. kaeris_add_language is the only
# tool below that calls the translation API.
# ---------------------------------------------------------------------------

# Directories never worth descending into when scanning a repo for locale files.
_SKIP_DIRS = {
    "node_modules", "dist", "build", "target", "vendor", "coverage",
    ".git", ".venv", "venv", "__pycache__", ".next", ".cache", ".idea", ".vscode",
}
# Every format the API accepts (backend `_SUPPORTED_EXTS`). Kept in step by
# tests/test_promises_match_the_product.py: when the two drifted, an agent read our own tool
# description and told its human that a format we sell is not supported.
_LOCALE_EXTS = {".json", ".yaml", ".yml", ".arb", ".strings", ".po", ".xml", ".csv",
                ".xliff", ".xlf", ".properties", ".resx", ".ftl", ".xcstrings",
                ".md", ".mdx", ".markdown"}
# An Xcode String Catalog holds EVERY language in one file, so it has no language in its name
# — its languages are read out of the catalog itself.
_CATALOG_EXTS = {".xcstrings"}
# A bare language/locale code: en, de, en-US, en_US, zh-Hans, pt-BR, ...
_LANG_RE = re.compile(r"^[a-zA-Z]{2,3}([_-][a-zA-Z]{2,4})?$")
_PREFERRED_BASE = ("en", "en-us", "en_us")

_MAX_SCAN_ENTRIES = 20000  # bound total files visited
_MAX_SCAN_DEPTH = 8        # bound directory depth below the scan root


def _walk_bounded(root):
    """Yield (dirpath, filename) for every file under `root`, skipping
    node_modules/.git/build-output-style dirs and hidden dirs, and capping
    both depth and total file count so a huge repo can't blow up the scan."""
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        depth = 0 if dirpath == root else os.path.relpath(dirpath, root).count(os.sep) + 1
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        if depth >= _MAX_SCAN_DEPTH:
            dirnames[:] = []
        for name in filenames:
            count += 1
            if count > _MAX_SCAN_ENTRIES:
                return
            yield dirpath, name


def _classify_locale_file(root, dirpath, name):
    """Best-effort: is this file a locale file, and if so what language does
    it represent? Returns {"path", "lang", "ext"} (path relative to `root`)
    or None if it doesn't look like a locale file at all."""
    ext = os.path.splitext(name)[1].lower()
    if ext not in _LOCALE_EXTS:
        return None
    stem = name[: -len(ext)]
    parent = os.path.basename(dirpath)
    rel_path = os.path.relpath(os.path.join(dirpath, name), root)

    lang = None
    if ext == ".strings" and parent.endswith(".lproj"):
        # iOS: en.lproj/Localizable.strings
        lang = parent[: -len(".lproj")]
    elif ext == ".xml" and (parent == "values" or parent.startswith("values-")):
        # Android: values/strings.xml (base/default) or values-de/strings.xml
        lang = "en" if parent == "values" else parent.split("values-", 1)[1]
    elif ext == ".po" and parent == "LC_MESSAGES":
        # gettext: locale/de/LC_MESSAGES/messages.po
        grandparent = os.path.basename(os.path.dirname(dirpath))
        lang = grandparent if _LANG_RE.match(grandparent) else None
    elif ext == ".arb":
        # Flutter ARB: app_en.arb / app_en_US.arb — the locale is everything AFTER the app
        # prefix (split on the FIRST underscore, so app_en_US -> en_US, not US). A bare en.arb
        # keeps its stem. Handled before the generic stem match because _LANG_RE would otherwise
        # read "app_en" itself as a locale.
        if "_" in stem:
            cand = stem.split("_", 1)[1]
            lang = cand if _LANG_RE.match(cand) else (stem if _LANG_RE.match(stem) else None)
        elif _LANG_RE.match(stem):
            lang = stem
    elif _LANG_RE.match(stem):
        # locales/en.json, messages/pt-BR.json, ...
        lang = stem
    elif "_" in stem and _LANG_RE.match(stem.rsplit("_", 1)[-1]):
        # <name>_<lang> style for other formats: messages_en.properties
        lang = stem.rsplit("_", 1)[-1]
    elif _LANG_RE.match(parent):
        # i18next-style namespace file inside a lang-named dir: locales/en/translation.json
        lang = parent

    if lang is None:
        return None
    return {"path": rel_path.replace(os.sep, "/"), "lang": lang, "ext": ext}


def _guess_base_lang(langs):
    """Pick the most likely source/base language out of the languages found."""
    if not langs:
        return None
    lower_map = {l.lower(): l for l in langs}
    for cand in _PREFERRED_BASE:
        if cand in lower_map:
            return lower_map[cand]
    for l in langs:
        if l.lower().startswith("en"):
            return l
    return sorted(langs)[0]


def _guess_framework(found):
    """Heuristic framework/convention guess from the shape of the paths found."""
    paths = [f["path"] for f in found]

    def any_match(pattern):
        return any(re.search(pattern, p) for p in paths)

    if any_match(r"(^|/)l10n/.+\.arb$"):
        return "flutter (arb)"
    if any_match(r"(^|/)values(-[^/]+)?/[^/]+\.xml$"):
        return "android (strings.xml)"
    if any_match(r"\.xcstrings$"):
        return "ios/macos (xcstrings catalog)"
    if any_match(r"\.lproj/[^/]+\.strings$"):
        return "ios (.strings)"
    if any_match(r"LC_MESSAGES/[^/]+\.po$"):
        return "gettext (.po)"
    if any_match(r"(^|/)messages/[^/]+\.json$"):
        return "next-intl"
    if any_match(r"(^|/)src/locales/[^/]+\.json$"):
        return "vue-i18n"
    if any_match(r"(^|/)locales/[^/]+/.+\.json$"):
        return "i18next"
    if found:
        return "generic (flat locale files)"
    return None


def scan_repo(path="."):
    """Pure implementation behind kaeris_scan_repo — see that tool's docstring."""
    root = path or "."
    if not os.path.isdir(root):
        raise FileNotFoundError(
            # The agent reading this is the one that must recover: a bare path told it
            # nothing, so say what is wrong and what a good value looks like.
            f"no such directory: {root} — pass an absolute path, or the repo root you want scanned"
        )

    found, catalogs = [], []
    for dirpath, name in _walk_bounded(root):
        if os.path.splitext(name)[1].lower() in _CATALOG_EXTS:
            catalogs.append(os.path.join(dirpath, name))
            continue
        info = _classify_locale_file(root, dirpath, name)
        if info:
            found.append(info)

    # String Catalogs: one file, every language inside it. Read them so an Xcode project is
    # not reported as "no i18n here" — the catalog IS the whole setup.
    catalog_langs, catalog_source = set(), None
    for path in catalogs:
        try:
            src_lang, by_lang = xc.load(path)
        except Exception:
            continue                      # unreadable catalog: report the file, guess nothing
        catalog_source = catalog_source or src_lang
        catalog_langs.update(by_lang)
        found.append({"path": os.path.relpath(path, root).replace(os.sep, "/"),
                      "lang": src_lang, "ext": ".xcstrings"})

    langs_present = sorted({f["lang"] for f in found} | catalog_langs)
    # A catalog states its own source language — that beats guessing from file names.
    base_lang = catalog_source or _guess_base_lang(langs_present)
    target_langs = [l for l in langs_present if l != base_lang] if base_lang else langs_present

    return {
        "root": os.path.abspath(root),
        "locale_files": sorted(f["path"] for f in found),
        "base_lang": base_lang,
        "target_langs": target_langs,
        "framework": _guess_framework(found),
        "file_count": len(found),
    }


def _resolve_out_dir(source, out):
    return out or os.path.dirname(os.path.abspath(source)) or "."


def _safe_segments(member_name):
    """Path segments a downloaded member may be written under. Raises if the name tries to
    leave the output directory — see KaerisClient.download for what this defends against."""
    return safe_member_name(member_name).split("/")


def _refuse_outside_project(dest, out_dir):
    """The written file must stay inside the project. Layout mirroring legitimately writes to
    a SIBLING of out_dir (locales/en -> locales/de), so the boundary is the folder that holds
    out_dir — one step up, and not one step more."""
    root = os.path.abspath(os.path.dirname(os.path.abspath(out_dir)) or os.sep)
    full = os.path.abspath(dest)
    if full != root and not full.startswith(root + os.sep):
        raise RuntimeError(
            f"Refusing to write {dest} — it is outside the project directory {root}. "
            "Nothing was written.")
    return full


def _target_locale_path(source, lang, out_dir, member_name):
    """Where the `lang` translation belongs, mirroring the SOURCE file's layout for the two
    framework conventions the flat backend member name ('<lang>.json') loses: i18next/
    vue-i18n namespace dirs (locales/<lang>/translation.json) and Flutter ARB
    (app_<lang>.arb). Applied only when working beside the source (the default out); every
    other case — flat <lang>.json, Android values-<lang>/, CSV, or an explicit custom out —
    keeps the backend member name under out_dir, which is already correct.

    This is the MCP analogue of the CLI's _target_path: `kaeris translate` writes to these
    layouts, so add_language/translate_file must write — and status/list_missing must look —
    at the same place, not a phantom <out>/<lang>.json."""
    src_dir = os.path.dirname(source)
    base_name = os.path.basename(source)
    ext = os.path.splitext(base_name)[1].lower()
    stem = base_name[: -len(ext)] if ext else base_name
    parent = os.path.basename(src_dir)
    beside_source = os.path.abspath(out_dir) == os.path.abspath(src_dir or ".")

    if beside_source:
        # i18next / vue-i18n: the source's PARENT dir is its language code.
        if ext in (".json", ".yaml", ".yml") and _LANG_RE.match(parent):
            return os.path.join(os.path.dirname(src_dir) or ".", lang, base_name)
        # Flutter ARB with an app-prefix: app_<base>.arb -> app_<lang>.arb.
        if ext == ".arb" and "_" in stem:
            prefix, _sep, langish = stem.partition("_")
            if _LANG_RE.match(langish):
                return os.path.join(src_dir or ".", f"{prefix}_{lang}{ext}")

    # The member name comes off the wire, so it is built into a path from sanitized segments
    # only, and the result is checked to stay inside the project (the folder holding out_dir,
    # which is as far as layout-mirroring ever reaches — locales/en -> locales/de). The client
    # already refuses escaping names; this is the second half of the same guard, on the side
    # that actually opens the file for writing.
    dest = os.path.join(out_dir, *_safe_segments(member_name))
    _refuse_outside_project(dest, out_dir)
    return dest


def _discover_locale_langs(source, out_dir):
    """Fallback when no langs/config are given: sibling <lang>.json files
    next to the source, e.g. locales/en.json + locales/de.json -> ["de"]."""
    ext = os.path.splitext(source)[1]
    base_name = os.path.basename(source)
    if not ext:
        return []
    try:
        entries = sorted(os.listdir(out_dir))
    except OSError:
        return []
    langs = []
    for name in entries:
        if name == base_name or not name.endswith(ext):
            continue
        stem = name[: -len(ext)]
        if stem and _LANG_RE.match(stem):
            langs.append(stem)
    return langs


def _load_kaeris_config(source):
    """Minimal kaeris.json reader (source dir, then cwd) — just enough to pick
    up a project's `langs`/`out` defaults, matching the CLI's config precedence."""
    cfg_dir = os.path.dirname(os.path.abspath(source)) or "."
    for d in (cfg_dir, "."):
        path = os.path.join(d, "kaeris.json")
        if os.path.isfile(path):
            try:
                # utf-8-sig: kaeris.json, сохранённый на Windows, начинается с BOM, и
                # строгий utf-8 на нём падает — тихо, в except ниже, вместе с langs/out
                # проекта. Файлы локалей сюда не попадают: они уходят на сервер байтами,
                # и кодировку разбирает он (пункт 7 плана безопасности).
                with open(path, encoding="utf-8-sig") as f:
                    raw = json.load(f)
            except (OSError, ValueError):
                return {}
            if isinstance(raw, dict):
                return {k: v for k, v in raw.items() if not k.startswith("//") and not k.startswith("_")}
            return {}
    return {}


def _resolve_langs(source, langs, out_dir):
    if langs:
        return [str(l).strip() for l in langs if str(l).strip()]
    cfg = _load_kaeris_config(source)
    cfg_langs = cfg.get("langs")
    if cfg_langs:
        return [str(l).strip() for l in cfg_langs if str(l).strip()]
    return _discover_locale_langs(source, out_dir)


def _source_ext(path):
    return os.path.splitext(path)[1] or ".json"


def _load_flat(path, client=None):
    """Flat {key: value} for a locale file of ANY supported format. JSON is parsed locally
    (fast, offline, no network); every other format (.arb/.strings/.po/.xml/.ftl/…) is parsed
    by the backend /api/parse endpoint (no cost, no translation). Raises FileNotFoundError if
    the file is absent; KaerisError if the backend parse fails."""
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"no such file: {path} — check the path is relative to the directory the server runs in"
        )
    if path.lower().endswith(".json"):
        obj = inc.load_json(path)
        if not isinstance(obj, dict):
            raise ValueError(f"Invalid file {path}: expected a JSON object")
        return inc.flatten_all(obj)
    with open(path, "rb") as f:
        content = f.read()
    return (client or _client()).parse(os.path.basename(path), content)


def _current_model(client):
    """The exact model this key's tier translates with — recorded in the lock so a tier switch
    is caught as a settings change. Best-effort: an older/unreachable server reports "", which
    settings_changed() reads as "unknown, not changed" rather than forcing a full retranslate."""
    try:
        return client.key_info().get("model_id", "") or ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Bounds on what a tool hands back to the model.
#
# Measured 16.08.2026 on a file of the size the product officially accepts (2 MB, 20 237
# strings): kaeris_list_missing_keys returned 2.7M characters — about 667 000 tokens in ONE
# tool result. That does not "make the answer long", it ends the agent's session, and the
# client pays their own provider for every token of it. Our own 239-string locale already
# cost ~4 500 tokens, and kaeris_status over 10 languages ~9 800.
#
# The rule: counts and the verdict are ALWAYS exact and complete — they are what a decision
# is made on. Only the detail lists are paged, and a paged list says so in the same breath
# (a silent cut reads as "that's all of it", which is how a tool starts lying).
# ---------------------------------------------------------------------------
_PAGE_DEFAULT = 200      # detail entries per page unless the caller asks for another size
_PAGE_MAX = 2000         # ceiling even when the caller asks for more
_PAGE_CHAR_BUDGET = 60000  # ≈15k tokens: a page stops here even if the entry count doesn't
_STATUS_EXAMPLES = 50    # per-language / per-list examples inside kaeris_status


def _page_keys(keys, values, offset, limit):
    """One page of (keys, {key: value}) plus honest bookkeeping.

    Cuts on WHOLE entries only: a value sliced in half is corrupted content, and content is
    the client's, not ours."""
    total = len(keys)
    offset = max(0, int(offset or 0))
    limit = min(max(1, int(limit or _PAGE_DEFAULT)), _PAGE_MAX)
    window = keys[offset:offset + limit]

    page, used = [], 0
    for k in window:
        v = values.get(k) if values is not None else None
        used += len(k) + (len(v) if isinstance(v, str) else 8) + 8
        if page and used > _PAGE_CHAR_BUDGET:
            break
        page.append(k)

    shown_to = offset + len(page)
    info = {"total": total, "returned": len(page), "offset": offset,
            "complete": offset == 0 and shown_to >= total}
    if shown_to < total:
        info["next_offset"] = shown_to
        info["note"] = (f"showing {len(page)} of {total} — this is a PAGE, not the whole list. "
                        f"Call again with offset={shown_to} for the next one, or use "
                        f"kaeris_add_language / kaeris_translate_file(only_new=True) to have "
                        f"KAERIS fill them in instead of doing it key by key.")
    return page, info


def _cap(items, limit=_STATUS_EXAMPLES):
    """First `limit` entries of a detail list, plus how many there are in total."""
    return list(items[:limit]), len(items)


# ---------------------------------------------------------------------------
# How long one tool call may block.
#
# The client used to wait up to 1800 seconds inside a single tool call (and 180s per HTTP
# request). No MCP client waits half an hour — Claude Desktop, Cursor and the rest cut a call
# off on their own schedule, and when they do, the job id goes with it. The work was submitted,
# it is being paid for, and nobody can ever collect it: the tool returned nothing to come back
# with. Measured 16.08.2026: 60 strings × 8 languages take 8.3s, but a Pro-sized run (200k
# characters, 25 languages) took 202s on the same path — minutes, in one call.
#
# So the wait is bounded well inside any client's patience, and running out of it is not an
# error: it is an answer that carries the job id.
# ---------------------------------------------------------------------------
_MAX_WAIT = max(10, int(os.environ.get("KAERIS_MCP_MAX_WAIT", "50")))


def _await(client, job):
    """Wait for `job` within our own bound. Raises KaerisTimeout (carrying the id) if the
    server is still working — the caller turns that into an answer, not a failure."""
    return client.poll(job, max_wait=_MAX_WAIT)


def _still_running(job, extra=None):
    """The answer for a job that outlived our wait: everything needed to come back for it."""
    body = {
        "status": "running",
        "job_id": job,
        "note": (f"The translation is still running on the server after {_MAX_WAIT}s. It is "
                 "already submitted and paid for — do NOT start it again. Call the same tool "
                 f"with job_id=\"{job}\" (and the same file_path/out_dir) to collect the "
                 "result; repeat until it returns written files."),
    }
    body.update(extra or {})
    return body


def _client(api_key="", openrouter_key=""):
    return KaerisClient(
        api_url=os.environ.get("KAERIS_API_URL", "https://kaeris.dev"),
        api_key=api_key or os.environ.get("KAERIS_API_KEY"),
        openrouter_key=openrouter_key or os.environ.get("KAERIS_OPENROUTER_KEY"),
    )


@mcp.tool()
def kaeris_list_languages() -> dict:
    """List every target language KAERIS can translate into.

    Returns a mapping of language code → English name (e.g. {"es": "Spanish", ...}).
    Use the codes with kaeris_translate / kaeris_translate_file.
    """
    return _client().languages()


@mcp.tool()
def kaeris_scan_repo(path: str = ".") -> dict:
    """Discover a repo's i18n setup: which locale files exist, what the base
    (source) language and target languages are, and which framework/convention
    is in use. Purely local — reads the filesystem, makes no API call.

    Use this FIRST when asked to "look at/audit/fix this repo's i18n" and you
    don't already know the source file — it tells you where to point
    kaeris_status / kaeris_list_missing_keys / kaeris_add_language.

    Supports i18next/react-i18next (locales/<lang>/*.json), next-intl
    (messages/<lang>.json), vue-i18n (src/locales/<lang>.json), Flutter ARB
    (lib/l10n/*.arb), Android (res/values[-<lang>]/strings.xml), iOS
    (<lang>.lproj/Localizable.strings), gettext (locale/<lang>/LC_MESSAGES/*.po),
    Xcode String Catalogs (Localizable.xcstrings — one file holding every language,
    read out of the catalog itself), XLIFF (.xliff/.xlf), Java/Spring .properties,
    .NET RESX, Mozilla Fluent (.ftl), Markdown/MDX, plus plain
    <lang>.json/.yaml/.csv files anywhere else. Skips node_modules,
    .git, build/dist output and other noise; bounded so it's safe on large repos.

    Args:
        path: directory to scan (default: current directory).

    Returns:
        { "root": "<abs path scanned>",
          "locale_files": ["locales/en.json", "locales/de.json", ...],
          "base_lang": "en",            # best guess, or null if none found/ambiguous
          "target_langs": ["de", "fr"], # languages present other than base_lang
          "framework": "i18next",       # best guess, or null if unrecognized
          "file_count": 2 }
    """
    return scan_repo(path)


@mcp.tool()
def kaeris_status(source: str, langs: list = None, out: str = "") -> dict:
    """Completeness/health check for every target locale against a source file
    — the "i18n firewall". Works for EVERY supported format: JSON is checked fully
    offline; other formats (.arb/.strings/.po/.xml/.ftl/…) are parsed by the backend
    /api/parse endpoint (no translation, no cost).

    For each target language reports missing keys, extra/stale keys,
    placeholder mismatches (e.g. "Hello {name}" -> "Bonjour" loses {name}, or
    "{name}" -> "{nom}" which crashes str.format()/ICU at runtime), and the same
    quality detectors the CLI firewall gates a merge on: number drift
    ("Delete 5 files" -> "Delete 50 files"), lost inline tags, broken entities
    or escapes, missing ICU/CLDR plural categories. Those come back under
    "faults" (RED — they set ok=false) and "warnings" (YELLOW — advisory).
    Use this
    to find out what's broken before deciding whether to call
    kaeris_list_missing_keys (existing locale, some keys missing/broken) or
    kaeris_add_language (locale doesn't exist yet at all).

    Args:
        source: path to the base-language locale file, e.g. "locales/en.json" or
                "lib/l10n/app_en.arb" (typically kaeris_scan_repo's base_lang file).
        langs: target language codes to check, e.g. ["de", "fr"]. If omitted,
               falls back to kaeris.json's "langs" if present, else auto-discovers
               sibling <lang> files next to the source.
        out: directory containing the target locale files (default: source's own
             directory; each target is expected at "<out>/<lang><source-ext>",
             e.g. de.json for en.json, de.arb for en.arb).

    Returns:
        { "ok": bool, "missing": {lang: [keys]}, "extra": {lang: [keys]},
          "placeholder_issues": [{"lang","key","missing","added"}, ...],
          "faults": [{"lang","key","severity":"error","msg"}, ...],    # RED — ok is false
          "warnings": [{"lang","key"?,"severity":"warn","msg"}, ...],  # YELLOW — advisory
          "missing_files": [lang, ...],  # target language has no locale file at all
          "counts": {"missing": {lang: n}, "extra": {lang: n},
                     "placeholder_issues": n, "faults": n, "warnings": n},
          "note": "...",                 # present only when a list was shortened
          "source", "out_dir", "langs" }

    "ok", "counts" and "missing_files" are always exact and complete. The lists are EXAMPLES —
    at most 50 entries each — because a status call over a large repo otherwise returns a
    six-figure token bill in a single reply. Use kaeris_list_missing_keys (paged) for the full
    picture of one language.
    """
    # An exception raised here reaches the agent as a bare "Error executing tool
    # kaeris_status" (the SDK hides the text), so a typo in the path would read as an outage.
    # Answer with the reason instead.
    if not os.path.isfile(source):
        return {"ok": False, "error": f"no such file: {source} — check the path is relative "
                                      "to the directory the server runs in"}
    out_dir = _resolve_out_dir(source, out)
    target_langs = _resolve_langs(source, langs, out_dir)
    if not target_langs:
        return {"ok": False, "error": (
            "No target languages given and none could be discovered — pass langs=[...], "
            "add a kaeris.json with \"langs\", or place sibling <lang> files next to the source")}

    client = _client()                 # anonymous is fine — /api/parse needs no auth and costs nothing
    try:
        source_flat = _load_flat(source, client)
    except (OSError, ValueError, KaerisError) as e:
        return {"ok": False, "error": f"cannot read {source}: {e}"}
    ext = _source_ext(source)          # target locales share the source's format/extension

    # Same shape AND the same verdict as chk.check_locales, but in flat space so any parsed
    # format works, not only JSON. Until 03.08.2026 this ran the key/placeholder diff and
    # nothing else: a number drift ("Delete 5 files" -> "50 Dateien löschen") that the CLI
    # blocks a merge over came back as ok=true, so an agent asked to check a repo told its
    # human everything was fine. A false all-clear is worse than no tool at all.
    result = {"ok": True, "missing": {}, "extra": {}, "placeholder_issues": [],
              "missing_files": [], "faults": [], "warnings": []}
    for lang in target_langs:
        target_path = _target_locale_path(source, lang, out_dir, f"{lang}{ext}")
        try:
            target_flat = _load_flat(target_path, client)
        except (FileNotFoundError, ValueError, KaerisError):
            result["missing_files"].append(lang)
            result["missing"][lang] = sorted(source_flat)
            result["ok"] = False
            continue
        missing, extra, issues = chk.diff_locale(source_flat, target_flat)
        if missing:
            result["missing"][lang] = missing
            result["ok"] = False
        if extra:
            result["extra"][lang] = extra
        for issue in issues:
            result["placeholder_issues"].append({"lang": lang, **issue})
        if issues:
            result["ok"] = False

        # The detectors — number drift, lost inline tags, broken entities, ICU/CLDR plural
        # completeness, glossary terms. RED faults decide `ok`, exactly as in the CLI;
        # YELLOW warnings are advisory there and stay advisory here.
        for key, src_val in source_flat.items():
            tgt_val = target_flat.get(key)
            if not isinstance(src_val, str) or not isinstance(tgt_val, str):
                continue
            if src_val.strip() and not tgt_val.strip():
                result["warnings"].append({"lang": lang, "key": key, "severity": "warn",
                                           "msg": "empty translation — the source has text"})
                continue
            for f in det.string_faults(src_val, tgt_val, lang, None):
                rec = {"lang": lang, "key": key, "msg": f["msg"]}
                if f["severity"] == det.ERROR:
                    result["faults"].append({**rec, "severity": "error"})
                    result["ok"] = False
                else:
                    result["warnings"].append({**rec, "severity": "warn"})
        # File-level detectors read text: a locale legitimately holds numbers, booleans and
        # nulls, and joining those raises TypeError (the bug this same sweep found in the CLI).
        src_text = {k: v for k, v in source_flat.items() if isinstance(v, str)}
        tgt_text = {k: v for k, v in target_flat.items() if isinstance(v, str)}
        for f in det.file_faults(src_text, tgt_text, lang):
            result["warnings"].append({"lang": lang, "msg": f["msg"], "severity": "warn"})

    # Exact counts first, examples second. `ok`, the counts and `missing_files` are the whole
    # basis for a decision and are never cut; the lists behind them are examples, and a cut
    # list says so. (Before 16.08.2026 nothing was cut: 10 languages against a 2 MB source
    # meant a six-figure token bill for one status call — see the bounds block above.)
    result["counts"] = {
        "missing": {lang: len(keys) for lang, keys in result["missing"].items()},
        "extra": {lang: len(keys) for lang, keys in result["extra"].items()},
        "placeholder_issues": len(result["placeholder_issues"]),
        "faults": len(result["faults"]),
        "warnings": len(result["warnings"]),
    }
    shortened = False
    for section in ("missing", "extra"):
        for lang, keys in list(result[section].items()):
            if len(keys) > _STATUS_EXAMPLES:
                shortened = True
                result[section][lang] = keys[:_STATUS_EXAMPLES]
    for section in ("placeholder_issues", "faults", "warnings"):
        if len(result[section]) > _STATUS_EXAMPLES:
            shortened = True
            result[section] = result[section][:_STATUS_EXAMPLES]
    if shortened:
        result["note"] = (
            f"Lists are examples — at most {_STATUS_EXAMPLES} entries each; \"counts\" holds the "
            "exact totals. For the full list of one language use kaeris_list_missing_keys "
            "(it pages), or let kaeris_translate_file(only_new=True) fill the gaps.")

    result["source"] = source
    result["out_dir"] = out_dir
    result["langs"] = target_langs
    return result


@mcp.tool()
def kaeris_list_missing_keys(source: str, lang: str, out: str = "",
                             limit: int = _PAGE_DEFAULT, offset: int = 0) -> dict:
    """The exact keys missing or placeholder-broken in ONE target locale, with
    their source-language text, so an agent knows exactly what to translate/fix.
    Every supported format works (JSON offline; others via the backend /api/parse,
    no cost) — use kaeris_add_language or kaeris_translate to actually fill them in.

    The answer is PAGED: "missing_count" is always the exact total, while "missing" /
    "missing_values" carry one page of it (default 200 entries, and never more than about
    15k tokens' worth). A whole locale file returned in one tool result is not a long answer,
    it is the end of the agent's session — measured 16.08.2026: a 2 MB source file, the size
    the product accepts, produced 667 000 tokens in a single reply. When there is a next page,
    "page.next_offset" says where it starts.

    Args:
        source: path to the base-language locale file, e.g. "locales/en.json" or "app_en.arb".
        lang: the target language code to inspect, e.g. "de".
        out: directory containing the target locale file (default: source's own
             directory; expected at "<out>/<lang><source-ext>", e.g. de.json / de.arb).
        limit: how many missing keys to return in this call (default 200, max 2000).
        offset: where to start — pass "page.next_offset" from the previous call.

    Returns:
        { "lang": "de", "missing_file": bool,
          "missing_count": 1234,                       # ALWAYS the exact total
          "missing": ["<dotted.key>", ...],            # one page of them
          "missing_values": {"<dotted.key>": "<source text>", ...},   # the same page
          "page": {"total", "returned", "offset", "complete", "next_offset"?, "note"?},
          "extra": ["<dotted.key>", ...], "extra_count": 12,
          "placeholder_issues": [{"key","missing","added"}, ...], "placeholder_issue_count": 3 }
    """
    out_dir = _resolve_out_dir(source, out)
    client = _client()                 # anonymous — /api/parse needs no auth and costs nothing
    source_flat = _load_flat(source, client)
    ext = _source_ext(source)          # target locale shares the source's format/extension

    target_path = _target_locale_path(source, lang, out_dir, f"{lang}{ext}")
    if not os.path.isfile(target_path):
        keys = sorted(source_flat)
        page, info = _page_keys(keys, source_flat, offset, limit)
        return {
            "lang": lang,
            "missing_file": True,
            "missing_count": len(keys),
            "missing": page,
            "missing_values": {k: source_flat[k] for k in page},
            "page": info,
            "extra": [], "extra_count": 0,
            "placeholder_issues": [], "placeholder_issue_count": 0,
        }

    target_flat = _load_flat(target_path, client)
    missing, extra, issues = chk.diff_locale(source_flat, target_flat)
    page, info = _page_keys(missing, source_flat, offset, limit)
    extra_page, extra_total = _cap(extra, _PAGE_MAX)
    issue_page, issue_total = _cap(issues, _STATUS_EXAMPLES)
    return {
        "lang": lang,
        "missing_file": False,
        "missing_count": len(missing),
        "missing": page,
        "missing_values": {k: source_flat[k] for k in page},
        "page": info,
        "extra": extra_page,
        "extra_count": extra_total,
        "placeholder_issues": issue_page,
        "placeholder_issue_count": issue_total,
    }


@mcp.tool()
def kaeris_add_language(
    source: str,
    lang: str,
    out: str = "",
    tone: str = "",
    icu: bool = False,
    keep: list = None,
    context: str = "",
    api_key: str = "",
    openrouter_key: str = "",
    job_id: str = "",
) -> dict:
    """Bootstrap a BRAND-NEW target locale by translating the entire source file
    into it and writing the result. Use this when kaeris_scan_repo/kaeris_status
    show a language with no locale file at all yet (in "missing_files").

    This CALLS THE TRANSLATION API (same as kaeris_translate_file) — unlike
    kaeris_scan_repo/kaeris_status/kaeris_list_missing_keys, which are local-only
    and free. For an EXISTING locale that's just missing a few keys, prefer
    kaeris_translate_file(..., only_new=True) so you don't re-translate the whole file.

    Args:
        source: path to the base-language locale file, e.g. "locales/en.json".
        lang: the new target language code to create, e.g. "de".
        out: output directory (default: source file's own directory).
        tone: "formal" or "casual" to steer register; "" (default) is neutral.
        icu: True if the source uses ICU MessageFormat (plurals/select), so the
             model preserves the syntax instead of translating it.
        keep: optional list of terms to NEVER translate (brand/product names).
        context: one line about what the app IS, e.g. "a mobile bank for teenagers" or
              "a wildlife documentary app". The model uses it to pick the right sense of
              ambiguous strings — "Bank" becomes Ufer in the second and Bank in the first.
              You are reading the repo, so you know this: pass it. Max 300 chars.
        api_key / openrouter_key: optional auth overrides. PREFER the KAERIS_API_KEY /
              KAERIS_OPENROUTER_KEY environment variables: a key passed as a tool argument is
              written into the conversation transcript and into the MCP client's logs, where
              it outlives this task.
        job_id: leave empty to start a translation. If a previous call came back with
              {"status": "running", "job_id": ...}, pass that id (with the same source/out) to
              collect the finished result instead of paying for the work twice.

    Returns:
        { "written": ["<out>/<lang>.json"], "lang": "de", "languages": ["de"] }
        or, if the server is still working: { "status": "running", "job_id": "...", "note": ... }
    """
    if not os.path.isfile(source):
        raise FileNotFoundError(
            f"source file not found: {source} — this is the base-language file, e.g. locales/en.json"
        )
    if not lang or not lang.strip():
        raise ValueError("`lang` is required")

    out_dir = _resolve_out_dir(source, out)
    os.makedirs(out_dir, exist_ok=True)
    fname = os.path.basename(source)
    client = _client(api_key, openrouter_key)

    with open(source, "rb") as f:
        content = f.read()
    job = job_id.strip()
    try:
        if not job:
            job = client.submit(fname, content, [lang], keep or [], tone=tone, icu=icu,
                                app_context=context)
        status = _await(client, job)
        members = client.download(job)
    except KaerisTimeout as e:
        return _still_running(e.job_id, {"lang": lang})
    except KaerisError as e:
        raise RuntimeError(str(e))

    if lang in (status.get("failed_langs") or []):
        raise RuntimeError(f"Translation for '{lang}' failed and returned source text — not written")

    # Every destination is resolved (and checked) BEFORE anything is opened for writing: a
    # refusal must not be half-applied, leaving one file on disk and an exception in the agent's
    # lap. Caught by the test for this guard, not by reading the code.
    plan = [(_target_locale_path(source, lang, out_dir, name), data)
            for name, data in members.items()]
    written = []
    for dest, data in plan:
        os.makedirs(os.path.dirname(dest) or out_dir, exist_ok=True)
        with open(dest, "wb") as f:
            f.write(data)
        written.append(dest)
    return {"written": written, "lang": lang, "languages": [lang]}


@mcp.tool()
def kaeris_translate(
    strings: dict,
    target_languages: list,
    keep: list = None,
    verify: bool = False,
    back_lang: str = "en",
    tone: str = "",
    icu: bool = False,
    reuse: dict = None,
    context: str = "",
    api_key: str = "",
    openrouter_key: str = "",
    job_id: str = "",
) -> dict:
    """Translate a set of UI strings into one or more languages.

    Format-aware and placeholder-safe: values like "Hello, {name}" keep their
    placeholders intact. Non-string values are passed through unchanged.

    Includes Translation QA so you can trust the output: it flags translations that
    dropped a placeholder or that grew long enough to risk overflowing the UI. With
    verify=True it also back-translates each result into back_lang so you can check the
    meaning is right even in a language you can't read.

    Args:
        strings: key → source-text pairs, e.g. {"greeting": "Hello", "save": "Save"}.
        target_languages: language codes from kaeris_list_languages, e.g. ["es", "fr", "ja"].
        keep: optional list of terms to NEVER translate — kept verbatim in every language
              (brand/product names), e.g. ["KAERIS", "GitHub"].
        context: one line about what the app IS, e.g. "a mobile bank for teenagers" or
              "a wildlife documentary app". The model uses it to pick the right sense of
              ambiguous strings — "Bank" becomes Ufer in the second and Bank in the first.
              You are reading the repo, so you know this: pass it. Max 300 chars.
        verify: back-translate each result into back_lang to confirm meaning (uses more tokens).
        back_lang: language for the verify back-translation (default "en").
        tone: "formal" or "casual" to steer register; "" (default) is neutral.
        icu: True if values may contain ICU MessageFormat (plurals/select) so the
             model preserves the syntax instead of translating it.
        reuse: optional {lang: {key: previous_translation}} translation-memory map —
               strings that are unchanged from a prior run are reused verbatim
               server-side and only new/changed strings are actually translated.
        api_key / openrouter_key: optional auth overrides. PREFER the KAERIS_API_KEY /
              KAERIS_OPENROUTER_KEY environment variables: a key passed as a tool argument is
              written into the conversation transcript and into the MCP client's logs, where
              it outlives this task.

    Returns:
        { "translations": { "<lang>": { "<key>": "<translation>", ... }, ... },
          "placeholder_warnings": { "<lang>": { "<key>": ["{name}", ...] } },  # if any placeholders were lost
          "overflow_warnings":   { "<lang>": [ {"key","src","tr","pct"}, ... ] },  # if UI-overflow risk
          "back_translations":   { "<lang>": { "<key>": "<back-translation>" } },  # only if verify=True
          "failed_languages": [...]  # present only if some languages could not be translated
        }
    """
    if not strings:
        raise ValueError("`strings` is empty")
    if not target_languages:
        raise ValueError("`target_languages` is empty")

    client = _client(api_key, openrouter_key)
    content = json.dumps(strings, ensure_ascii=False).encode()
    job = (job_id or "").strip()
    try:
        if not job:
            job = client.submit("strings.json", content, target_languages, keep or [],
                                verify=verify, back_lang=back_lang,
                                tone=tone, icu=icu, reuse=reuse, app_context=context)
        status = _await(client, job)
        members = client.download(job)
    except KaerisTimeout as e:
        return _still_running(e.job_id, {"languages": target_languages})
    except KaerisError as e:
        raise RuntimeError(str(e))

    failed = status.get("failed_langs") or []
    translations = {}
    # ZIP members are named "<lang>.json"; map each back to its language
    for lang in target_languages:
        member = members.get(f"{lang}.json")
        if member is None:
            continue
        try:
            translations[lang] = json.loads(member.decode("utf-8"))
        except Exception:
            continue
    result = {"translations": translations}

    # Translation QA — placeholder loss + UI-overflow (free), back-translation if verify.
    try:
        prev = client.preview(job)
        warnings = prev.get("_warnings") or {}
        overflow = {l: v["overflow"] for l, v in (prev.get("_qa") or {}).items() if v.get("overflow")}
        back = prev.get("_back") or {}
        if warnings:
            result["placeholder_warnings"] = warnings
        if overflow:
            result["overflow_warnings"] = overflow
        if verify and back:
            result["back_translations"] = back
    except KaerisError:
        pass  # QA is best-effort — never fail the translation over it

    if failed:
        # These came back as source text, not a real translation — flag it, don't hide it.
        result["failed_languages"] = failed
        result["warning"] = (f"{len(failed)} language(s) could not be translated and returned "
                             f"source text: {', '.join(failed)}")
    return result


@mcp.tool()
def kaeris_translate_file(
    file_path: str,
    target_languages: list,
    out_dir: str = "",
    only_new: bool = False,
    keep: list = None,
    verify: bool = False,
    back_lang: str = "en",
    tone: str = "",
    icu: bool = False,
    reuse: dict = None,
    context: str = "",
    api_key: str = "",
    openrouter_key: str = "",
    job_id: str = "",
) -> dict:
    """Translate a strings file on disk and write the results next to it.

    Supports JSON, YAML, .strings, Xcode String Catalogs (.xcstrings), .po, ARB,
    Android XML, CSV, XLIFF 1.2, Java/Spring .properties, .NET RESX, Mozilla Fluent
    (.ftl) and Markdown/MDX — every format the API accepts. With only_new=True
    (JSON), translates just the keys missing from existing target files and merges.

    Args:
        file_path: path to the source file, e.g. "locales/en.json".
        target_languages: language codes, e.g. ["de", "uk", "zh"].
        out_dir: where to write outputs (default: the source file's directory).
        only_new: reproducible incremental mode — translate only new AND edited keys, tracked via
                  kaeris.lock (a tone/ICU/glossary change re-translates all); unchanged stays
                  stable. JSON and ARB only — for any other format the whole file is translated
                  (and charged), and the result says so in "note".
        keep: optional list of terms to NEVER translate — kept verbatim in every language
              (brand/product names), e.g. ["KAERIS", "GitHub"].
        context: one line about what the app IS, e.g. "a mobile bank for teenagers" or
              "a wildlife documentary app". The model uses it to pick the right sense of
              ambiguous strings — "Bank" becomes Ufer in the second and Bank in the first.
              You are reading the repo, so you know this: pass it. Max 300 chars.
        verify: back-translate each result into back_lang to confirm meaning (non-incremental only).
        back_lang: language for the verify back-translation (default "en").
        tone: "formal" or "casual" to steer register; "" (default) is neutral.
        icu: True if values may contain ICU MessageFormat (plurals/select) so the
             model preserves the syntax instead of translating it.
        reuse: optional {lang: {key: previous_translation}} translation-memory map —
               strings unchanged from a prior run are reused verbatim server-side
               (non-incremental only; only_new does its own client-side diffing).
        api_key / openrouter_key: optional auth overrides.

    Returns:
        { "written": ["locales/de.json", ...], "languages": [...] }
    """
    if not os.path.isfile(file_path):
        raise FileNotFoundError(file_path)
    if not target_languages:
        raise ValueError("`target_languages` is empty")

    out = out_dir or os.path.dirname(os.path.abspath(file_path))
    os.makedirs(out, exist_ok=True)
    fname = os.path.basename(file_path)
    client = _client(api_key, openrouter_key)

    only_new_note = ""
    if only_new:
        if fname.lower().endswith((".json", ".arb")):
            return _translate_file_incremental(client, file_path, out, target_languages, keep or [],
                                               tone=tone, icu=icu, context=context)
        # Silence here billed the client for the WHOLE file after they asked for the delta —
        # and the agent reported plain success. The CLI says this out loud; so do we.
        only_new_note = (
            f"only_new is supported for JSON and ARB only — the whole "
            f"{os.path.splitext(fname)[1] or 'file'} was translated and charged, not just the "
            f"new keys.")

    with open(file_path, "rb") as f:
        content = f.read()
    job = (job_id or "").strip()
    try:
        if not job:
            job = client.submit(fname, content, target_languages, keep or [],
                                verify=verify, back_lang=back_lang,
                                tone=tone, icu=icu, reuse=reuse, app_context=context)
        status = _await(client, job)
        members = client.download(job)
    except KaerisTimeout as e:
        return _still_running(e.job_id, {"languages": target_languages})
    except KaerisError as e:
        raise RuntimeError(str(e))

    # Resolve (and check) every destination first — see kaeris_add_language.
    plan = [(_target_locale_path(file_path, os.path.splitext(os.path.basename(name))[0],
                                 out, name), data)
            for name, data in members.items()]
    written = []
    for dest, data in plan:
        os.makedirs(os.path.dirname(dest) or out, exist_ok=True)
        with open(dest, "wb") as f:
            f.write(data)
        written.append(dest)
    result = {"written": written, "languages": target_languages}
    if only_new_note:
        result["note"] = only_new_note
    failed = status.get("failed_langs") or []
    if failed:
        result["failed_languages"] = failed
        result["warning"] = f"{len(failed)} language(s) returned source text (not translated): {', '.join(failed)}"
    return result


def _arb_meta_key(flat_key):
    """True if a flattened key belongs to ARB non-translatable metadata: its FIRST dotted
    segment starts with '@' (`@@locale`, `@foo`, `@foo.placeholders...`). Never sent to the
    model — same rule as the CLI."""
    return flat_key.split(".", 1)[0].startswith("@")


def _translate_file_incremental(client, path, out, languages, keep=None, tone="", icu=False,
                                context=""):
    """Delta translation with the SAME rules as `kaeris translate --only-new`.

    This used to be a simplified copy of the CLI's incremental path, and the copy fell three
    fixes behind (all three reproduced on 16.08.2026 before this rewrite):
      * it wrote to a flat `<out>/<lang>.json`, ignoring the layout every other tool mirrors —
        on an i18next repo (locales/en/translation.json) a COMPLETE, up-to-date German locale
        was never found, so every run resent the whole file (the client pays for all of it) and
        the result landed in locales/en/de.json, where nothing reads it;
      * it detected against ONE global baseline, so translating German today marked an edited
        English string as done for French too — French kept the old meaning forever — and
        writing the lock without per-language baselines ERASED the ones the CLI had recorded;
      * it had no unverified-keys guard: translations that exist with nothing in the lock
        vouching for them were stamped as current, which makes the loss permanent.
    """
    is_arb = path.lower().endswith(".arb")
    source_obj = inc.load_json(path)
    source_flat = inc.flatten(source_obj)
    if is_arb:
        source_flat = {k: v for k, v in source_flat.items() if not _arb_meta_key(k)}
    flat_style = inc.is_flat(source_obj)
    source_hashes = inc.hash_flat(source_flat)
    ext = _source_ext(path)

    # kaeris.lock records each source string's hash plus the run settings, so an EDITED source
    # value or a tone/ICU/glossary change triggers (re)translation while unchanged strings are
    # reused verbatim. (Plain missing_keys used to silently keep a stale translation when the
    # English text was edited.)
    lock_path = inc.default_lock_path(path)
    lock_raw = inc.load_lock(lock_path)
    detect_lock = inc.lock_keys(lock_raw)      # global map: "reached every language"
    lang_locks = inc.lock_langs(lock_raw)      # per-language baselines
    cur_settings = inc.settings_signature(tone, icu, keep or [], _current_model(client), context)
    locked_settings = inc.lock_settings(lock_raw)
    settings_changed = inc.settings_changed(locked_settings, cur_settings)

    written, skipped, failed, pending = [], [], [], []
    broken = set()
    broken_by_lang = {}
    unverified_by_lang = {}

    for lang in languages:
        target_path = _target_locale_path(path, lang, out, f"{lang}{ext}")
        os.makedirs(os.path.dirname(target_path) or ".", exist_ok=True)
        existing = inc.load_json(target_path) if os.path.isfile(target_path) else {}
        existing_flat = inc.flatten(existing) if existing else {}
        # Detect against THIS language's baseline: a run that touched only German must not
        # mark an edited string as done for French.
        lang_baseline = inc.lock_keys(lock_raw, lang)
        if not settings_changed:
            unverified_by_lang[lang] = inc.unverified_keys(source_flat, existing_flat, lang_baseline)
        todo = (dict(source_flat) if settings_changed
                else inc.changed_or_missing_keys(source_flat, existing_flat, lang_baseline))
        if not todo:
            skipped.append(lang)
            continue
        subset = inc.build_subset(source_obj, set(todo), flat_style)
        content = json.dumps(subset, ensure_ascii=False).encode()
        try:
            job = client.submit(os.path.basename(path), content, [lang], keep or [],
                                tone=tone, icu=icu, app_context=context)
            status = _await(client, job)
            members = client.download(job)
        except KaerisTimeout as e:
            # This language is still being translated. Nothing is merged, and its keys stay
            # stale-locked below, so the next only_new run picks exactly them up again.
            pending.append({"lang": lang, "job_id": e.job_id})
            broken_by_lang[lang] = set(todo)
            continue
        except KaerisError as e:
            raise RuntimeError(str(e))
        # Don't merge a failed (source-fallback) result — it would poison existing translations
        if lang in (status.get("failed_langs") or []):
            failed.append(lang)
            broken_by_lang[lang] = set(todo)   # not merged — keep these keys stale-locked
            continue
        translated = None
        for name, data in members.items():
            if name.lower().endswith((".json", ".arb")):
                translated = json.loads(data.decode("utf-8"))
                break
        if translated is None:
            broken_by_lang[lang] = set(todo)
            continue
        merged = inc.merge_translation(existing, translated)
        if is_arb:
            merged["@@locale"] = lang       # stamp the target locale on the written ARB
        inc.dump_json(merged, target_path)
        written.append(target_path)

    # Advance the lock exactly once, AFTER every language. Per-language baselines record what
    # each language actually took, so a later run for a DIFFERENT language still sees the edit
    # as pending; the global map keeps its old meaning ("current in EVERY language we know
    # about") so an older client reading this lock is not told a key is done while one lags.
    unverified_all = set()
    for keys in unverified_by_lang.values():
        unverified_all |= keys
    for lang in languages:
        base = dict(lang_locks.get(lang) or inc.lock_keys(lock_raw, lang))
        skip = broken_by_lang.get(lang, set()) | unverified_by_lang.get(lang, set())
        for k, h in source_hashes.items():
            if k not in skip:
                base[k] = h
        lang_locks[lang] = {k: v for k, v in base.items() if k in source_hashes}
    # Never record a hash for something nobody checked: that is what made the loss permanent.
    broken |= unverified_all
    for keys in broken_by_lang.values():
        broken |= keys

    new_keys = dict(detect_lock)
    for k in source_hashes:
        if k in broken:
            continue
        if all(m.get(k) == source_hashes[k] for m in lang_locks.values()):
            new_keys[k] = source_hashes[k]
        else:
            new_keys.pop(k, None)
    # Keep the OLD settings if a settings change couldn't be fully applied, so the next run
    # re-forces the stragglers.
    final_settings = locked_settings if (settings_changed and broken) else cur_settings
    inc.dump_lock(inc.build_lock(new_keys, final_settings, lang_locks), lock_path)

    result = {"written": written, "up_to_date": skipped, "languages": languages}
    if pending:
        result["still_running"] = pending
        result["pending_note"] = (
            f"{len(pending)} language(s) were still being translated after {_MAX_WAIT}s and "
            "were NOT merged. Their keys stay pending in kaeris.lock — run the same call again "
            "and they will be picked up. (Those runs are on the server; a repeat translates "
            "the same keys again.)")
    if failed:
        result["failed_languages"] = failed
        result["warning"] = f"{len(failed)} language(s) could not be translated and were skipped: {', '.join(failed)}"
    if unverified_all:
        # Say it out loud: the agent is the one deciding what to do next, and "left alone" is
        # only honest if it is visible.
        result["unverified_keys"] = sorted(unverified_all)
        result["unverified_note"] = (
            f"{len(unverified_all)} key(s) already exist in the target locale(s) but nothing in "
            "kaeris.lock vouches for them, so they were NOT translated and NOT recorded as "
            "current. If those translations are known good, translate the file once without "
            "only_new to establish a baseline.")
    return result


_HELP = f"""kaeris-mcp {__version__} - KAERIS i18n MCP server (stdio)

This program is not run by hand: an MCP client (Claude Desktop, Cursor, Claude Code) starts it
and talks to it over stdin/stdout. Add it to your client instead:

  claude mcp add kaeris-i18n kaeris-mcp
  or in the client config: {{"mcpServers": {{"kaeris-i18n": {{"command": "kaeris-mcp"}}}}}}

Optional key for paid plans: env KAERIS_API_KEY. Docs: https://kaeris.dev/mcp-localization.html
"""


def main():
    # stdout is the protocol channel: print there only when asked, and then do not serve.
    args = sys.argv[1:]
    if any(a in ("-h", "--help") for a in args):
        print(_HELP)
        return
    if "--version" in args:
        print(f"kaeris-mcp {__version__}")
        return
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
