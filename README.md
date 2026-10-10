# Stream Ready

A Blender add-on that gets a scene ready for [Miris](https://www.miris.com) 3D streaming and publishes it: preflight against Miris's own error codes, a correctly configured OpenUSD export, a check of the exported files, a SigV4-signed upload to the Miris REST API, and a watch on processing until the preview (or, on accounts that allow skipping it, the streamable asset) is ready.

**[Live demo →](https://sugarsheriff.github.io/stream-ready/)** · an interactive walkthrough of the panel and the publish flow, with toggleable scene problems and network faults

> **Unofficial.** I built this on my own to show how I'd approach a Miris DCC integration. It isn't affiliated with or endorsed by Miris. It's built from Miris's public docs and OpenAPI spec, and the gaps those leave are listed under [Verified vs. assumed](#verified-vs-assumed).

---

## Why this exists

Miris's [Blender guide](https://docs.miris.com/preparing-assets/blender) is clear, but it's about twenty manual steps. Set units to Metric / 1.0 / Meters. Make texture paths relative. Build a World shader with two labeled Multiply nodes and multiply the HDRI intensity by 1562. Then export USD with *Relative Paths*, *Convert World Material*, *USD Preview Surface*, and *Export Textures: New path* on, rename `.usdc` to `.usd`, and upload the whole folder. Miss one and the asset fails processing with one of the codes in the [error reference](https://docs.miris.com/preparing-assets/error-reference), or worse, it processes fine and streams too dark.

Miris ships a Substance 3D Painter plugin. Blender has a checklist. Stream Ready turns that checklist into one button and adds checks the checklist can't do by hand:

```
 Blender scene
     │
     ▼
 Preflight ── Miris's own error keys (UNSUPPORTED_SCENE_SIZE, MISSING_REFERENCED_FILE, …)
     │        + the guide's rules (units, shaders, HDRI) ── one-click fix where it's safe
     ▼
 USD export ── the guide's settings, built from what this Blender's exporter accepts
     │
     ▼
 Post-export fix-up ── DomeLight color = intensity × 1562 · metersPerUnit · defaultPrim
     │
     ▼
 Folder check with OpenUSD (pxr ships inside Blender): resolve every dependency,
     │   evaluate visibility + purpose, measure world bounds × metersPerUnit
     ▼
 POST /v1/asset ──► SigV4 PUT each file ──► PUT /v1/asset/upload/{id} ──► poll ──► viewer key
 (scoped creds)     (multipart if large)     (completed + size manifest)   GET /v1/asset/all/{id}
```

## What it catches

| Code | Caught where | What it means |
|---|---|---|
| `UNSUPPORTED_SCENE_SIZE` | scene + exported USD | Outside 1 cm³–100 km³. Usually a cm/m mix-up. Measured from world bounds × `metersPerUnit`. |
| `MISSING_REFERENCED_FILE` | scene + exported USD | Missing or unpacked textures, absolute paths, files outside the upload folder, UDIM sets with missing tiles. |
| `MISSING_VISIBLE_USD_PRIMS` | scene + exported USD | Nothing renderable: hidden, invisible, or `guide`/`proxy` purpose geometry. |
| `MORE_THAN_ONE_ROOT_USD_FILE` | exported folder | More than one USD file at the root. |
| `MISSING_USD_FILE` | exported folder | No USD file at the root. |
| `units.not_meters` | scene | Not Metric / 1.0 / Meters, as the guide requires. **One-click fix.** |
| `units.suspicious_size` | scene | Legal size, but 100× too big is the usual FBX centimeter mistake. |
| `material.unsupported_shader` | scene | Not a Principled or Diffuse BSDF, so it exports as a flat default. |
| `material.procedural` | scene | Noise, Voronoi, and similar nodes, which USD Preview Surface can't carry. |
| `transform.negative_scale` | scene | Mirrored mesh with flipped winding. Back-face culling draws it inside out. |
| `lighting.unequal_rgb` | scene | Tinted Blender Light Intensity. The guide requires equal components. |
| `lighting.no_hdri` | scene | Note only: the asset will stream under default lighting. |

The upper-case codes are Miris's own ingest error keys. The same code shows up whether it was caught locally in a second or returned by the server's `job_errors` after a round trip, so an artist only has to learn one vocabulary. Each finding's **?** button opens Miris's error reference for the upper-case codes and [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for the rest.

## Design decisions

**Calibrate the lighting in the file, not the scene.** The guide gets HDRI brightness into the stream by having the artist build an "Export Light Intensity" node set to Blender intensity × 1562. The smoke test showed how easily that goes wrong. With the export node set to 1562 but not the one wired into the Background, Blender 4.2 wrote `DomeLight color = (1, 1, 1)`, so the calibration silently never reached the file. So the add-on reads the intensity (the guide's "Blender Light Intensity" node, else the Background Strength) and writes `color = intensity × 1562` into the exported DomeLight itself. The viewport lighting is never touched, and the test checks the value in the real exported file.

**Check the output, not just the input.** After export, the add-on opens its own USD with OpenUSD (`pxr` ships inside Blender). It resolves every dependency with `UsdUtils.ComputeAllDependencies`, walks every `asset`-typed attribute, evaluates visibility and purpose, and measures world-space bounds × `metersPerUnit`. The mock server runs the same check as its "processing" step, so client and server can't disagree.

**SigV4 by hand, tested against AWS's own examples.** `POST /v1/asset` returns temporary storage credentials, not presigned URLs, so the client has to sign its own requests. Blender doesn't ship boto3, and pip-installing into an artist's Blender isn't a dependency strategy. `core/sigv4.py` is about 80 lines, and `tests/test_sigv4.py` checks it byte for byte against the four worked examples in AWS's S3 documentation. Large files use multipart upload, and an interrupted multipart upload is aborted so orphaned parts don't sit in the bucket.

**Never leave a half-finished upload.** If anything fails between `POST /v1/asset` and completion, including the artist pressing Cancel, the upload is marked `failed` or `canceled` through `PUT /v1/asset/upload/{id}`. A cancelled transport refuses to send, so that cleanup call goes out on a separate transport that ignores Cancel.

**Let the server verify the bytes.** Completion sends the spec's optional `manifest` (every file name and size), so Miris checks storage against what the client says it sent. The mock enforces it, and a test changes a file mid-upload to prove the check fires.

**The API key goes to one place.** The `Miris-API-Key` header is attached only to requests for the API's own origin, so storage hosts never see it (the mock records any request that leaks it, and the tests assert none do). `$MIRIS_API_KEY` overrides preferences, so a studio can inject it from a secrets manager. Otherwise it lives in Blender's user preferences, never in the `.blend`. The viewer key the API returns once is held in memory and copied to the clipboard on request, never saved.

**Respect the artist's Blender.** Stdlib only. Export runs on the main thread, where it has to, and the upload runs on a worker thread that never touches `bpy`. A timer redraws the panel. Cancel also cuts short any backoff wait. Blender's global *Allow Online Access* switch is honored.

**Exporter options are discovered, not assumed.** USD export options get renamed between Blender releases (`export_textures` became `export_textures_mode`). The add-on reads the running operator's RNA properties and only passes options and enum values that this Blender accepts.

## Verified vs. assumed

Built from Miris's public docs and their published OpenAPI spec (`Miris Public API 1.0.0`). Everything below the line still needs a first run against a real account.

| | |
|---|---|
| ✅ Verified | Endpoint paths, methods, request and response field names, the `Miris-API-Key` header, problem+json errors, error keys, the `<miris-scene>` embed markup (from the spec and docs). Asset states `processing_preview` → `preview` and `processing_streamable` → `streamable` / `error` (from the Miris portal's filters). SigV4 (AWS test vectors). USD export, DomeLight calibration, and the OpenUSD checks (headless Blender 4.2 LTS with OpenUSD 0.24). |
| ⚠️ Assumed | `input_type` is a free string in the spec; the add-on sends `"usd"` (configurable). `content_path` is sent as the root USD's file name. `job_process_status` values aren't enumerated, so the usual done/failed spellings are recognized and anything else counts as "still processing". Storage addressing is virtual-hosted style on AWS and path style elsewhere. How Miris measures flat geometry for the volume limit isn't documented; the add-on treats it as 1 mm thick. |

## Try it

```
python -m unittest discover -s tests                              # 47 tests, no Blender needed
python mock/mock_miris.py --port 8787 --api-key dev-key           # a local Miris to publish to
./package.ps1                                                     # builds dist/stream-ready-extension.zip
./tests/run_blender_smoke.ps1 -Blender "C:\path\to\blender.exe"   # end to end inside real Blender
```

Install `dist/stream-ready-extension.zip` with *Edit → Preferences → Get Extensions → Install from Disk* (Blender 4.2+). Open the **Miris** tab in the 3D Viewport sidebar (N). To publish without an account, set the API URL in the add-on preferences to the mock server. **Export Only** writes a Miris-ready folder without needing a key.

## What's here

| Path | What it is |
|---|---|
| `stream_ready/` | The add-on: sidebar panel, operators, preferences, `blender_manifest.toml`. |
| `stream_ready/snapshot.py` | The only preflight code that reads `bpy`. Turns the live scene into plain data. |
| `stream_ready/export.py` | USD export with the guide's settings, plus the post-export fix-up. |
| `stream_ready/core/` | No `bpy` imports: preflight rules, OpenUSD folder checks, SigV4, S3 multipart, the Miris client, and the publish flow. |
| `mock/mock_miris.py` | A local Miris API plus S3 storage. It verifies every signature, enforces the manifest, runs the same checks as "processing", and injects 503s and 429s on request. |
| `tests/` | 47 unit tests, plus `blender_smoke.py`: installs the zip into a sandboxed Blender, exports real USD, inspects it with `pxr`, and publishes through injected faults. |
| `index.html`, `demo.css`, `demo.js` | The GitHub Pages demo. |

## Not yet

- **Generate streamable from Blender.** Uploads land as a preview, like the Miris CLI's default. Turning a preview into the full streamable asset is a separate step (`miris asset generate`, or the portal), and the public REST docs don't name the endpoint, so for now the panel points you to the portal. *Skip preview* in preferences goes straight to streamable on accounts where Miris has enabled it.
- **Sign in with a browser.** The spec has a device-authorization flow (`/v1/oauth/device/*`), and the Substance plugin signs in that way. The docs don't say which scopes to request or how the resulting token is presented, so this version uses an API key rather than guessing.
- **Resume across Blender sessions.** Retries cover dropped requests, but quitting Blender mid-upload starts over (the CLI can resume).
- **Animation and MaterialX.** Miris's upload API only allows `output_type: static`, so animation export is off. MaterialX networks are a one-line switch in `export.py` once there's a reason to prefer them over USD Preview Surface.

---

Built by [Liam Hulsey](https://www.linkedin.com/in/liam-hulsey/) · [GitHub](https://github.com/SugarSheriff) · GPL-3.0-or-later (Blender add-ons import `bpy`)
