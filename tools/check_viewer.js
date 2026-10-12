// Headless check of the streaming viewer: node check_viewer.js <url> [screenshot.png]
// Loads the page, zooms in, switches layers, and reports what was fetched. No npm packages.
const {spawn} = require("node:child_process");
const fs = require("node:fs"), path = require("node:path"), os = require("node:os");
const URL_ = process.argv[2], SHOT = process.argv[3];
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe", PORT = 9388;
const sleep = ms => new Promise(r => setTimeout(r, ms));
let ws, seq = 0; const pending = new Map(), logs = [];
const send = (method, params = {}) => { const id = ++seq; ws.send(JSON.stringify({id, method, params})); return new Promise((res, rej) => pending.set(id, {res, rej})); };
const js = async expr => { const r = await send("Runtime.evaluate", {expression: `(async()=>{${expr}})()`, awaitPromise: true, returnByValue: true});
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text); return r.result.value; };
(async () => {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "tilecheck-"));
  const chrome = spawn(CHROME, ["--headless=new", `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`, "--window-size=1600,900",
    "--no-first-run", "--ignore-gpu-blocklist", "--enable-unsafe-swiftshader", "about:blank"], {windowsHide: true, stdio: "ignore"});
  let code = 1;
  try {
    let target; for (let i = 0; i < 40 && !target; i++) { await sleep(400); try { target = (await (await fetch(`http://127.0.0.1:${PORT}/json`)).json()).find(t => t.type === "page"); } catch (e) {} }
    ws = new WebSocket(target.webSocketDebuggerUrl); await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
    ws.onmessage = ev => { const m = JSON.parse(ev.data);
      if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.rej(new Error(m.error.message)) : p.res(m.result); }
      if (m.method === "Runtime.consoleAPICalled" && ["error", "warning"].includes(m.params.type)) logs.push(m.params.type + ": " + m.params.args.map(a => a.value ?? a.description).join(" "));
      if (m.method === "Runtime.exceptionThrown") logs.push("exception: " + (m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text));
      if (m.method === "Network.responseReceived" && m.params.response.status >= 400) logs.push("HTTP " + m.params.response.status + " " + m.params.response.url); };
    await send("Page.enable"); await send("Runtime.enable"); await send("Network.enable");
    await send("Emulation.setDeviceMetricsOverride", {width: 1600, height: 900, deviceScaleFactor: 1, mobile: false});
    await send("Page.navigate", {url: URL_});
    const wait = async (cond, label, ms = 60000) => { const t0 = Date.now(); while (Date.now() - t0 < ms) { if (await js(`return !!(${cond})`)) return; await sleep(300); } throw new Error("timed out: " + label); };
    const snap = label => js(`const s=window.__tiles; return ${JSON.stringify(label)}+": detail "+s.cell+" m, level "+s.level+", ground "+s.groundCell+" m ("+s.groundTiles+" tiles), in view "+s.inView+", fetched "+s.fetched+", "+(s.bytes/1e6).toFixed(2)+" MB, waiting "+s.pending+", errors "+s.errors`);
    const settle = async () => { await sleep(600); await wait("window.__tiles.pending===0", "tiles to arrive"); await sleep(400); };
    const pixel = () => js(`const c=document.getElementById('gl'),g=c.getContext('webgl2');await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
      const p=new Uint8Array(4*200*200);g.readPixels(c.width/2-100|0,c.height/2-100|0,200,200,g.RGBA,g.UNSIGNED_BYTE,p);const u=new Set();for(let i=0;i<p.length;i+=4)u.add(p[i]+','+p[i+1]+','+p[i+2]);return u.size`);
    await wait("window.__tiles && window.__tiles.ready", "viewer ready"); await settle();
    console.log(await snap("whole site, LiDAR depth"), "| distinct colours at centre:", await pixel());
    console.log("whole site, exact flag:", await js(`return document.getElementById('s-exact').textContent`));
    await js(`window.__setView(900, 0, 0)`); await settle();
    console.log(await snap("zoomed in"), "| distinct colours at centre:", await pixel());
    await js(`window.__setView(250, 0, 0)`); await settle();
    console.log(await snap("zoomed in close, panned"), "| distinct colours at centre:", await pixel());
    for (const k of ["prediction", "error", "terrain"]) { await js(`document.querySelector('#layers [data-kind=${k}]').click()`); await settle(); console.log(await snap(k), "| colours:", await pixel()); }
    await js(`document.querySelector('#layers [data-kind=prediction]').click()`); await settle();
    for (const b of await js(`return [...document.querySelectorAll('#blocks button')].map(b=>b.dataset.block)`)) { await js(`document.querySelector('#blocks [data-block="${b}"]').click()`); await settle(); console.log(await snap("prediction " + b + " m"), "| finest:", await js(`return document.getElementById('s-finest').textContent`)); }
    await js(`document.querySelector('#layers [data-kind=lidar]').click()`); await settle();
    for (const b of await js(`return [...document.querySelectorAll('#blocks button')].map(b=>b.dataset.block)`)) { await js(`document.querySelector('#blocks [data-block="${b}"]').click()`); await settle();
      console.log(await snap("LiDAR at " + b + " m"), "| exact:", await js(`return document.getElementById('s-exact').textContent`), "|", await js(`return document.getElementById('hud').textContent.split(' · ').slice(0,2).join(' · ')+' | range '+document.getElementById('lo').textContent+'..'+document.getElementById('hi').textContent`)); }
    // seam check: zoom in on the LiDAR layer, pan in steps, and count pixels that show the background through the ground
    await js(`document.querySelector('#layers [data-kind=error]').click(); document.querySelector('#blocks [data-block="3"]').click()`); await settle();
    const holes = () => js(`const c=document.getElementById('gl'),g=c.getContext('webgl2');await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
      const p=new Uint8Array(4*c.width*c.height);g.readPixels(0,0,c.width,c.height,g.RGBA,g.UNSIGNED_BYTE,p);let n=0;for(let i=0;i<p.length;i+=4)if(p[i]<12&&p[i+1]>14&&p[i+1]<22&&p[i+2]>18&&p[i+2]<26)n++;return [n,c.width*c.height]`);
    const seen = [];
    for (let k = 0; k <= 8; k++) { await js(`window.__setView(420, ${-1200 + k * 100}, 150)`); await settle(); const [n, total] = await holes(); seen.push((100 * n / total).toFixed(3) + "/" + await js(`return window.__tiles.groundTiles+"t"`)); }
    console.log("background showing through while panning at 3 m (% of pixels per step):", seen.join(" "), "| ground tiles", await js(`return window.__tiles.groundTiles`));
    if (SHOT) { await js(`document.querySelector('#layers [data-kind=lidar]').click(); window.__setView(1400, 0, 0)`); await settle(); await sleep(500);
      const r = await send("Page.captureScreenshot", {format: "png"}); fs.writeFileSync(SHOT, Buffer.from(r.data, "base64")); console.log("screenshot", SHOT); }
    console.log(logs.length ? "PAGE MESSAGES:\n  " + [...new Set(logs)].slice(0, 12).join("\n  ") : "no console errors, no failed requests");
    code = logs.some(l => !l.startsWith("warning")) ? 2 : 0;
  } catch (e) { console.log("FAILED:", e.message, "\n", logs.slice(0, 10).join("\n ")); }
  finally { try { ws && ws.close(); } catch (e) {} chrome.kill(); process.exit(code); }
})();
