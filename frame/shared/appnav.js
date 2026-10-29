/* <app-bar>: the shared top bar for the house's own apps (House, Frame).
 *
 * One source of truth: this file lives in /shared and is copied into each app by
 * tools/sync-shared. Each app has a test that fails if its copy drifts.
 *
 * Usage:
 *   <app-bar app="house"><nav slot="tabs">...your tabs...</nav></app-bar>
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
  const APPS = [
    {id: "house", name: "House", path: "/3dd8a0cc_house", note: "Today's light, shortcuts, devices, activity"},
    {id: "frame", name: "Frame", path: "/3dd8a0cc_frame", note: "Art on the Frame, collections and its schedule"},
    {id: "ha", name: "Home Assistant", path: "/lovelace/0", note: "Everything else"},
  ];

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

  const css = `
  :host{display:block;position:sticky;top:0;z-index:20;background:var(--ground,Canvas);color:var(--ink,CanvasText);
    font-family:"Archivo",system-ui,sans-serif;font-feature-settings:"tnum" 1}
  .bar{display:flex;align-items:center;gap:12px;min-height:52px;border-bottom:1px solid var(--hair,color-mix(in srgb,currentColor 18%,transparent))}
  .tabs{flex:1;min-width:0;overflow-x:auto;scrollbar-width:none;-webkit-overflow-scrolling:touch}
  .tabs::-webkit-scrollbar{display:none}
  .apps{appearance:none;border:0;background:none;color:inherit;font:inherit;font-stretch:88%;font-weight:520;
    padding:10px 0 10px 12px;cursor:pointer;display:flex;align-items:center;gap:8px;white-space:nowrap}
  .apps svg{width:15px;height:15px}
  .apps:focus-visible,a:focus-visible,button:focus-visible{outline:2px solid currentColor;outline-offset:3px}
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
  ul{list-style:none;margin:0;padding:0}
  a{display:grid;grid-template-columns:1fr auto;gap:2px 12px;padding:12px 0;color:inherit;text-decoration:none;
    border-top:1px solid var(--hair,color-mix(in srgb,currentColor 15%,transparent))}
  a b{font-weight:540;font-size:19px;letter-spacing:-.01em}
  a span{grid-column:1;font-size:13.5px;font-stretch:86%;color:var(--quiet,GrayText)}
  a i{grid-row:1/3;grid-column:2;align-self:center;font-style:normal;font-size:13px;font-stretch:86%;color:var(--quiet,GrayText)}
  a:hover b{text-decoration:underline;text-underline-offset:4px;text-decoration-thickness:1px}
  `;

  class AppBar extends HTMLElement {
    connectedCallback() {
      if (this.shadowRoot) return;
      const here = this.getAttribute("app");
      const root = this.attachShadow({mode: "open"});
      root.innerHTML = `<style>${css}</style>
        <div class="bar"><div class="tabs"><slot name="tabs"></slot></div>
          <button class="apps" aria-haspopup="dialog">
            <svg viewBox="0 0 15 15" aria-hidden="true"><g fill="currentColor">${[2,7.5,13].flatMap(x=>[2,7.5,13].map(y=>`<circle cx="${x}" cy="${y}" r="1.35"/>`)).join("")}</g></svg>
            All apps</button></div>
        <dialog aria-label="All apps"><div class="sheet" tabindex="-1"><h2>All apps</h2><ul>
          ${APPS.map(a => `<li><a href="${a.path}" data-path="${a.path}" ${a.id === here ? 'aria-current="page"' : ""}><b>${a.name}</b><span>${a.note}</span>${a.id === here ? "<i>You're here</i>" : ""}</a></li>`).join("")}
        </ul></div></dialog>`;
      const dlg = root.querySelector("dialog");
      root.querySelector(".apps").addEventListener("click", () => { dlg.showModal(); root.querySelector(".sheet").focus(); });
      dlg.addEventListener("click", e => { if (e.target === dlg) dlg.close(); });
      root.querySelectorAll("a").forEach(a => a.addEventListener("click", e => {
        e.preventDefault(); dlg.close();
        if (a.getAttribute("aria-current") !== "page") go(a.dataset.path);
      }));
    }
  }
  if (!customElements.get("app-bar")) customElements.define("app-bar", AppBar);
})();
