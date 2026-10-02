# RDR2 Mod Hub

A searchable catalog of **Red Dead Redemption 2 (PC)** mods that talks to the
Nexus Mods API and keeps your own shortlist in this repo.

Everything is free. No hosting, no subscriptions.

## What it does

| Feature | Notes |
|---|---|
| **Curated Catalog** | ~60 hand-picked mods, tagged by category, with notes on what you already run |
| **Live Nexus Search** | Real search against every RDR2 mod on Nexus (name, downloads, author) |
| **Top RDR2 Mods** | Browse the whole mod list sorted by downloads / name / endorsements |
| **My List** | Add any mod with one click, keep it locally, export it, or commit it here |
| **GitHub sync** | One button writes `mods.json` + `MODS.md` into this repository |

## Running it locally

```bash
cd rdr2-mod-hub
python server.py
```

Then open <http://127.0.0.1:8777/>

Requirements: Python 3 only (standard library, no pip installs).

## Files

| File | Purpose |
|---|---|
| `index.html` | The site (static, works offline, contains **no secrets**) |
| `server.py` | Local server: serves the page, talks to Nexus and GitHub |
| `installer.py` | Extracts a downloaded mod archive into the game folder, reversibly |
| `catalog.json` | Curated mod catalog |
| `secrets.json` | **Local only.** API keys. Git-ignored, never committed |
| `.gitignore` | Keeps `secrets.json`, caches and `installs/` out of git |

## Installing a mod

Nexus requires a logged-in browser session for file downloads, so the **archive
has to be on disk first** (download it from the Files tab on the mod's Nexus
page). Once you have the `.zip` or `.rar`, this installs it:

```bash
python installer.py dry-run "C:\path\to\Mod.zip"   # preview, writes nothing
python installer.py install    "C:\path\to\Mod.zip" # do it
python installer.py list                           # what has been installed
python installer.py undo      20261002-124818       # revert one install
```

What it does:

* Strips the single wrapper folder most mod zips have, so files land flat in
  the game folder, which is what RDR2 mods expect
* Skips `readme.txt`, `mods.txt`, licence files and other junk
* Refuses `..` and absolute paths, so a malicious archive cannot write outside
  the game folder
* **Copies any file it is about to overwrite into `installs/<id>/backup/`**, then
  records a manifest, so `undo` puts the original back and removes what it added
* Deletes only files it added itself, never anything else

Default game folder is `E:\rdr2\Red Dead Redemption 2`; override with `--game`.

`.rar` works via 7-Zip, UnRAR or WinRAR's console tool if one is installed.

## Why there is a server at all

1. **Nexus blocks browser calls** (no CORS), so a plain HTML page cannot query it.
2. **Keys must not be published.** A GitHub Pages site is world-readable, so an
   API key pasted into the page would leak. Instead the keys live in
   `secrets.json` on your machine and the local server does the API calls.

The page therefore works in two modes:

* **Local (full):** run `server.py` → live search + one-click GitHub commits.
* **Published (static):** GitHub Pages → catalog browsing and exports only;
  live search buttons report that the server is needed.

## Keys

`secrets.json` looks like this (never commit it):

```json
{
  "github_token": "github_pat_…",
  "github_repo": "you/your-repo",
  "nexus_api_key": "…"
}
```

* **Nexus key:** Nexus → account → `nexusmods.com/app/api` → *Personal API Key*.
* **GitHub token:** Settings → Developer settings → Personal access tokens →
  *Fine-grained*, scoped to this repository only, permission **Contents: read and write**.

Revoke and regenerate both if they are ever pasted into a chat, screenshot, or commit.

## Files written to this repo

* `mods.json` — your shortlist as data (used by the site's *Load from repo* button)
* `MODS.md` — the same list as a readable Markdown table