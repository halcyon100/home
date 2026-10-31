/* <app-bar>: the shared top bar for the house's own apps (House, Music, Frame).
 *
 * Home is a grid of the apps (in House). Inside an app the bar has a back button to Home on the
 * left, the app's own views in the middle, and Talk on the right. On Home there's no back button.
 *
 * One source of truth: this file lives in /shared and is copied into each app by
 * tools/sync-shared. Each app has a test that fails if its copy drifts.
 *
 * Usage:
 *   <app-bar app="music" title="Music"><nav slot="tabs">...your tabs...</nav></app-bar>
 *   Add the `home` attribute on the Home page itself. The back button fires a cancelable "back"
 *   event first, so a page with an inner level can handle it; otherwise it goes Home.
 *   <script src="appnav.js"></script>
 *
 * Colours come from the page: --ground, --ink, --quiet, --hair (with fallbacks).
 * Inside Home Assistant, switching apps uses HA's own navigation, so there's no reload.
 */
(() => {
  // The typeface sits next to this script; declare it on the page so every app gets it.
  const here = document.currentScript && document.currentScript.src;
  if (here && !document.getElementById("app-bar-font")) {
    const st = document.createElement("style");
    st.id = "app-bar-font";
    st.textContent = `@font-face{font-family:"Archivo";src:url("${new URL("archivo.woff2", here)}") format("woff2-variations");font-weight:100 900;font-stretch:62% 125%;font-display:swap}`;
    document.head.appendChild(st);
  }
  // Day or night, from the sun times House last recorded (shared across apps on this origin).
  try {
    const sun = JSON.parse(localStorage.getItem("house-sun") || "null");
    if (sun && !document.documentElement.dataset.sky) {
      const now = Date.now(), rise = Date.parse(sun.rise), set = Date.parse(sun.set);
      if (now - rise < 36e5 * 30)   // only trust it for about a day
        document.documentElement.dataset.sky = (now >= rise && now < set) ? "day" : "night";
    }
  } catch (e) {}
  // Inside Home Assistant the page sits in a frame below HA's own header: no notch to clear.
  try { if (window.top !== window) document.documentElement.classList.add("in-ha"); } catch (e) { document.documentElement.classList.add("in-ha"); }
  const HOME = "/3dd8a0cc_house";

  function inHA() {
    try { return window.top !== window && window.top.location.origin === location.origin; }
    catch (e) { return false; }
  }

  function go(path) {
    if (inHA()) {
      const top = window.top;
      top.history.pushState(null, "", path);
      top.dispatchEvent(new CustomEvent("location-changed", {detail: {replace: false}}));
    } else {
      location.href = "http://homeassistant.local:8123" + path;
    }
  }

  window.appGo = go;   // pages use this to open another app

  /* ---------- Ask the house: typed or spoken, through Home Assistant's voice pipeline ---------- */
  function hass() {
    try { return inHA() ? window.top.document.querySelector("home-assistant").hass : null; } catch (e) { return null; }
  }
  let convId = null;
  // The Home Assistant phone app has its own Assist: it holds the microphone itself (so it works over
  // plain http at home), speaks the answer, and shows the text. Use it when we're inside the app.
  function nativeAssist() {
    try { const ext = hass() && hass().auth.external; return ext && ext.config && ext.config.hasAssist ? ext : null; } catch (e) { return null; }
  }
  const canListen = () => { try { return window.top.isSecureContext && !!window.top.navigator.mediaDevices; } catch (e) { return false; } };
  // iOS plays sound only from a tap; unlock one audio element during the tap and reuse it for the reply.
  let voice = null;
  const SILENCE = (() => {   // 0.1 s of silent 8 kHz mono WAV
    const n = 800, b = new DataView(new ArrayBuffer(44 + n)), w = (o, t) => [...t].forEach((c, i) => b.setUint8(o + i, c.charCodeAt(0)));
    w(0, "RIFF"); b.setUint32(4, 36 + n, true); w(8, "WAVEfmt "); b.setUint32(16, 16, true); b.setUint16(20, 1, true); b.setUint16(22, 1, true);
    b.setUint32(24, 8000, true); b.setUint32(28, 8000, true); b.setUint16(32, 1, true); b.setUint16(34, 8, true); w(36, "data"); b.setUint32(40, n, true);
    for (let i = 0; i < n; i++) b.setUint8(44 + i, 128);
    let bin = ""; new Uint8Array(b.buffer).forEach(x => bin += String.fromCharCode(x)); return "data:audio/wav;base64," + btoa(bin);
  })();
  function unlockVoice() {
    try { voice = voice || new Audio(); voice.src = SILENCE; voice.play().catch(() => {}); } catch (e) {}
  }
  function speak(url) { try { const a = voice || new Audio(); a.src = url; a.play().catch(() => {}); } catch (e) {} }

  function runPipeline(msg, onEvent) {
    const h = hass();
    if (!h) return Promise.reject(new Error("Open this inside Home Assistant to ask."));
    return new Promise((resolve, reject) => {
      let unsub = null, done = false;
      const finish = (fn) => { if (done) return; done = true; if (unsub) unsub(); fn(); };
      h.connection.subscribeMessage(ev => {
        onEvent && onEvent(ev);
        if (ev.type === "intent-end") {
          const o = ev.data.intent_output || {};
          convId = o.conversation_id || convId;
        }
        if (ev.type === "error") finish(() => reject(new Error(ev.data.message || ev.data.code)));
        if (ev.type === "run-end") finish(resolve);
      }, Object.assign({type: "assist_pipeline/run"}, convId ? {conversation_id: convId} : {}, msg))
        .then(u => { unsub = u; if (done) u(); })
        .catch(e => finish(() => reject(e)));
    });
  }
  const speechOf = ev => (((ev.data.intent_output || {}).response || {}).speech || {}).plain?.speech || "";

  // Microphone: 16 kHz mono 16-bit PCM, captured in Home Assistant's own window (it holds the
  // microphone permission), streamed until Home Assistant hears the speech end.
  async function listen(onEvent, ui) {
    const top = window.top, h = hass();
    if (!top.isSecureContext || !top.navigator.mediaDevices) throw new Error("NOSECURE");
    const stream = await top.navigator.mediaDevices.getUserMedia({audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true}});
    const Ctx = top.AudioContext || top.webkitAudioContext, ctx = new Ctx();
    const src = ctx.createMediaStreamSource(stream), proc = ctx.createScriptProcessor(4096, 1, 1);
    let handler = null, open = false, stopped = false;
    const queue = [];
    const send = buf => { const m = new Uint8Array(buf.byteLength + 1); m[0] = handler; m.set(new Uint8Array(buf), 1); h.connection.socket.send(m); };
    const stop = () => {
      if (stopped) return; stopped = true;
      try { proc.disconnect(); src.disconnect(); } catch (e) {}
      stream.getTracks().forEach(t => t.stop()); ctx.close();
      if (handler !== null) h.connection.socket.send(new Uint8Array([handler]));
      ui.stopped();
    };
    proc.onaudioprocess = e => {
      if (stopped) return;
      const inp = e.inputBuffer.getChannelData(0), ratio = ctx.sampleRate / 16000, n = Math.floor(inp.length / ratio);
      const out = new Int16Array(n);
      for (let i = 0; i < n; i++) { const v = Math.max(-1, Math.min(1, inp[Math.floor(i * ratio)])); out[i] = v < 0 ? v * 0x8000 : v * 0x7fff; }
      if (open) { while (queue.length) send(queue.shift()); send(out.buffer); } else queue.push(out.buffer);
    };
    src.connect(proc); proc.connect(ctx.destination);
    ui.stop = stop;
    const run = runPipeline({start_stage: "stt", end_stage: "tts", input: {sample_rate: 16000}}, ev => {
      if (ev.type === "run-start") handler = ev.data.runner_data.stt_binary_handler_id;
      if (ev.type === "stt-start") open = true;
      if (ev.type === "stt-vad-end" || ev.type === "stt-end") stop();
      onEvent(ev);
    });
    setTimeout(stop, 15000);   // never leave the microphone open
    try { await run; } finally { stop(); }
  }

  const css = `
  :host{display:block;position:sticky;top:0;z-index:20;background:var(--ground,Canvas);color:var(--ink,CanvasText);
    font-family:"Archivo",system-ui,sans-serif;font-feature-settings:"tnum" 1}
  .bar{display:flex;align-items:center;gap:12px;min-height:52px;border-bottom:1px solid var(--hair,color-mix(in srgb,currentColor 18%,transparent))}
  .tabs{flex:1;min-width:0;overflow-x:auto;scrollbar-width:none;-webkit-overflow-scrolling:touch}
  .tabs::-webkit-scrollbar{display:none}
  .back{appearance:none;flex:none;display:flex;align-items:center;gap:6px;height:40px;padding:0 16px 0 10px;margin:6px 4px 6px 0;
    border:0;border-radius:999px;background:var(--well,color-mix(in srgb,currentColor 9%,transparent));color:inherit;cursor:pointer;
    font:inherit;font-weight:560;font-stretch:90%;font-size:15px;letter-spacing:-.005em;transition:transform .15s ease,background-color .2s}
  .back svg{width:18px;height:18px}
  .back:active{transform:scale(.94)}
  .back[hidden]{display:none}
  .home-title{flex:1;font-weight:600;font-stretch:92%;font-size:17px;letter-spacing:-.01em;padding:14px 0}
  .home-title[hidden]{display:none}
  .back:focus-visible,button:focus-visible{outline:2px solid currentColor;outline-offset:3px}
  dialog{border:0;padding:0;margin:0;max-width:none;max-height:none;width:100%;height:100%;background:transparent;color:inherit}
  dialog::backdrop{background:color-mix(in srgb,var(--ink,#000) 28%,transparent)}
  .sheet{position:fixed;left:0;right:0;bottom:0;background:var(--ground,Canvas);color:var(--ink,CanvasText);
    padding:22px max(20px,env(safe-area-inset-left)) calc(26px + env(safe-area-inset-bottom));border-radius:14px 14px 0 0;
    font-family:"Archivo",system-ui,sans-serif;animation:up .22s cubic-bezier(.2,.8,.2,1)}
  @media (min-width:700px){.sheet{left:auto;right:max(var(--gut,24px),calc((100vw - var(--maxw,1180px))/2));top:62px;bottom:auto;width:360px;border-radius:6px;
    box-shadow:0 1px 0 var(--hair,#ccc),0 18px 48px -12px color-mix(in srgb,#000 35%,transparent);animation:none}}
  @keyframes up{from{transform:translateY(24px);opacity:0}}
  @media (prefers-reduced-motion:reduce){.sheet{animation:none}}
  .sheet:focus{outline:none}
  h2{margin:0 0 14px;font-size:15px;font-weight:600;font-stretch:88%}
  .ask{appearance:none;flex:none;border:0;border-radius:999px;height:40px;margin:6px 0 6px 8px;padding:0 16px 0 12px;
    background:var(--ink,CanvasText);color:var(--ground,Canvas);font:inherit;font-stretch:90%;font-weight:560;font-size:15px;
    cursor:pointer;display:flex;align-items:center;gap:7px;white-space:nowrap;transition:transform .15s ease}
  .ask:active{transform:scale(.94)}
  .ask svg{width:17px;height:17px}
  .ask[hidden]{display:none}
  .asksheet{display:flex;flex-direction:column;max-height:min(70vh,560px)}
  @media (min-width:700px){.asksheet{width:420px}}
  .log{overflow-y:auto;display:flex;flex-direction:column;gap:12px;margin:0 0 14px;min-height:40px}
  .log p{margin:0;line-height:1.4}
  .log .you{color:var(--quiet,GrayText);font-stretch:90%;font-size:14px}
  .log .house{font-size:18px;letter-spacing:-.005em}
  .log .err{color:var(--alert,#c55);font-size:14px}
  .log .hint{color:var(--quiet,GrayText);font-size:14px;font-stretch:88%}
  form{display:flex;gap:8px;align-items:center}
  input{flex:1;min-width:0;font:inherit;font-size:17px;color:inherit;background:var(--well,#eee);border:1px solid transparent;border-radius:999px;padding:11px 16px;min-height:46px}
  input:focus{outline:none;border-color:var(--hair,#ccc)}
  .round{appearance:none;flex:none;width:46px;height:46px;border-radius:50%;border:1px solid var(--hair,#ccc);background:none;color:inherit;cursor:pointer;display:grid;place-items:center}
  .round svg{width:18px;height:18px}
  .round[hidden]{display:none}
  .round.send{background:var(--ink,#111);color:var(--ground,#fff);border-color:var(--ink,#111)}
  .round.live{background:var(--light,#ffd9a8);border-color:var(--light,#ffd9a8);color:var(--on-light,#111);animation:pulse 1.2s ease-in-out infinite}
  @keyframes pulse{50%{box-shadow:0 0 0 6px color-mix(in srgb,var(--light,#ffd9a8) 30%,transparent)}}
  @media (prefers-reduced-motion:reduce){.round.live{animation:none}}
  `;
  const MIC = `<svg viewBox="0 0 18 18" aria-hidden="true"><rect x="6.2" y="1.5" width="5.6" height="9.5" rx="2.8" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M3.5 8.5a5.5 5.5 0 0 0 11 0M9 14v2.5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`;
  const SEND = `<svg viewBox="0 0 18 18" aria-hidden="true"><path d="M9 15V3.5M4 8l5-5 5 5" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>`;

  const BACK = `<svg viewBox="0 0 18 18" aria-hidden="true"><path d="M11.5 3.5 6 9l5.5 5.5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;

  class AppBar extends HTMLElement {
    static get observedAttributes() { return ["home"]; }
    attributeChangedCallback() { this.sync(); }
    sync() {
      if (!this.shadowRoot) return;
      const home = this.hasAttribute("home");
      this.shadowRoot.querySelector(".back").hidden = home;
      this.shadowRoot.querySelector(".tabs").hidden = home;
      this.shadowRoot.querySelector(".home-title").hidden = !home;
    }
    connectedCallback() {
      if (this.shadowRoot) return;
      const root = this.attachShadow({mode: "open"});
      root.innerHTML = `<style>${css}</style>
        <div class="bar">
          <button class="back" aria-label="Home">${BACK}<span>Home</span></button>
          <div class="home-title" hidden>${this.getAttribute("home-title") || "Home"}</div>
          <div class="tabs"><slot name="tabs"></slot></div>
          <button class="ask" aria-label="Talk to the house" ${hass() ? "" : "hidden"}>${MIC}Talk</button></div>
        <dialog class="askd" aria-label="Ask the house"><div class="sheet asksheet" tabindex="-1">
          <h2>Ask the house</h2>
          <div class="log" aria-live="polite"><p class="hint">Ask anything, or tell the house what to do.</p></div>
          <form><input name="q" autocomplete="off" enterkeyhint="send" placeholder="Is Costco open?">
            <button type="button" class="round mic" aria-label="Speak">${MIC}</button>
            <button type="submit" class="round send" aria-label="Send">${SEND}</button></form>
        </div></dialog>`;
      root.querySelector(".back").addEventListener("click", () => {
        const ev = new CustomEvent("back", {cancelable: true});
        if (this.dispatchEvent(ev)) go(HOME);
      });
      this.setupAsk(root);
      this.sync();
    }
  }
  AppBar.prototype.setupAsk = function (root) {
    const d = root.querySelector(".askd"), log = root.querySelector(".log"), form = root.querySelector("form"),
          input = form.q, mic = root.querySelector(".mic");
    const esc = s => String(s).replace(/[&<>]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;"}[c]));
    const say = (cls, text) => { const p = document.createElement("p"); p.className = cls; p.innerHTML = esc(text); log.append(p); log.scrollTop = log.scrollHeight; return p; };
    d.addEventListener("click", e => { if (e.target === d) d.close(); });
    if (!canListen()) mic.hidden = true;   // no microphone over plain http; the keyboard's dictation still works
    let busy = false;
    form.addEventListener("submit", async e => {
      e.preventDefault();
      const q = input.value.trim(); if (!q || busy) return;
      busy = true; input.value = ""; log.querySelector(".hint")?.remove();
      say("you", q); const wait = say("house", "…");
      try {
        let answer = "";
        await runPipeline({start_stage: "intent", end_stage: "intent", input: {text: q}}, ev => { if (ev.type === "intent-end") answer = speechOf(ev); });
        wait.textContent = answer || "Done.";
      } catch (err) { wait.className = "err"; wait.textContent = err.message; }
      busy = false; input.focus();
    });
    const ui = {stop: null, stopped: () => { mic.classList.remove("live"); mic.setAttribute("aria-label", "Speak"); }};
    const talk = async () => {
      if (mic.classList.contains("live")) { ui.stop && ui.stop(); return; }
      if (busy) return;
      unlockVoice();
      busy = true; log.querySelector(".hint")?.remove();
      mic.classList.add("live"); mic.setAttribute("aria-label", "Stop listening");
      const you = say("you", "Listening…"); let wait = null;
      try {
        await listen(ev => {
          if (ev.type === "stt-end") { you.textContent = ev.data.stt_output.text || "(didn't catch that)"; wait = say("house", "…"); }
          if (ev.type === "intent-end" && wait) wait.textContent = speechOf(ev) || "Done.";
          if (ev.type === "tts-end" && ev.data.tts_output?.url) speak(ev.data.tts_output.url);
        }, ui);
      } catch (err) { (wait || you).className = "err"; (wait || you).textContent = err.message === "NOSECURE" ? "The microphone needs a secure (https) connection, and this one is plain http. For now, tap the text box and use the keyboard's microphone." : err.name === "NotAllowedError" ? "Microphone access is off for Home Assistant." : err.message; }
      busy = false;
    };
    mic.addEventListener("click", talk);
    // One tap to talk: the phone app's own Assist when we're inside it; otherwise listen right away
    // where the browser allows the microphone, and fall back to typing where it doesn't.
    root.querySelector(".ask").addEventListener("click", () => {
      const ext = nativeAssist();
      if (ext) { ext.fireMessage({type: "assist/show", payload: {pipeline_id: "preferred", start_listening: true}}); return; }
      d.showModal();
      if (canListen()) talk(); else input.focus();
    });
  };
  if (!customElements.get("app-bar")) customElements.define("app-bar", AppBar);
})();
