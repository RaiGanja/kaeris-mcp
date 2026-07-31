"""KAERIS i18n MCP server.

Exposes AI-native localization tools over the Model Context Protocol so agents
(Claude Desktop, Cursor, Claude Code, …) can translate app strings on demand.

Auth (optional — the free anonymous tier works up to 10k chars):
  KAERIS_API_KEY         Pro/Scale key (kaerisp_…)
  KAERIS_OPENROUTER_KEY  OpenRouter key for Lifetime/BYOK
  KAERIS_API_URL         override the API base URL (default https://kaeris.dev)
"""

import json
import os
import re

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # MCP SDK >= 2.0 renamed FastMCP to MCPServer and moved it
    # Same decorator/run surface (.tool(), .run(), .list_tools(), .call_tool()), so the
    # rest of this file is version-agnostic. Without this, `pip install kaeris-mcp` in a
    # fresh environment resolves to the newest SDK and dies on this import.
    from mcp.server.mcpserver import MCPServer as FastMCP

from kaeris.client import KaerisClient, KaerisError
from kaeris import check as chk
from kaeris import incremental as inc

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
_LOCALE_EXTS = {".json", ".yaml", ".yml", ".arb", ".strings", ".po", ".xml", ".csv",
                ".xliff", ".xlf", ".properties", ".resx", ".ftl"}
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

    found = []
    for dirpath, name in _walk_bounded(root):
        info = _classify_locale_file(root, dirpath, name)
        if info:
            found.append(info)

    langs_present = sorted({f["lang"] for f in found})
    base_lang = _guess_base_lang(langs_present)
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

    return os.path.join(out_dir, member_name)


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
                with open(path, encoding="utf-8") as f:
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
    XLIFF (.xliff/.xlf), Java/Spring .properties, .NET RESX,
    plus plain <lang>.json/.yaml/.csv files anywhere else. Skips node_modules,
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

    For each target language reports missing keys, extra/stale keys, and
    placeholder mismatches (e.g. "Hello {name}" -> "Bonjour" loses {name}, or
    "{name}" -> "{nom}" which crashes str.format()/ICU at runtime). Use this
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
          "missing_files": [lang, ...],  # target language has no locale file at all
          "source", "out_dir", "langs" }
    """
    out_dir = _resolve_out_dir(source, out)
    target_langs = _resolve_langs(source, langs, out_dir)
    if not target_langs:
        raise ValueError(
            "No target languages given and none could be discovered — pass langs=[...], "
            "add a kaeris.json with \"langs\", or place sibling <lang> files next to the source"
        )

    client = _client()                 # anonymous is fine — /api/parse needs no auth and costs nothing
    source_flat = _load_flat(source, client)
    ext = _source_ext(source)          # target locales share the source's format/extension

    # Same shape as chk.check_locales, but in flat space so any parsed format works, not only JSON.
    result = {"ok": True, "missing": {}, "extra": {}, "placeholder_issues": [], "missing_files": []}
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

    result["source"] = source
    result["out_dir"] = out_dir
    result["langs"] = target_langs
    return result


@mcp.tool()
def kaeris_list_missing_keys(source: str, lang: str, out: str = "") -> dict:
    """The exact keys missing or placeholder-broken in ONE target locale, with
    their source-language text, so an agent knows exactly what to translate/fix.
    Every supported format works (JSON offline; others via the backend /api/parse,
    no cost) — use kaeris_add_language or kaeris_translate to actually fill them in.

    Args:
        source: path to the base-language locale file, e.g. "locales/en.json" or "app_en.arb".
        lang: the target language code to inspect, e.g. "de".
        out: directory containing the target locale file (default: source's own
             directory; expected at "<out>/<lang><source-ext>", e.g. de.json / de.arb).

    Returns:
        { "lang": "de", "missing_file": bool,
          "missing": ["<dotted.key>", ...],           # present in source, absent in target
          "missing_values": {"<dotted.key>": "<source text>", ...},
          "extra": ["<dotted.key>", ...],              # present in target, absent in source
          "placeholder_issues": [{"key","missing","added"}, ...] }
    """
    out_dir = _resolve_out_dir(source, out)
    client = _client()                 # anonymous — /api/parse needs no auth and costs nothing
    source_flat = _load_flat(source, client)
    ext = _source_ext(source)          # target locale shares the source's format/extension

    target_path = _target_locale_path(source, lang, out_dir, f"{lang}{ext}")
    if not os.path.isfile(target_path):
        return {
            "lang": lang,
            "missing_file": True,
            "missing": sorted(source_flat),
            "missing_values": dict(source_flat),
            "extra": [],
            "placeholder_issues": [],
        }

    target_flat = _load_flat(target_path, client)
    missing, extra, issues = chk.diff_locale(source_flat, target_flat)
    return {
        "lang": lang,
        "missing_file": False,
        "missing": missing,
        "missing_values": {k: source_flat[k] for k in missing},
        "extra": extra,
        "placeholder_issues": issues,
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
        api_key / openrouter_key: optional auth overrides (else free tier or env vars).

    Returns:
        { "written": ["<out>/<lang>.json"], "lang": "de", "languages": ["de"] }
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
    try:
        job = client.submit(fname, content, [lang], keep or [], tone=tone, icu=icu,
                            app_context=context)
        status = client.poll(job)
        members = client.download(job)
    except KaerisError as e:
        raise RuntimeError(str(e))

    if lang in (status.get("failed_langs") or []):
        raise RuntimeError(f"Translation for '{lang}' failed and returned source text — not written")

    written = []
    for name, data in members.items():
        dest = _target_locale_path(source, lang, out_dir, name)
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
        api_key: optional KAERIS key (else uses the free tier or KAERIS_API_KEY env).
        openrouter_key: optional OpenRouter key for Lifetime/BYOK.

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
    try:
        job = client.submit("strings.json", content, target_languages, keep or [],
                            verify=verify, back_lang=back_lang,
                            tone=tone, icu=icu, reuse=reuse, app_context=context)
        status = client.poll(job)
        members = client.download(job)
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
) -> dict:
    """Translate a strings file on disk and write the results next to it.

    Supports JSON, YAML, .strings, .po, ARB, Android XML, CSV, XLIFF 1.2,
    Java/Spring .properties, .NET RESX and Mozilla Fluent (.ftl). With only_new=True
    (JSON), translates just the keys missing from existing target files and merges.

    Args:
        file_path: path to the source file, e.g. "locales/en.json".
        target_languages: language codes, e.g. ["de", "uk", "zh"].
        out_dir: where to write outputs (default: the source file's directory).
        only_new: reproducible incremental mode — translate only new AND edited keys, tracked via
                  kaeris.lock (a tone/ICU/glossary change re-translates all); unchanged stays stable. JSON only.
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

    if only_new and fname.lower().endswith(".json"):
        return _translate_file_incremental(client, file_path, out, target_languages, keep or [],
                                           tone=tone, icu=icu, context=context)

    with open(file_path, "rb") as f:
        content = f.read()
    try:
        job = client.submit(fname, content, target_languages, keep or [],
                            verify=verify, back_lang=back_lang,
                            tone=tone, icu=icu, reuse=reuse, app_context=context)
        status = client.poll(job)
        members = client.download(job)
    except KaerisError as e:
        raise RuntimeError(str(e))

    written = []
    for name, data in members.items():
        member_stem = os.path.splitext(os.path.basename(name))[0]
        dest = _target_locale_path(file_path, member_stem, out, name)
        os.makedirs(os.path.dirname(dest) or out, exist_ok=True)
        with open(dest, "wb") as f:
            f.write(data)
        written.append(dest)
    result = {"written": written, "languages": target_languages}
    failed = status.get("failed_langs") or []
    if failed:
        result["failed_languages"] = failed
        result["warning"] = f"{len(failed)} language(s) returned source text (not translated): {', '.join(failed)}"
    return result


def _translate_file_incremental(client, path, out, languages, keep=None, tone="", icu=False,
                                context=""):
    source_obj = inc.load_json(path)
    source_flat = inc.flatten(source_obj)
    flat_style = inc.is_flat(source_obj)
    source_hashes = inc.hash_flat(source_flat)

    # Reproducible incremental — parity with the CLI. kaeris.lock records each source string's
    # hash plus the run settings, so an EDITED source value or a tone/ICU/glossary change triggers
    # (re)translation while unchanged strings are reused verbatim. (Plain missing_keys used to
    # silently keep a stale translation when the English text was edited.)
    lock_path = inc.default_lock_path(path)
    lock_raw = inc.load_lock(lock_path)
    detect_lock = inc.lock_keys(lock_raw)
    cur_settings = inc.settings_signature(tone, icu, keep or [], _current_model(client), context)
    locked_settings = inc.lock_settings(lock_raw)
    settings_changed = inc.settings_changed(locked_settings, cur_settings)

    written, skipped, failed = [], [], []
    broken = set()

    for lang in languages:
        target_path = os.path.join(out, f"{lang}.json")
        existing = inc.load_json(target_path) if os.path.isfile(target_path) else {}
        existing_flat = inc.flatten(existing) if existing else {}
        todo = (dict(source_flat) if settings_changed
                else inc.changed_or_missing_keys(source_flat, existing_flat, detect_lock))
        if not todo:
            skipped.append(lang)
            continue
        subset = inc.build_subset(source_obj, set(todo), flat_style)
        content = json.dumps(subset, ensure_ascii=False).encode()
        try:
            job = client.submit(os.path.basename(path), content, [lang], keep or [],
                                tone=tone, icu=icu, app_context=context)
            status = client.poll(job)
            members = client.download(job)
        except KaerisError as e:
            raise RuntimeError(str(e))
        # Don't merge a failed (source-fallback) result — it would poison existing translations
        if lang in (status.get("failed_langs") or []):
            failed.append(lang)
            broken.update(todo)   # not merged — keep these keys stale-locked for a retry
            continue
        translated = None
        for name, data in members.items():
            if name.endswith(".json"):
                translated = json.loads(data.decode("utf-8"))
                break
        if translated is None:
            broken.update(todo)
            continue
        merged = inc.merge_translation(existing, translated)
        inc.dump_json(merged, target_path)
        written.append(target_path)

    # Advance the lock once, after every language: record a key's current hash unless it was left
    # broken; keep the old settings if a settings change couldn't be fully applied (so the next
    # run re-forces the stragglers).
    new_keys = dict(detect_lock)
    for k in source_hashes:
        if k not in broken:
            new_keys[k] = source_hashes[k]
    final_settings = locked_settings if (settings_changed and broken) else cur_settings
    inc.dump_lock(inc.build_lock(new_keys, final_settings), lock_path)

    result = {"written": written, "up_to_date": skipped, "languages": languages}
    if failed:
        result["failed_languages"] = failed
        result["warning"] = f"{len(failed)} language(s) could not be translated and were skipped: {', '.join(failed)}"
    return result


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
