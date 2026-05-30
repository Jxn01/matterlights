from __future__ import annotations

from html import escape
import threading
import webbrowser

from flask import Flask, Response, jsonify, request

from matterlights.config import load_settings
from matterlights.home_assistant import HomeAssistantClient, LightUpdate
from matterlights.preview import activate_preview_override
from matterlights.screen import RgbColor, ScreenZone, capture_screen_png, load_configured_light_zones, save_light_zones


APP = Flask(__name__)
FLASH_DURATION_SECONDS = 2.5


@APP.get("/")
def index() -> str:
    return _page_html()


@APP.get("/api/config")
def get_config() -> Response:
    settings = load_settings()
    _, width, height = capture_screen_png(settings.screen_capture_target)
    zones = load_configured_light_zones(settings.light_zone_layout, settings.light_entities, settings.light_zone_file)
    return jsonify(
        {
            "captureTarget": settings.screen_capture_target,
            "zoneFile": str(settings.light_zone_file) if settings.light_zone_file is not None else "",
            "width": width,
            "height": height,
            "zones": [
                {
                    "entityId": entity_id,
                    "name": zone.name,
                    "left": zone.left,
                    "top": zone.top,
                    "right": zone.right,
                    "bottom": zone.bottom,
                }
                for entity_id, zone in zip(settings.light_entities, zones)
            ],
        }
    )


@APP.get("/api/screenshot")
def get_screenshot() -> Response:
    settings = load_settings()
    image_bytes, _, _ = capture_screen_png(settings.screen_capture_target)
    return Response(image_bytes, mimetype="image/png")


@APP.post("/api/save")
def save_config() -> Response:
    settings = load_settings()
    if settings.light_zone_file is None:
        return jsonify({"error": "LIGHT_ZONE_FILE is not configured"}), 400

    payload = request.get_json(force=True, silent=False)
    zone_entries = payload.get("zones", [])
    if len(zone_entries) != len(settings.light_entities):
        return jsonify({"error": "One zone is required for each configured light entity"}), 400

    zones: list[ScreenZone] = []
    for entity_id, entry in zip(settings.light_entities, zone_entries):
        if entry.get("entityId") != entity_id:
            return jsonify({"error": f"Zone/entity mismatch for {entity_id}"}), 400
        left = float(entry["left"])
        top = float(entry["top"])
        right = float(entry["right"])
        bottom = float(entry["bottom"])
        if not 0 <= left < right <= 1 or not 0 <= top < bottom <= 1:
            return jsonify({"error": f"Invalid bounds for {entity_id}"}), 400
        name = str(entry.get("name") or entity_id).strip()
        zones.append(ScreenZone(name=name, left=left, top=top, right=right, bottom=bottom))

    save_light_zones(settings.light_zone_file, settings.light_entities, zones)
    return jsonify({"saved": True, "zoneFile": str(settings.light_zone_file)})


@APP.post("/api/flash")
def flash_bulb() -> Response:
    settings = load_settings()
    payload = request.get_json(force=True, silent=False)
    entity_id = str(payload.get("entityId", "")).strip()
    if entity_id not in settings.light_entities:
        return jsonify({"error": f"Unknown light entity: {entity_id}"}), 400

    color_values = payload.get("color", [0, 255, 255])
    if not isinstance(color_values, list) or len(color_values) != 3:
        return jsonify({"error": "color must be an RGB array with 3 entries"}), 400

    flash_color = RgbColor(*[max(0, min(255, int(channel))) for channel in color_values])
    flash_brightness = max(1, min(255, int(payload.get("brightness", 255))))

    activate_preview_override(
        settings.preview_override_file,
        entity_id,
        flash_color,
        flash_brightness,
        FLASH_DURATION_SECONDS,
    )

    client = HomeAssistantClient(
        settings.ha_url,
        settings.ha_token,
        timeout_seconds=settings.request_timeout_seconds,
        inter_light_delay_seconds=0.0,
    )
    failed_entity_ids = client.apply_light_updates(
        [LightUpdate(entity_id=entity_id, color=flash_color, brightness=flash_brightness)],
        transition_seconds=0.0,
    )
    if failed_entity_ids:
        return jsonify({"error": f"Failed to flash {entity_id}"}), 409

    return jsonify(
        {
            "flashed": True,
            "entityId": entity_id,
            "durationSeconds": FLASH_DURATION_SECONDS,
        }
    )


