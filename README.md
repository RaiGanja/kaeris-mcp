<img src="https://kaeris.dev/icon-512.png" alt="KAERIS" width="72" align="left" style="margin-right:16px" />

# KAERIS i18n — MCP Server

AI-native localization over the **Model Context Protocol**. Give Claude Desktop, Cursor,
Claude Code (or any MCP client) the ability to translate your app's strings into **46
languages** — placeholder-safe, format-aware, incremental, with built-in Translation QA.

## Tools

| Tool | What it does | Calls the API? |
|------|--------------|:---:|
| `kaeris_scan_repo` | Discover a repo's i18n setup: locale files found, base language, target languages, framework guess (i18next/next-intl/vue-i18n/Flutter/Android/iOS/gettext/generic) | No |
| `kaeris_status` | Completeness/health per target locale — missing keys, extra keys, placeholder mismatches, plus the quality detectors that gate a merge in CI: number drift, lost inline tags, broken entities/escapes, ICU/CLDR plural gaps (same verdict as `kaeris check --json`) | No |
| `kaeris_list_missing_keys` | The exact missing/broken keys (with source text) for one target locale, so an agent knows exactly what to fix | No |
| `kaeris_list_languages` | List all supported target languages | Yes, free (no quota) |
| `kaeris_translate` | Translate inline strings → per-language results, with QA (placeholder-loss & UI-overflow flags; `verify=True` back-translates to check meaning) | Yes |
| `kaeris_translate_file` | Translate a file on disk (JSON/YAML/.strings/.xcstrings/.po/ARB/XML/CSV/XLIFF/.properties/.resx/.ftl/Markdown), optional incremental — reproducible via `kaeris.lock` | Yes |
| `kaeris_add_language` | Bootstrap a brand-new target locale by translating the whole source file into it | Yes |

The first three tools are local-only (no network call, no cost), and `kaeris_list_languages` is one
free API call that counts toward no limit — an agent can use all four freely to audit and
understand a repo's i18n before deciding what (if anything) to translate.

## Reproducible by design

`kaeris_translate_file` with `only_new=True` keeps a `kaeris.lock` next to your source file —
the same lock the CLI writes, so an agent and a human sharing a repo stay in sync. It records a
hash of every source string **plus the settings that produced it**: tone, glossary, app context, and the
model. That means:

- **Edit one string** — only that string is re-translated; everything else stays byte-for-byte.
- **Change tone, glossary or context** — the whole locale is re-translated, never a mix of old and new.
- **Change plan** — every tier runs the same model (Gemini 2.5 Flash-Lite), so upgrading does not force a
  re-translation. The lock records the model regardless: the day we change it, the locale is
  rebuilt in full instead of quietly ending up the work of two models.

Commit `kaeris.lock` alongside your source file so the agent, your teammates and CI all agree on
what is already done.

## Install

```bash
pip install kaeris-mcp
```

### Or run it with Docker

```bash
docker build -t kaeris-mcp .
docker run -i --rm -v "$PWD:/work" -w /work kaeris-mcp
```

The server speaks JSON-RPC over stdin/stdout, so there is no port to expose —
`-i` is what keeps the conversation open. Mount your project at `/work` and the
repo-aware tools (`scan_repo`, `status`, `list_missing_keys`) read it directly;
pass `-e KAERIS_API_KEY=…` to use your KAERIS key.

## Configure your client

**Claude Desktop** — add to `claude_desktop_config.json`
(macOS: `~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`):

```json
{
  "mcpServers": {
    "kaeris-i18n": {
      "command": "kaeris-mcp",
      "env": {
        "KAERIS_API_KEY": "kaerisp_optional"
      }
    }
  }
}
```

**Cursor** — Settings → MCP → Add, or `.cursor/mcp.json`:

```json
{ "mcpServers": { "kaeris-i18n": { "command": "kaeris-mcp" } } }
```

**Claude Code** — one command:

```bash
claude mcp add kaeris-i18n kaeris-mcp
```

Restart the client; the KAERIS tools appear automatically.

## Auth & tiers (all optional)

| Env var | Purpose |
|---------|---------|
| `KAERIS_API_KEY` | Your KAERIS key: the free API key (50k chars/file), Pro, Team/API or Lifetime — higher limits (without one, the free 10k-char tier is used) |
| `KAERIS_OPENROUTER_KEY` | OpenRouter key for Lifetime/BYOK — no monthly volume cap |
| `KAERIS_API_URL` | Override the API base URL |
| `KAERIS_NO_UPDATE_CHECK` | Set to `1` to turn off the once-a-day "new version available" line (stderr only; already off in CI) |

No key is required to try it — the free anonymous tier works out of the box.

## Example prompts

- *"Translate the strings in `locales/en.json` into German, Ukrainian and Japanese."*
- *"Add French and Spanish translations for these buttons: Save, Cancel, Delete."*
- *"Only translate the new keys I added to en.json — don't redo the whole file."*
- *"Check this repo's i18n and tell me what's missing or broken."* (scans, then reports status — no API call)
- *"We don't have Ukrainian yet — add it."* (bootstraps a new locale via translation)

## License

MIT
