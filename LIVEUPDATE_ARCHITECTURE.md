# Liveupdate: architecture

This document describes the interaction between two components:

- **`tools/liveupdate_pack.py`** — offline archive/manifest builder (invoked from `make buildliveupdateres`).
- **`common/services/liveupdater.lua`** — runtime client that downloads archives, mounts them and keeps the local cache consistent.

They communicate through two artifacts published to the CDN: `manifest.json` and `<archive>_<version>.arcd0`.

---

## 1. Data flow

```
┌────────────────────┐                   ┌────────────────────────┐
│    bob build       │  ───────────────▶ │ liveupdate_dist/       │
│                    │                   │   <hex>     (raw)      │
└────────────────────┘                   │   liveupdate.dmanifest │
                                         │   game.graph.json      │
                                         └─────────┬──────────────┘
                                                   │
                                                   ▼
                                    ┌──────────────────────────────┐
                                    │ tools/liveupdate_pack.py     │
                                    │  • groups resources          │
                                    │  • splits into chunks        │
                                    │  • computes version_hash     │
                                    │  • writes archives + manifest│
                                    └─────────┬────────────────────┘
                                              │
                                              ▼
                                    ┌──────────────────────────────┐
                                    │ liveupdate_zip/              │
                                    │   manifest.json              │
                                    │   <archive>_<ver>.arcd0      │
                                    └─────────┬────────────────────┘
                                              │ deploy
                                              ▼
                                       CDN  (lowres / highres)
                                              │
                                              ▼
                                    ┌──────────────────────────────┐
                                    │ liveupdater.lua (client)     │
                                    │  • check manifests           │
                                    │  • diff against saved_list   │
                                    │  • download + mount          │
                                    └──────────────────────────────┘
```

The pipeline runs separately for **lowres** and **highres** (when highres is enabled — see §6).

---

## 2. What `liveupdate_pack.py` does

### 2.1 Inputs

- `build/default/game.graph.json` — Defold's resource graph (`path`, `hexDigest`, `children`, `nodeType`, `isInMainBundle`).
- `liveupdate_dist/` — raw resources, file names = `hexDigest`.
- `liveupdate_dist/liveupdate.game.dmanifest` — engine's protobuf manifest.

### 2.2 Grouping

All resources outside the main bundle are split into four archive classes:

| Prefix | Contents |
| --- | --- |
| `<collection>.collectionc`        | non-texture resources of a specific collection-proxy |
| `<collection>.collectionc_texture`| textures (`.texturec`, `.a.texturesetc`) of that collection |
| `common_<hash>`                   | non-texture resources shared by ≥ 2 collections |
| `common_texture_<hash>`           | shared textures used by ≥ 2 collections |

Logic:

1. `get_deps_files` walks every `ExcludedCollectionProxy` and gathers their transitive dependencies.
2. `build_common_files` tags resources with `use_count` (how many collections reference each one).
3. `create_common_archives_by_dependency_sets` groups shared resources by the **exact set of consumer collections** — so a common chunk is re-downloaded only by the collections that actually use it.
4. `split_by_size` slices each class into pieces ≤ `MAX_ARCHIVE_SIZE` (7 MiB), trying to keep `*.texturec` + `*.a.texturesetc` pairs together.

### 2.3 Archive version

`compute_version_hash_from_files` (and the equivalent in `create_zip_archive`) computes `version_hash` as a SHA-256 over:

- `dmanifest` `resource` entries sorted by `hash.data.hex()`,
- the string `content_hash_no_manifest:<sha256 of contents excluding dmanifest>`.

The first `HASH_LEN = 16` hex characters are kept. The same string is used in:

- the CDN file name: `<archive_name>_<version_hash>.arcd0`,
- the version field in `manifest.json`.

`engine_versions` is **not** part of the hash — bumping the engine version does not invalidate archives.

### 2.4 Outputs

- `liveupdate_zip/<archive>_<ver>.arcd0` — zip archives with a `dmanifest` inside.
- `liveupdate_zip/manifest.json` — index for the client (format — see §3).
- `files_tree.json` — internal snapshot for reproducible rebuilds (`--restore_from_tree`).

---

## 3. `manifest.json` format

```json
{
  "version": "1777019959",
  "collections": [
    "baking_festival_window.collectionc",
    "baking_festival_info_window.collectionc",
    ...
  ],
  "files": {
    "baking_festival_window.collectionc": ["a3e317707f7a3947"],
    "common_texture_ad6d67fefa501371":   ["21f9526418de6268", [0, 1, 5, 7]],
    ...
  },
  "dmanifest_info": { ... }
}
```

- **`collections`** — deduplicated array of collection names referenced by common archives. Used as a pool for index-based addressing.
- **`files`** — single map `archive_name → entry`:
  - `[version_hash]` — a leaf (collection) archive with no dependents.
  - `[version_hash, [idx, idx, ...]]` — a common dependency archive; 0-based indices into `collections[]` show which collections pull this archive.
