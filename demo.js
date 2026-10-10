// Scripted replay of the add-on's publish flow. Codes, messages, endpoints, and retry
// rules mirror stream_ready/core/{preflight,bundle,publish,miris}.py and mock/mock_miris.py.
(() => {
  const consoleEl = document.getElementById('console');
  const rail = document.getElementById('rail');
  const findingsEl = document.getElementById('findings');
  const btnPublish = document.getElementById('btn-publish');
  const btnPreflight = document.getElementById('btn-preflight');
  const toggles = [...document.querySelectorAll('[data-fault]')];

  const STAGES = ['preflight', 'export', 'check', 'reserve', 'upload', 'complete', 'process', 'share'];
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const FILES = [['oak_chair.usd', 3891], ['textures/oak_basecolor.png', 41203], ['textures/studio_hdri.exr', 100663296]];
  const MULTIPART_THRESHOLD = 64 * 1048576; // core/storage.py
  const PART_SIZE = 16 * 1048576;
  let t0 = 0;
  let running = false;

  const faults = () => Object.fromEntries(toggles.map((t) => [t.dataset.fault, t.checked]));
  const sleep = (ms) => new Promise((r) => setTimeout(r, reduceMotion ? Math.min(ms, 60) : ms));
  const hex = (n) => [...crypto.getRandomValues(new Uint8Array(n))].map((b) => b.toString(16).padStart(2, '0')).join('');
  const uuid = () => crypto.randomUUID ? crypto.randomUUID() : `${hex(4)}-${hex(2)}-4${hex(2).slice(1)}-a${hex(2).slice(1)}-${hex(6)}`;
  const kb = (n) => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${(n / 1024).toFixed(1)} KB`);
  const today = new Date().toISOString().slice(0, 10).replace(/-/g, '');

  function log(text, cls = '') {
    const line = document.createElement('div');
    line.className = `line ${cls}`;
    const t = document.createElement('span');
    t.className = 't';
    t.textContent = `+${((performance.now() - t0) / 1000).toFixed(2)}s`;
    line.append(t, text);
    consoleEl.append(line);
    consoleEl.scrollTop = consoleEl.scrollHeight;
  }

  const stage = (name, state) => { rail.querySelector(`[data-stage="${name}"]`).className = state; };
  const resetRail = () => rail.querySelectorAll('li').forEach((li) => (li.className = ''));
  const skipRest = (from) => STAGES.slice(STAGES.indexOf(from) + 1).forEach((s) => stage(s, 'skip'));

  // Same codes, severities, and wording as core/preflight.py, errors first.
  function preflightFindings(f) {
    const out = [];
    if (f.hidden) out.push({ sev: 'error', code: 'MISSING_VISIBLE_USD_PRIMS', target: 'Scene', miris: true,
      msg: 'Nothing to stream: no visible mesh with faces in the export scope.',
      fix: 'Select the objects to publish (or switch scope to Visible) and make sure they have faces.' });
    if (f.missing) out.push({ sev: 'error', code: 'MISSING_REFERENCED_FILE', target: 'Oak / oak_basecolor.png', miris: true,
      msg: 'oak_basecolor.png points at a file that does not exist, so the export cannot copy it into the upload.',
      fix: 'Relink it (File > External Data > Find Missing Files) or pack it into the .blend.' });
    if (f.tiny) out.push({ sev: 'error', code: 'UNSUPPORTED_SCENE_SIZE', target: 'Scene', miris: true,
      msg: "The export is 0.005 m x 0.005 m x 0.009 m, under Miris's 1 cm³ minimum. Usually a model built in centimeters or millimeters but exported as meters.",
      fix: 'Scale the objects to real-world meters and apply scale (Ctrl+A > Scale).' });
    if (f.tint) out.push({ sev: 'warn', code: 'lighting.unequal_rgb', target: 'World',
      msg: 'Blender Light Intensity is (1, 1, 0.5). The Miris guide requires all three components to be equal; the add-on exports their average.',
      fix: 'Set all three components to the same value.' });
    if (f.procedural) out.push({ sev: 'warn', code: 'material.procedural', target: 'Oak',
      msg: 'Oak uses a Noise Texture node. USD Preview Surface has no procedural textures, so that part of the look is dropped on export.',
      fix: 'Bake the procedural result to an image texture and plug that in instead.' });
    if (f.units) out.push({ sev: 'warn', code: 'units.not_meters', target: 'Scene', autofix: 'units',
      msg: "Scene units are Metric, scale 0.01, length Centimeters. Miris's Blender guide requires Metric, Unit Scale 1, Meters.",
      fix: 'Set Scene Properties > Units to Metric / 1.0 / Meters (one click below).' });
    return out;
  }

  function renderFindings(findings) {
    findingsEl.replaceChildren();
    if (!findings.length) {
      const ok = document.createElement('div');
      ok.className = 'bfinding ok';
      ok.textContent = '✓ Ready for Miris';
      findingsEl.append(ok);
      return;
    }
    for (const x of findings) {
      const box = document.createElement('div');
      box.className = `bfinding ${x.sev}`;
      const head = document.createElement('div');
      head.className = 'bf-head';
      head.textContent = `${x.sev === 'error' ? '⛔' : '⚠'} ${x.code} · ${x.target}`;
      const msg = document.createElement('div');
      msg.className = 'bf-msg';
      msg.textContent = x.msg;
      box.append(head, msg);
      if (x.autofix === 'units') {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'bbtn small';
        btn.textContent = '🔧 Set Metric / 1.0 / Meters';
        btn.addEventListener('click', () => {
          toggles.find((t) => t.dataset.fault === 'units').checked = false;
          renderFindings(preflightFindings(faults()));
        });
        box.append(btn);
      }
      findingsEl.append(box);
    }
  }

  async function runPreflight(f) {
    stage('preflight', 'active');
    log('stream_ready.preflight  scope=VISIBLE  objects=1  materials=1  world=Studio', 'req');
    await sleep(450);
    const findings = preflightFindings(f);
    renderFindings(findings);
    for (const x of findings) {
      log(`${x.sev.toUpperCase().padEnd(5)} ${x.code}  ${x.target}`, x.sev === 'error' ? 'err' : 'warn');
      log(x.msg, 'fix');
      await sleep(220);
    }
    const errors = findings.filter((x) => x.sev === 'error').length;
    log(findings.length ? `Preflight: ${errors} error(s), ${findings.length - errors} warning(s)` : 'Preflight clean',
      errors ? 'err' : findings.length ? 'warn' : 'ok');
    stage('preflight', errors ? 'fail' : 'done');
    return errors;
  }

  // One request under the client's retry policy (core/http.py): retry 429/5xx with
  // Retry-After or jittered backoff, fail fast on other 4xx. Last scripted response repeats.
  async function call(label, script, stageName) {
    for (let attempt = 1; attempt <= 5; attempt++) {
      const r = script[Math.min(attempt - 1, script.length - 1)];
      await sleep(260);
      if (r.status < 400) return r;
      if (r.status === 429 || r.status >= 500) {
        const wait = r.retryAfter ?? (0.25 * 2 ** (attempt - 1) * (0.5 + Math.random() / 2));
        log(`${label} -> ${r.status} ${r.detail}; retry ${attempt}/4 in ${wait.toFixed(1)}s`, 'warn');
        stage(stageName, 'retry');
        await sleep(wait * 1000);
        stage(stageName, 'active');
        continue;
      }
      log(`${label} -> ${r.status}: ${r.detail}`, 'err');
      return r;
    }
    return { status: 599 };
  }

  async function publish() {
    if (running) return;
    running = true;
    btnPublish.disabled = btnPreflight.disabled = true;
    consoleEl.replaceChildren();
    resetRail();
    t0 = performance.now();
    const f = faults();
    const sceneErrors = preflightFindings(f).filter((x) => x.sev === 'error');

    try {
      if (f.skip) {
        stage('preflight', 'skip');
        log('local checks skipped (demo only: the real add-on always runs them)', 'dim');
      } else if (await runPreflight(f)) {
        log('Fix errors before publishing. Nothing was exported or uploaded.', 'err');
        skipRest('preflight');
        return;
      }

      stage('export', 'active');
      log('wm.usd_export  relative_paths=True  convert_world_material=True  generate_preview_surface=True  export_textures=True', 'req');
      await sleep(500);
      const level = f.tint ? (1 + 1 + 0.5) / 3 * 1562 : 1562;
      log(`DomeLight color = ${+level.toFixed(2)} (Blender intensity x 1562, per the Miris guide)`, 'dim');
      stage('export', 'done');

      stage('check', 'active');
      if (f.skip) {
        log('OpenUSD check skipped', 'dim');
        stage('check', 'skip');
      } else {
        await sleep(400);
        log(`OpenUSD: ${FILES.length} files · root oak_chair.usd · metersPerUnit 1 · 0.50 m x 0.50 m x 0.90 m · all asset paths resolve inside the folder`, 'ok');
        stage('check', 'done');
      }

      const total = FILES.reduce((a, [, n]) => a + n, 0);
      log(`${FILES.length} file(s), ${kb(total)}`, 'dim');
      stage('reserve', 'active');
      const reserve = await call('POST /v1/asset', f.s503
        ? [{ status: 503, detail: 'Service Unavailable', retryAfter: 1 }, { status: 503, detail: 'Service Unavailable', retryAfter: 1 }, { status: 200 }]
        : [{ status: 200 }], 'reserve');
      if (reserve.status >= 400) { stage('reserve', 'fail'); skipRest('reserve'); return; }
      const id = uuid();
      const prefix = `uploads/${id}/`;
      log(`asset ${id} reserved · scoped credentials for s3://miris-ingest/${prefix}`, 'ok');
      stage('reserve', 'done');

      stage('upload', 'active');
      const akid = `ASIA${hex(8).toUpperCase()}`;
      for (const [i, [name, size]] of FILES.entries()) {
        const parts = size >= MULTIPART_THRESHOLD ? Math.ceil(size / PART_SIZE) : 0;
        log(`PUT ${prefix}${name}${parts ? `  (multipart: ${parts} x 16 MiB parts)` : ''}\n    Authorization: AWS4-HMAC-SHA256 Credential=${akid}/${today}/us-west-2/s3/aws4_request …`, 'req');
        const r = await call(`PUT ${name}`, f.s429 && i === 0
          ? [{ status: 429, detail: 'SlowDown: Please reduce your request rate.', retryAfter: 1 }, { status: 200 }]
          : [{ status: 200 }], 'upload');
        if (r.status >= 400) { stage('upload', 'fail'); skipRest('upload'); return; }
        log(`uploaded ${name} (${kb(size)})`, 'ok');
      }
      log('Miris-API-Key sent to storage: never (it only goes to the API origin)', 'dim');
      stage('upload', 'done');

      stage('complete', 'active');
      log(`PUT /v1/asset/upload/${id.slice(0, 8)}…  {"status": "completed", "manifest": [${FILES.length} files + sizes]}`, 'req');
      const done = await call('PUT /v1/asset/upload', f.changed
        ? [{ status: 422, detail: 'textures/oak_basecolor.png: expected 41203 bytes, storage has 41987 [manifest_mismatch]' }]
        : [{ status: 200 }], 'complete');
      if (done.status >= 400) {
        log(`PUT /v1/asset/upload/${id.slice(0, 8)}…  {"status": "failed"}`, 'req');
        await sleep(250);
        log('upload marked failed on Miris (nothing left half-finished in the portal)', 'warn');
        stage('complete', 'fail');
        skipRest('complete');
        return;
      }
      log('upload verified by Miris; processing', 'ok');
      stage('complete', 'done');

      stage('process', 'active');
      log(`GET /v1/asset/all/${id.slice(0, 8)}…  state=processing_streamable job=processing`, 'req');
      await sleep(900);
      const rejected = sceneErrors.filter((x) => x.miris);
      if (rejected.length) {
        log(`GET /v1/asset/all/${id.slice(0, 8)}…  job=failed`, 'req');
        log(`Miris rejected the asset: ${rejected.map((x) => x.code).join(', ')}`, 'err');
        for (const x of rejected) log(`${x.code}: ${x.msg}`, 'fix');
        log('Same code preflight raises locally, in about a second, before any upload.', 'warn');
        stage('process', 'fail');
        skipRest('process');
        return;
      }
      log(`GET /v1/asset/all/${id.slice(0, 8)}…  state=streamable job=completed`, 'req');
      log('asset ready (streamable)', 'ok');
      stage('process', 'done');

      stage('share', 'active');
      log('POST /v1/asset-viewer-key  {"name": "oak_chair (Blender share)", "asset_ids": [1 asset], "tags": ["stream-ready"]}', 'req');
      await sleep(350);
      const key = hex(16);
      log(`viewer key ${key.slice(0, 6)}… created, scoped to this asset (held in memory, never saved)`, 'ok');
      log(`<miris-scene viewer-key="${key.slice(0, 6)}…">\n  <miris-stream uuid="${id}"></miris-stream>\n</miris-scene>`, 'dim');
      stage('share', 'done');
    } finally {
      running = false;
      btnPublish.disabled = btnPreflight.disabled = false;
    }
  }

  btnPublish.addEventListener('click', publish);
  btnPreflight.addEventListener('click', async () => {
    if (running) return;
    consoleEl.replaceChildren();
    resetRail();
    t0 = performance.now();
    await runPreflight(faults());
  });
  toggles.forEach((t) => t.addEventListener('change', () => {
    if (findingsEl.childElementCount) renderFindings(preflightFindings(faults()));
  }));
})();
