import gradio as gr
import requests
from datetime import datetime

URL = "http://kool.to/mediahubmx-catalog.json"
HEADERS = {"User-Agent": "MediaHubMX/2", "Accept-Encoding": "gzip"}

BASE_PAYLOAD = {
    "language": "de", "region": "DE", "catalogId": "iptv", "id": "", "adult": False,
    "search": "", "sort": "name", "cursor": None, "clientVersion": "3.0.2"
}

COUNTRIES = {"Deutschland": "Germany", "Österreich": "Austria", "Schweiz": "Switzerland", "International": "International"}
FALLBACK_MSG = "⚠️ kool.to API antwortet aktuell nicht."

def clean(name):
    for s in [" HD", " FHD", " UHD", " 4K", " []", " 1", " .c", " .b", " .s"]:
        name = name.replace(s, "")
    return name.strip()

def fetch_playlist(country=None):
    payload = BASE_PAYLOAD.copy()
    if country:
        payload["filter"] = {"group": country}
    try:
        r = requests.post(URL, json=payload, headers=HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
        if "error" in data or not isinstance(data.get("items"), list):
            return [], "", FALLBACK_MSG

        m3u_lines = ['#EXTM3U', '#EXTVLCOPT:http-user-agent=MediaHubMX/2', f'# Vavuu IPTV – {country or "Alle"} – {datetime.now().strftime("%d.%m.%Y %H:%M")}']
        channels = []
        for item in data.get("items", []):
            if item.get("type") == "iptv":
                name = clean(item["name"])
                url = item["url"]
                group = item.get("group", "Sonstige")
                logo = item.get("logo", "")
                m3u_lines.append(f'#EXTINF:-1 tvg-logo="{logo}" group-title="{group}",{name}')
                m3u_lines.append(url)
                channels.append((name, url, logo or "https://i.imgur.com/6Nk2t.png"))
        return channels, "\n".join(m3u_lines), f"✅ {len(channels)} Kanäle geladen"
    except Exception as e:
        return [], "", f"❌ {str(e)} – {FALLBACK_MSG}"

with gr.Blocks(title="Vavuu IPTV") as demo:
    gr.HTML("""<div style="text-align:center; padding:30px;"><h1>📺 Vavuu IPTV</h1><p>Die schnellste & schönste kostenlose IPTV-App</p><p><small>Powered by kool.to</small></p></div>""")
    
    with gr.Row():
        btn_de = gr.Button("🇩🇪 Deutschland", scale=2)
        btn_at = gr.Button("🇦🇹 Österreich", scale=2)
        btn_ch = gr.Button("🇨🇭 Schweiz", scale=2)
    with gr.Row():
        btn_dach = gr.Button("🇩🇪🇦🇹🇨🇭 DACH", scale=2)
        btn_int = gr.Button("🌍 International", scale=2)
        btn_all = gr.Button("🗺️ Alle Kanäle", scale=2)

    status = gr.Textbox(label="Status", interactive=False, value="Klicke auf eine Schaltfläche")
    with gr.Row():
        gallery = gr.Gallery(label="Klicke auf Sender", columns=6, height=620, object_fit="contain")
        player = gr.Video(label="Live-Player", width=900, autoplay=True)
    m3u_output = gr.File(label="M3U herunterladen", visible=True)

    def load(country_input):
        country = COUNTRIES.get(country_input) if country_input in COUNTRIES else None
        channels, m3u, stat = fetch_playlist(country)
        if country_input == "DACH":
            all_ch = []
            for c in ["Germany", "Austria", "Switzerland"]:
                ch, _, _ = fetch_playlist(c)
                all_ch.extend(ch)
            channels = all_ch
            stat = f"✅ {len(channels)} Kanäle (DACH)"
        return channels, m3u, stat, gr.update(visible=bool(m3u))

    def play_channel(evt: gr.SelectData):
        return evt.value[1] if isinstance(evt.value, tuple) and len(evt.value) >= 2 else None

    btn_de.click(load, gr.State("Deutschland"), [gallery, m3u_output, status, m3u_output])
    btn_at.click(load, gr.State("Österreich"), [gallery, m3u_output, status, m3u_output])
    btn_ch.click(load, gr.State("Schweiz"), [gallery, m3u_output, status, m3u_output])
    btn_int.click(load, gr.State("International"), [gallery, m3u_output, status, m3u_output])
    btn_all.click(load, gr.State(None), [gallery, m3u_output, status, m3u_output])
    btn_dach.click(load, gr.State("DACH"), [gallery, m3u_output, status, m3u_output])

    gallery.select(play_channel, None, player)

if __name__ == "__main__":
    demo.launch(
        server_name="0.0.0.0",
        server_port=7860,
        theme=gr.themes.Soft(),
        css=".gradio-container {max-width: 1000px !important}", share=False)