- **`dmanifest_info`** — auxiliary metadata (signature, engine versions, header hashes).

This format replaced the older `deps` + `file_versions` pair (see §7).

---

## 4. What `liveupdater.lua` does

### 4.1 State

- `M.modules` — module declarations: `{ files, priority, res_mode, by_request? }`.
- `M.saved_list[file_name] = version` — what is already on disk.
- `global_manifest = { lowres, highres? }` — the latest fresh manifests fetched from the CDN.
- `dependency_map_low/high[file_name]` — reverse map `collection → set(common archives)`, built from `manifest.files` (see `build_dependency_map`).
- `download_files_queue` — priority queue.

### 4.2 Init flow

1. **Load `saved_list`** from `sys.save`.
2. **`check_manifests`** — parallel HTTP requests for `lowres/manifest.json` and (optionally) `highres/manifest.json`.
3. **`classify_files`** — computes `files_to_remove`: anything in `saved_list` that is missing from both fresh manifests or whose version no longer matches.
4. **Cleanup of stale entries**: unmount + delete file + clear `saved_list`.
5. **`mount_existing_valid_files`** — mount whatever is already valid on disk.
6. **`enqueue_modules`** — build the queue.
7. **`check_modules_integrity`** — sanity-check modules vs manifest (warnings only, non-fatal).
8. **`try_start_load_resources`** — drain the queue sequentially.
9. After every successful mount: `M.saved_list[file] = version` + `MSG_FILE_LOADED` event. When the queue empties — `MSG_ALL_LOADED`.

### 4.3 Queue

- Dedup key: `file_name:version`. If a new item with the same key arrives, the lower `priority` wins.
- Dependencies (`build_dependency_map`) are inserted with priority `module.priority - 0.5`, i.e. strictly before the main file.
- Highres items are added with `priority + 1000` so lowres always overtakes highres.
- Highres items are flagged `remount=true` — the old mount and local file are removed before saving the new one.
- Sort order: `(priority asc, order asc)`.

### 4.4 Network retries

`request_data` retries up to `max_attempts = 3` times on a network error / non-200. When all attempts fail, it emits `MSG_NETWORK_ERROR` and reschedules the queue 5 s later.

---

## 5. Resolution modes (`RES_*`)

| Mode | Semantics |
| --- | --- |
| `RES_LOW_ONLY`  | module uses only the lowres variant |
| `RES_HIGH_ONLY` | module uses highres |
| `RES_BOTH`      | low is downloaded first, then upgraded to high |

Dedup by `file_name:version` guarantees that when low/high versions match, the file is downloaded once.

---

## 6. Optional highres

`hires_server_path` in `init_options` is optional. Behaviour:

- if no path is set → `has_highres = false`, `check_manifests` skips the highres manifest request, `global_manifest.highres = nil`.
- every module (including `RES_HIGH_ONLY`) is served from lowres: `supports_high` is forced to `false`, `supports_low` is forced to `true`.
- `check_modules_integrity` falls back to `lowres` as the reference when `highres` is missing.

This lets projects/configurations without a dedicated highres server share a single pipeline.

---

## 7. How the format evolved

Historically the client and the packer used:

```json
{ "deps": { "archive": ["coll1.collectionc", ...] },
  "file_versions": { "archive": "<hash>" } }
```

The current format (§3) brings:

1. Deduplication of collection names via `collections[]` + indices instead of repeated strings.
2. Merging of `deps` and `file_versions` into a single `files` table — no key duplication.

Net effect: ~−45 % JSON size before compression (~79 KB → ~45 KB on the current production manifest), and another ~5× shrink after brotli thanks to high redundancy.

---

## 8. Files and entry points

| File | Purpose |
| --- | --- |
| `tools/liveupdate_pack.py`                  | Pack: archive construction and `manifest.json` |
| `common/services/liveupdater.lua`           | Runtime: download + mount + queue |
| `lobby/util/liveupdater_modules_util.lua`   | Module declarations (`priority`, `RES_*`, `by_request`) |
| `dist/output/<ver>/liveupdatelowres/`       | Lowres artifacts (for CDN deploy) |
| `dist/output/<ver>/liveupdatehighres/`      | Highres artifacts |

---

## 9. Invariants worth preserving

- `version_hash` is deterministic with respect to the contents of `dmanifest.resources` and the raw bytes of the resources in the archive — reordering / regrouping without changing content must not change versions.
- The CDN file name is always `<archive>_<files[archive][0]>.arcd0`.
- `collections[i]` — stable order **within a single manifest**; indices are valid only inside it and must not be compared across builds.
- Any client-side branch that reads `manifest.highres` must tolerate `nil` (helpers `get_file_version`, `build_dep_set`, `build_dependency_map` are already nil-safe).
- `saved_list` is the single source of truth about what is on disk; mounting without updating `saved_list` will desync on the next start-up.
