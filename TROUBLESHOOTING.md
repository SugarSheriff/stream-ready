# Troubleshooting

Every finding the add-on can raise has an entry here. Upper-case codes are Miris's own ingest error keys, and the **?** button for those opens [Miris's error reference](https://docs.miris.com/preparing-assets/error-reference). Lower-case codes are this add-on's own checks, and their **?** button opens the matching entry below.

---

## Miris error keys

### UNSUPPORTED_SCENE_SIZE
**You see:** *The export is 0.004 m x 0.004 m x 0.004 m, under Miris's 1 cm³ minimum…*
**Why:** Miris accepts geometry between 1 cm³ and 100 km³. Outside that range, it's almost always units. Something modeled in centimeters gets exported as meters, or the reverse. Changing `metersPerUnit` alone doesn't fix it, because the geometry itself is the wrong size.
**Fix:** Scale the objects to real-world meters and apply scale (*Ctrl+A → Scale*). The add-on measures world bounds × `metersPerUnit`, the same way the server does. Flat geometry is treated as 1 mm thick, so a ground plane isn't rejected for having zero volume.

### MISSING_REFERENCED_FILE
**You see:** *wood.png points at a file that does not exist…* or *…is an absolute path…*
**Why:** Miris only receives the upload folder. A texture that's missing, referenced by an absolute path, or sitting outside that folder isn't there when processing runs.
**Fix:** Relink missing files (*File → External Data → Find Missing Files*) or pack them. The add-on's export copies textures into `textures/` and writes relative paths, so absolute paths in the scene are fine. For UDIM sets, every tile listed on the image has to exist on disk.

### MISSING_VISIBLE_USD_PRIMS
**You see:** *Nothing to stream: no visible mesh with faces in the export scope.*
**Why:** Miris needs at least one renderable prim: visible, active, and with a `default` or `render` purpose.
**Fix:** Select what you want to publish, or switch the scope to **Visible**, and check that those objects are visible in the viewport and enabled for render.

### MORE_THAN_ONE_ROOT_USD_FILE / MISSING_USD_FILE
**Why:** Miris needs exactly one USD file at the root of the upload. The add-on's own export always produces exactly one. You'll only see these if a folder was assembled by hand.
**Fix:** Keep one `.usd` at the root and move sublayers or payloads into a subfolder.

---

## Add-on checks

### units.not_meters
**You see:** *Scene units are Metric, scale 0.01, length Centimeters…*
**Why:** The Miris Blender guide requires Metric, Unit Scale 1.0, and Meters. A non-1.0 unit scale is the classic cause of `UNSUPPORTED_SCENE_SIZE`.
**Fix:** One click: **Set Metric / 1.0 / Meters**. If the unit scale was not 1.0, check afterwards that objects are still real-world size.

### units.suspicious_size
**You see:** *The export is 800 m x 800 m x 10 m…*
**Why:** It's within Miris's limits, but things that big are usually a centimeter FBX imported as meters (100× too big).
**Fix:** Re-import with the right scale factor, then *Object → Apply → Scale*.

### material.unsupported_shader
**Why:** Blender's USD exporter turns a Principled BSDF (or Diffuse BSDF) into USD Preview Surface. Emission-only, Toon, and custom node setups export as a flat default material.
**Fix:** Rebuild the material on a Principled BSDF, or bake its look to image textures.

### material.procedural
**Why:** USD Preview Surface has no procedural textures. Noise, Voronoi, Wave, and similar nodes render in Blender and vanish on export.
**Fix:** Bake the procedural result to an image texture and connect that instead.

### transform.negative_scale
**Why:** Mirroring flips triangle winding. Clients that cull back faces will draw the object inside out.
**Fix:** *Ctrl+A → Scale*, then *Mesh → Normals → Recalculate Outside* (Shift+N) in Edit Mode.

### lighting.unequal_rgb
**Why:** The guide requires the light intensity's three components to be equal. The add-on exports their average, so the stream won't carry the tint you see in Blender.
**Fix:** Set all three components of the *Blender Light Intensity* node to the same value.

### lighting.no_hdri
**Why:** This is a note, not a problem. Without an Environment Texture on the World, the asset streams under Miris's default lighting.
**Fix:** Optional. Load an HDRI as the World's Environment Texture. The add-on handles the guide's ×1562 export calibration itself.

### bundle.shallow_check / usd.no_default_prim
Only seen outside Blender (no OpenUSD available) or with hand-assembled folders. Inside Blender, the add-on always runs the full OpenUSD check and sets `defaultPrim`.

---

## Publish errors

| Log says | Meaning | Fix |
|---|---|---|
| `401 … [invalid_api_key]` | The API key is missing, wrong, or revoked. | Use **Test Connection** in the add-on preferences, and **Set API Key** to replace it. Check `$MIRIS_API_KEY` too, because it overrides the saved key. If a key may have leaked, revoke it in the Miris portal and make a new one. |
| `422 … [manifest_mismatch]` | A file changed size between export and upload completion. | Publish again. Don't edit the export folder while an upload is running. |
| `429 SlowDown` / `503`, then `retry n/4` | Throttling or a brief outage. Retries follow `Retry-After`. | Nothing. If all retries fail, publish again later. |
| `SignatureDoesNotMatch` / `RequestTimeTooSkewed` | Storage rejected the signed upload. A wrong system clock is the usual cause. | Sync the system clock, then publish again. |
| `still processing after 600s` | Processing continues on Miris after Blender stops watching. | Check the asset in the Miris portal. The wait time is set in preferences. |