def main() -> int:
    settings = load_settings()
    url = f"http://127.0.0.1:{settings.zone_ui_port}"
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    APP.run(host="127.0.0.1", port=settings.zone_ui_port, debug=False, use_reloader=False)
    return 0


def _page_html() -> str:
    title = escape("MatterLights Zone Designer")
    return f"""
<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>{title}</title>
  <style>
    :root {{
      --bg: #0a0f18;
      --panel: rgba(15, 23, 42, 0.78);
      --panel-strong: rgba(15, 23, 42, 0.92);
      --line: rgba(148, 163, 184, 0.22);
      --text: #e5edf8;
      --muted: #8ea3bd;
      --accent: #4fd1c5;
      --accent-2: #f59e0b;
      --shadow: 0 22px 60px rgba(0, 0, 0, 0.42);
    }}

    * {{ box-sizing: border-box; }}

    body {{
      margin: 0;
      font-family: "Segoe UI Variable", "Segoe UI", sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(79, 209, 197, 0.18), transparent 32%),
        radial-gradient(circle at bottom right, rgba(245, 158, 11, 0.16), transparent 28%),
        linear-gradient(180deg, #060912 0%, #0a0f18 100%);
      min-height: 100vh;
    }}

    .shell {{
      display: grid;
      grid-template-columns: 340px minmax(0, 1fr);
      min-height: 100vh;
    }}

    .sidebar {{
      padding: 28px 24px;
      background: var(--panel);
      border-right: 1px solid var(--line);
      backdrop-filter: blur(16px);
    }}

    h1 {{
      margin: 0 0 10px;
      font-size: 28px;
      letter-spacing: -0.03em;
    }}

    .subtle {{
      color: var(--muted);
      line-height: 1.55;
      margin: 0 0 18px;
    }}

    .actions {{
      display: flex;
      gap: 10px;
      margin: 18px 0 20px;
      flex-wrap: wrap;
    }}

    button {{
      border: 0;
      border-radius: 999px;
      padding: 11px 16px;
      font: inherit;
      color: #04121d;
      background: linear-gradient(135deg, #4fd1c5, #7dd3fc);
      cursor: pointer;
      font-weight: 600;
      box-shadow: 0 10px 28px rgba(79, 209, 197, 0.28);
    }}

    button.secondary {{
      color: var(--text);
      background: rgba(148, 163, 184, 0.16);
      box-shadow: none;
      border: 1px solid var(--line);
    }}

    .status {{
      min-height: 24px;
      color: var(--muted);
      margin-bottom: 18px;
      font-size: 14px;
    }}

    .bulb-list {{
      display: grid;
      gap: 10px;
    }}

    .bulb-card {{
      display: grid;
      grid-template-columns: 18px minmax(0, 1fr);
      gap: 12px;
      align-items: center;
      padding: 12px 14px;
      background: rgba(15, 23, 42, 0.6);
      border: 1px solid var(--line);
      border-radius: 16px;
    }}

    .swatch {{
      width: 18px;
      height: 18px;
      border-radius: 50%;
      box-shadow: 0 0 0 3px rgba(255,255,255,0.08);
    }}

    .bulb-name {{
      font-weight: 600;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}

    .bulb-meta {{
      color: var(--muted);
      font-size: 13px;
      margin-top: 2px;
    }}

    .bulb-actions {{
      margin-top: 10px;
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }}

    .bulb-actions button {{
      padding: 8px 12px;
      font-size: 13px;
      box-shadow: none;
    }}

    .canvas-wrap {{
      padding: 28px;
      display: grid;
      place-items: center;
    }}

    .stage-card {{
      width: min(1240px, 100%);
      background: var(--panel-strong);
      border: 1px solid var(--line);
      border-radius: 28px;
      padding: 18px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(18px);
    }}

    .stage-head {{
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 16px;
      margin-bottom: 16px;
    }}

    .stage-head strong {{ font-size: 18px; }}

    .stage-head span {{ color: var(--muted); font-size: 14px; }}

    .screen-frame {{
      position: relative;
      overflow: hidden;
      border-radius: 20px;
      border: 1px solid rgba(148, 163, 184, 0.16);
      background: #02050a;
      min-height: 320px;
    }}

    .screen-frame img {{
      display: block;
      width: 100%;
      height: auto;
      user-select: none;
      -webkit-user-drag: none;
    }}

    .zone-layer {{
      position: absolute;
      inset: 0;
    }}

    .zone {{
      position: absolute;
      border-radius: 16px;
      border: 2px solid currentColor;
      background: color-mix(in srgb, currentColor 18%, transparent);
      box-shadow: inset 0 0 0 1px rgba(255,255,255,0.14);
      min-width: 32px;
      min-height: 32px;
      touch-action: none;
    }}

    .zone-label {{
      position: absolute;
      left: 8px;
      top: 8px;
      padding: 6px 9px;
      border-radius: 999px;
      background: rgba(2, 6, 23, 0.72);
      color: white;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.01em;
      backdrop-filter: blur(10px);
      cursor: grab;
    }}

    .zone-handle {{
      position: absolute;
      right: 8px;
      bottom: 8px;
      width: 18px;
      height: 18px;
      border-radius: 50%;
      background: white;
      border: 3px solid currentColor;
      cursor: nwse-resize;
      box-shadow: 0 6px 14px rgba(0, 0, 0, 0.28);
    }}

    .hint {{
      margin-top: 18px;
      color: var(--muted);
      font-size: 14px;
    }}

    @media (max-width: 980px) {{
      .shell {{ grid-template-columns: 1fr; }}
      .sidebar {{ border-right: 0; border-bottom: 1px solid var(--line); }}
      .canvas-wrap {{ padding-top: 0; }}
    }}
  </style>
</head>
<body>
  <div class=\"shell\">
    <aside class=\"sidebar\">
      <h1>MatterLights Zone Designer</h1>
      <p class=\"subtle\">Drag each bulb over the part of the screen it should represent. Drag the white handle to resize it. Save updates the running sync loop live, and Flash helps you identify the selected bulb physically.</p>
      <div class=\"actions\">
        <button id=\"saveButton\">Save Layout</button>
        <button id=\"refreshButton\" class=\"secondary\">Refresh Screenshot</button>
      </div>
      <div id=\"status\" class=\"status\">Loading current layout…</div>
      <div id=\"bulbList\" class=\"bulb-list\"></div>
      <p class=\"hint\">Tip: the first bulb in your .env is not special anymore. Use this screen to place every bulb exactly where it sits in the room around your monitor.</p>
    </aside>
    <main class=\"canvas-wrap\">
      <section class=\"stage-card\">
        <div class=\"stage-head\">
          <strong>Live Screen Placement</strong>
          <span id=\"stageMeta\"></span>
        </div>
        <div id=\"screenFrame\" class=\"screen-frame\">
          <img id=\"screenshot\" alt=\"Current screen capture\">
          <div id=\"zoneLayer\" class=\"zone-layer\"></div>
        </div>
      </section>
    </main>
  </div>
  <script>
    const palette = ['#38bdf8', '#34d399', '#f59e0b', '#f472b6', '#a78bfa', '#f97316', '#22c55e', '#fb7185'];
    const screenshot = document.getElementById('screenshot');
    const zoneLayer = document.getElementById('zoneLayer');
    const bulbList = document.getElementById('bulbList');
    const status = document.getElementById('status');
    const stageMeta = document.getElementById('stageMeta');
    let config = null;
    let activeDrag = null;

    async function loadConfig(refreshImage = true) {{
      const response = await fetch('/api/config');
      config = await response.json();
      stageMeta.textContent = `${{config.captureTarget}} capture • ${{config.width}}×${{config.height}} • ${'{'}config.zoneFile ? config.zoneFile : 'preset layout'{'}'}`;
      if (refreshImage) {{
        screenshot.src = `/api/screenshot?ts=${{Date.now()}}`;
      }}
      renderBulbCards();
      renderZones();
      status.textContent = 'Drag the labels to move zones. Drag the white handle to resize.';
    }}

    function renderBulbCards() {{
      bulbList.innerHTML = '';
      config.zones.forEach((zone, index) => {{
        const card = document.createElement('div');
        card.className = 'bulb-card';
        card.innerHTML = `
          <div class=\"swatch\" style=\"background:${{palette[index % palette.length]}}\"></div>
          <div>
            <div class=\"bulb-name\">${{zone.entityId}}</div>
            <div class=\"bulb-meta\">${{zone.name}}</div>
            <div class=\"bulb-actions\">
              <button type=\"button\" class=\"secondary flash-button\">Flash</button>
            </div>
          </div>
        `;
        card.querySelector('.flash-button').addEventListener('click', () => flashBulb(index));
        bulbList.append(card);
      }});
    }}

    function renderZones() {{
      zoneLayer.innerHTML = '';
      config.zones.forEach((zone, index) => {{
        const el = document.createElement('div');
        el.className = 'zone';
        el.dataset.index = String(index);
        el.style.color = palette[index % palette.length];
        updateZoneStyle(el, zone);

        const label = document.createElement('div');
        label.className = 'zone-label';
        label.textContent = `${{index + 1}} • ${{zone.name}}`;
        label.addEventListener('pointerdown', (event) => beginDrag(event, index, 'move'));

        const handle = document.createElement('div');
        handle.className = 'zone-handle';
        handle.addEventListener('pointerdown', (event) => beginDrag(event, index, 'resize'));

        el.append(label, handle);
        zoneLayer.append(el);
      }});
    }}

    function updateZoneStyle(element, zone) {{
      element.style.left = `${{zone.left * 100}}%`;
      element.style.top = `${{zone.top * 100}}%`;
      element.style.width = `${{(zone.right - zone.left) * 100}}%`;
      element.style.height = `${{(zone.bottom - zone.top) * 100}}%`;
    }}

    function beginDrag(event, index, mode) {{
      event.preventDefault();
      const frameRect = document.getElementById('screenFrame').getBoundingClientRect();
      const zone = config.zones[index];
      activeDrag = {{
        index,
        mode,
        startX: event.clientX,
        startY: event.clientY,
        frameRect,
        origin: {{ ...zone }},
      }};
      window.addEventListener('pointermove', onPointerMove);
      window.addEventListener('pointerup', endDrag, {{ once: true }});
    }}

    function onPointerMove(event) {{
      if (!activeDrag) return;
      const dx = (event.clientX - activeDrag.startX) / activeDrag.frameRect.width;
      const dy = (event.clientY - activeDrag.startY) / activeDrag.frameRect.height;
      const zone = config.zones[activeDrag.index];
      if (activeDrag.mode === 'move') {{
        const width = activeDrag.origin.right - activeDrag.origin.left;
        const height = activeDrag.origin.bottom - activeDrag.origin.top;
        zone.left = clamp(activeDrag.origin.left + dx, 0, 1 - width);
        zone.top = clamp(activeDrag.origin.top + dy, 0, 1 - height);
        zone.right = zone.left + width;
        zone.bottom = zone.top + height;
      }} else {{
        zone.right = clamp(activeDrag.origin.right + dx, activeDrag.origin.left + 0.03, 1);
        zone.bottom = clamp(activeDrag.origin.bottom + dy, activeDrag.origin.top + 0.03, 1);
      }}
      updateZoneStyle(zoneLayer.children[activeDrag.index], zone);
    }}

    function endDrag() {{
      activeDrag = null;
      window.removeEventListener('pointermove', onPointerMove);
    }}

    function clamp(value, min, max) {{
      return Math.min(max, Math.max(min, value));
    }}

    async function saveLayout() {{
      status.textContent = 'Saving layout…';
      const response = await fetch('/api/save', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ zones: config.zones }}),
      }});
      const payload = await response.json();
      if (!response.ok) {{
        status.textContent = payload.error || 'Failed to save layout.';
        return;
      }}
      status.textContent = `Saved to ${{payload.zoneFile}}. The running sync loop will reload it automatically.`;
    }}

    async function flashBulb(index) {{
      const zone = config.zones[index];
      status.textContent = `Flashing ${{zone.entityId}}…`;
      const rgb = hexToRgb(palette[index % palette.length]);
      const response = await fetch('/api/flash', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{
          entityId: zone.entityId,
          color: rgb,
          brightness: 255,
        }}),
      }});
      const payload = await response.json();
      if (!response.ok) {{
        status.textContent = payload.error || `Failed to flash ${{zone.entityId}}.`;
        return;
      }}
      status.textContent = `Flashing ${{zone.entityId}} for ${{payload.durationSeconds}} seconds.`;
    }}

    function hexToRgb(hex) {{
      const normalized = hex.replace('#', '');
      return [
        Number.parseInt(normalized.slice(0, 2), 16),
        Number.parseInt(normalized.slice(2, 4), 16),
        Number.parseInt(normalized.slice(4, 6), 16),
      ];
    }}

    document.getElementById('saveButton').addEventListener('click', saveLayout);
    document.getElementById('refreshButton').addEventListener('click', () => loadConfig(true));

    loadConfig(true).catch((error) => {{
      status.textContent = error.message;
    }});
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())