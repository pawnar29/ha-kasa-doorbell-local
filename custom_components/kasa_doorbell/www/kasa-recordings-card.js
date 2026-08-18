/**
 * Kasa Camera — recordings card.
 *
 * Browses the footage on the camera's SD card: pick a day, filter by what triggered the
 * recording, click a clip to play it.
 *
 * Deliberately dependency-free (no Lit, no bundler): it is served straight out of the
 * integration, so a plain custom element keeps the whole thing one readable file.
 *
 * Playing a clip CAPTURES it first: the integration fetches it from the camera into a real
 * MP4 file and this card polls until it is ready, then plays that file. Clips used to be
 * streamed as an open-ended response, and every browser heuristic fought it — fragment size,
 * muxer buffering, "have I got enough to start", and a two-track stream refusing to begin
 * (which is why audio had to be dropped). A finished file has none of those problems: it
 * plays at once, seeks, and has sound. Fetching costs a few seconds because the camera sends
 * ~2.5x faster than realtime; re-watching is instant because clips are cached.
 *
 * The camera allows exactly ONE stream connection, so the live view is unavailable while a
 * clip is being fetched — but only during the fetch, not for the whole time you watch it.
 *
 * Config:
 *   type: custom:kasa-recordings-card
 *   entity: camera.doorbell     # optional; the only camera is used when omitted
 *   title: Doorbell recordings  # optional
 */

const TYPE_ORDER = ["RING", "PD", "MD", "AD", "BCD"];
const TYPE_ICONS = { RING: "🔔", PD: "🚶", MD: "👁", AD: "🔊", BCD: "👶" };


class KasaRecordingsCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._day = null;
    this._days = [];
    this._clips = [];
    this._active = new Set();   // empty = show everything
    this._playing = null;
    this._ready = null;
    this._preparing = null;
    this._prepareToken = null;
    this._error = null;
    this._loading = false;
    this._entryId = null;
    this._built = false;
  }

  setConfig(config) {
    this._config = config || {};
  }

  static getStubConfig() {
    return { entity: "" };
  }

  getCardSize() {
    return 12;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) {
      this._build();
      this._init();
    }
  }

  async _init() {
    try {
      // An explicit entity wins; otherwise use the only recordings-capable camera, so the
      // card works with no configuration at all in the common single-camera case.
      if (this._config.entity) {
        this._entryId = this._config.entity;
      } else {
        const cameras = await this._hass.callWS({
          type: "kasa_doorbell/recordings/cameras",
        });
        if (!cameras.length) throw new Error("No Kasa camera with recordings is set up");
        this._entryId = cameras[0].entry_id;
        this._name = cameras[0].name;
      }
      const info = await this._hass.callWS({
        type: "kasa_doorbell/recordings/days",
        entry_id: this._entryId,
      });
      this._days = info.days;
      this._day = info.days[0];
      await this._loadClips();
    } catch (err) {
      this._error = err.message || String(err);
      this._render();
    }
  }

  async _loadClips() {
    this._loading = true;
    this._error = null;
    this._render();
    try {
      this._clips = await this._hass.callWS({
        type: "kasa_doorbell/recordings/clips",
        entry_id: this._entryId,
        day: this._day,
      });
    } catch (err) {
      this._clips = [];
      this._error = err.message || String(err);
    }
    this._loading = false;
    this._render();
  }

  _visible() {
    if (!this._active.size) return this._clips;
    return this._clips.filter((c) => this._active.has(c.type));
  }

  _dayLabel(iso) {
    const today = new Date();
    const d = new Date(`${iso}T00:00:00`);
    const days = Math.round((today.setHours(0, 0, 0, 0) - d.getTime()) / 86400000);
    if (days === 0) return "Today";
    if (days === 1) return "Yesterday";
    return d.toLocaleDateString(undefined, {
      weekday: "long",
      month: "long",
      day: "numeric",
    });
  }

  async _play(clip) {
    this._playing = clip;
    this._ready = null;
    this._preparing = 0;
    this._render();

    // Ask the integration to capture the clip to a real MP4 first, and poll until it is
    // ready. This is the whole point of the design: once it is a finished file the browser
    // plays it instantly, seeks within it, and plays the sound — none of which worked while
    // the recording was streamed as an open-ended response.
    const token = {};
    this._prepareToken = token;
    for (;;) {
      let status;
      try {
        status = await this._hass.callWS({
          type: "kasa_doorbell/recordings/prepare",
          entry_id: this._entryId,
          seek: clip.seek,
          duration: clip.duration,
        });
      } catch (err) {
        if (this._prepareToken !== token) return;
        this._error = err.message || String(err);
        this._render();
        return;
      }
      if (this._prepareToken !== token) return;   // the user picked another clip
      if (status.error) {
        this._error = status.error;
        this._render();
        return;
      }
      if (status.ready) {
        this._preparing = null;
        this._ready = status.url;
        this._render();
        const video = this.shadowRoot.querySelector("video");
        if (video) {
          // Sound first — clicking a clip is a user gesture, so this is normally permitted.
          video.muted = false;
          video.play().catch(() => {
            video.muted = true;   // muted playback always is
            video.play().catch(() => {});
          });
        }
        return;
      }
      this._preparing = status.progress;
      this._render();
      await new Promise((r) => setTimeout(r, 700));
    }
  }

  _build() {
    this.shadowRoot.innerHTML = `
      <style>
        ha-card { display: flex; flex-direction: column; overflow: hidden; }
        .head {
          display: flex; align-items: center; gap: 12px;
          padding: 12px 16px 0 16px; flex-wrap: wrap;
        }
        .head h2 {
          margin: 0; font-size: 1.1rem; font-weight: 500; flex: 1 1 auto;
          color: var(--primary-text-color);
        }
        select {
          font: inherit; padding: 6px 8px; border-radius: 8px;
          color: var(--primary-text-color);
          background: var(--card-background-color, #fff);
          border: 1px solid var(--divider-color, #ccc);
        }
        .filters { display: flex; gap: 8px; flex-wrap: wrap; padding: 12px 16px 4px; }
        .chip {
          border: 1px solid var(--divider-color, #ccc); border-radius: 16px;
          padding: 4px 12px; font-size: 0.85rem; cursor: pointer; user-select: none;
          color: var(--primary-text-color); background: transparent; line-height: 1.6;
        }
        .chip[aria-pressed="true"] {
          background: var(--primary-color); color: var(--text-primary-color, #fff);
          border-color: var(--primary-color);
        }
        .chip .n { opacity: 0.65; margin-left: 6px; }
        video { width: 100%; background: #000; display: block; max-height: 60vh; }
        .now { padding: 8px 16px; font-size: 0.85rem; color: var(--secondary-text-color); }
        ul { list-style: none; margin: 0; padding: 0; overflow-y: auto; max-height: 22rem; }
        li {
          display: flex; align-items: center; gap: 12px; padding: 10px 16px;
          cursor: pointer; border-top: 1px solid var(--divider-color, #eee);
          color: var(--primary-text-color);
        }
        li:hover { background: var(--secondary-background-color, #f5f5f5); }
        li[aria-selected="true"] { background: var(--secondary-background-color, #eee); }
        li .icon { font-size: 1.1rem; width: 1.5rem; text-align: center; }
        li .time { font-variant-numeric: tabular-nums; font-weight: 500; }
        li .label { flex: 1 1 auto; color: var(--secondary-text-color); }
        li .dur { color: var(--secondary-text-color); font-size: 0.85rem; }
        .msg { padding: 24px 16px; text-align: center; color: var(--secondary-text-color); }
        .msg.err { color: var(--error-color, #b00); }
      </style>
      <ha-card><div id="root"></div></ha-card>
    `;
    this._built = true;
  }

  _render() {
    if (!this._built) return;
    const root = this.shadowRoot.getElementById("root");
    if (!root) return;

    if (this._error) {
      root.innerHTML = `<div class="msg err"></div>`;
      root.querySelector(".msg").textContent = this._error;
      return;
    }
    if (!this._day) {
      root.innerHTML = `<div class="msg">Loading recordings…</div>`;
      return;
    }

    const counts = {};
    for (const clip of this._clips) counts[clip.type] = (counts[clip.type] || 0) + 1;
    const present = TYPE_ORDER.filter((t) => counts[t]).concat(
      Object.keys(counts).filter((t) => !TYPE_ORDER.includes(t))
    );
    const visible = this._visible();

    root.innerHTML = `
      <div class="head">
        <h2></h2>
        <select id="day"></select>
      </div>
      <div class="filters" id="filters"></div>
      ${this._playing && this._ready
        ? `<video controls playsinline preload="auto" src="${this._ready}"></video>
           <div class="now"></div>`
        : ""}
      ${this._playing && this._preparing !== null && !this._ready
        ? `<div class="msg">Fetching clip from the camera… ${this._preparing}%</div>`
        : ""}
      ${this._loading ? `<div class="msg">Loading…</div>` : ""}
      ${!this._loading && !visible.length ? `<div class="msg">No recordings match</div>` : ""}
      <ul id="clips"></ul>
    `;

    root.querySelector("h2").textContent =
      this._config.title || `${this._name || "Camera"} recordings`;

    const day = root.querySelector("#day");
    for (const iso of this._days) {
      const opt = document.createElement("option");
      opt.value = iso;
      opt.textContent = this._dayLabel(iso);
      if (iso === this._day) opt.selected = true;
      day.appendChild(opt);
    }
    day.addEventListener("change", () => {
      this._day = day.value;
      this._playing = null;
      this._ready = null;
      this._preparing = null;
      this._prepareToken = null;
      this._loadClips();
    });

    const filters = root.querySelector("#filters");
    for (const type of present) {
      const chip = document.createElement("button");
      chip.className = "chip";
      chip.type = "button";
      chip.setAttribute("aria-pressed", this._active.has(type) ? "true" : "false");
      const label = (this._clips.find((c) => c.type === type) || {}).label || type;
      chip.textContent = `${TYPE_ICONS[type] || "•"} ${label}`;
      const n = document.createElement("span");
      n.className = "n";
      n.textContent = counts[type];
      chip.appendChild(n);
      chip.addEventListener("click", () => {
        // No chips selected means "everything" — the same as all chips selected, but it
        // reads better and makes clearing the filter one click.
        if (this._active.has(type)) this._active.delete(type);
        else this._active.add(type);
        this._render();
      });
      filters.appendChild(chip);
    }

    if (this._playing && this._ready) {
      const video = root.querySelector("video");
      root.querySelector(".now").textContent =
        `${this._playing.time} · ${this._playing.label}`;
    }

    const list = root.querySelector("#clips");
    for (const clip of visible) {
      const li = document.createElement("li");
      li.setAttribute("aria-selected", this._playing === clip ? "true" : "false");
      const icon = document.createElement("span");
      icon.className = "icon";
      icon.textContent = TYPE_ICONS[clip.type] || "•";
      const time = document.createElement("span");
      time.className = "time";
      time.textContent = clip.time;
      const label = document.createElement("span");
      label.className = "label";
      label.textContent = clip.label;
      const dur = document.createElement("span");
      dur.className = "dur";
      dur.textContent = clip.duration ? `${clip.duration}s` : "";
      li.append(icon, time, label, dur);
      li.addEventListener("click", () => this._play(clip));
      list.appendChild(li);
    }
  }
}

customElements.define("kasa-recordings-card", KasaRecordingsCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "kasa-recordings-card",
  name: "Kasa Camera Recordings",
  description: "Browse and play footage from a Kasa camera's SD card, filtered by event type.",
  preview: false,
});

console.info("%c KASA-RECORDINGS-CARD %c loaded ", "background:#03a9f4;color:#fff", "");
