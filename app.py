"""
KlipCuan — AI Faceless Affiliate Video Engine
=============================================
Streamlit MVP. 100% free stack:
  - Naskah      : Groq API / Google Gemini API (free tier)
  - Voiceover   : edge-tts (Microsoft Edge TTS, suara id-ID natural)
  - Komposisi   : Pillow (bikin frame 2160x3840 -> anti distorsi, blur bg, phone mockup)
  - Render      : FFmpeg (Ken Burns zoompan + xfade + subtitle ASS)

FFmpeg dipakai lewat binary mandiri dari package `imageio-ffmpeg` (bukan apt),
supaya tidak menarik seluruh stack GTK/X11/systemd yang biasanya jadi
dependency paket `ffmpeg` di Debian — itu bisa >250 package & 700 MB, gampang
bikin container di free-tier hosting kehabisan resource dan mati tanpa traceback.

Jalankan lokal : streamlit run app.py
Deploy         : Streamlit Community Cloud (packages.txt cuma butuh font)
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from typing import Any

import imageio_ffmpeg
import requests
import streamlit as st
from PIL import Image, ImageDraw, ImageFilter, ImageOps

# Init FFmpeg dibungkus try/except dan dipindah ke bawah st.set_page_config, supaya
# kalau gagal (mis. platform tidak didukung imageio-ffmpeg), errornya dipaksa tampil
# jelas lewat st.exception() di halaman app — bukan crash senyap sebelum Streamlit
# sempat merender apa pun (yang bisa muncul sebagai "Oh no." kosong tanpa traceback).
FFMPEG_BIN: str | None = None
FFMPEG_INIT_ERROR: Exception | None = None
try:
    FFMPEG_BIN = imageio_ffmpeg.get_ffmpeg_exe()
except Exception as _e:  # noqa: BLE001
    FFMPEG_INIT_ERROR = _e

try:
    import edge_tts
except ImportError:  # pragma: no cover
    edge_tts = None

try:


    # ──────────────────────────────────────────────────────────────────────────────
    # KONSTANTA
    # ──────────────────────────────────────────────────────────────────────────────

    OUT_W, OUT_H = 1080, 1920          # output final (9:16)
    SS = 2                              # supersample: komposisi digambar 2x lalu di-downscale
    CANVAS_W, CANVAS_H = OUT_W * SS, OUT_H * SS
    FPS = 30
    XFADE_DUR = 0.5                     # transisi lembut antar scene (detik)
    SCENE_PAD = 0.35                    # jeda napas setelah tiap kalimat
    TAIL_PAD = 0.9                      # ekor di scene terakhir biar gak kepotong

    GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
    GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    VOICES = {
        "Ardi (Pria — tegas, cocok hard-selling)": "id-ID-ArdiNeural",
        "Gadis (Wanita — hangat, cocok storytelling)": "id-ID-GadisNeural",
    }

    CONCEPTS = {
        "Aesthetic Minimalist": "minimalist",
        'Flashy / Hype "Racun Shopee"': "flashy",
        "Phone Screen Mockup / POV": "phone",
    }

    CONCEPT_BRIEF = {
        "minimalist": (
            "Tenang, elegan, kalem. Nada seperti teman yang seleranya bagus lagi merekomendasikan "
            "temuan bagus. Hindari kapital berlebihan dan kata alay."
        ),
        "flashy": (
            "Hype dan kejut ala racun Shopee. Boleh pakai kata gila, parah, buruan, gak nyangka. "
            "Heboh tapi tetap masuk akal, jangan sampai norak."
        ),
        "phone": (
            "POV personal, seolah lagi ngomong ke kamera sambil pegang HP. Kesannya pengalaman "
            "pribadi setelah checkout, bukan iklan."
        ),
    }


    # ──────────────────────────────────────────────────────────────────────────────
    # 1. NASKAH (LLM) — Affiliate-first
    # ──────────────────────────────────────────────────────────────────────────────

    # Model default per Agustus 2026. Jangan dihardcode di tempat lain —
    # daftar model ditarik live lewat list_models() supaya tidak mati saat provider
    # men-decommission model (seluruh keluarga Llama sudah dicabut dari GroqCloud).
    DEFAULT_MODELS = {"Groq": "openai/gpt-oss-120b", "Gemini": "gemini-2.5-flash"}

    # model id yang bukan chat/text — disaring dari dropdown
    _NON_TEXT = ("whisper", "tts", "orpheus", "guard", "embedding", "embed", "safeguard", "rerank")

    CTA_TYPES = {
        "Keranjang kuning (Shopee)": "arahkan ke keranjang kuning di pojok kiri bawah layar",
        "Link di bio": "arahkan ke link di bio",
        "Link dipin di komentar": "arahkan ke link yang dipin di kolom komentar",
    }

    ANGLES = {
        "Racun / impulsif": (
            "Posisikan produk sebagai racun yang bikin pengen checkout sekarang juga. "
            "Tekankan sensasi 'kenapa gue baru tahu ini', bukan spesifikasi teknis."
        ),
        "Masalah → solusi": (
            "Buka dengan keresahan sehari-hari yang sangat spesifik, baru tunjukkan produk "
            "sebagai penyelesaiannya."
        ),
        "Before → after": (
            "Kontraskan kondisi sebelum punya produk ini dengan sesudahnya. Fokus ke perubahan "
            "yang kelihatan, bukan ke fitur."
        ),
        "Hidden gem / underrated": (
            "Posisikan sebagai temuan langka yang belum banyak orang tahu. Nada seperti "
            "membocorkan rahasia."
        ),
        "Worth it gak sih": (
            "Buka dengan keraguan harga, lalu patahkan dengan perbandingan nilai yang masuk akal."
        ),
    }

    SCRIPT_SYSTEM = """Kamu copywriter video affiliate Shopee/TikTok Indonesia. Kamu HANYA menulis untuk afiliator pemula yang jualan produk orang lain dan dibayar komisi. Bukan untuk brand, bukan untuk korporat.

    Tugas: naskah voiceover video vertikal ±{target_dur} detik yang tujuannya SATU — bikin penonton klik dan checkout hari itu juga.

    STRUKTUR WAJIB, {n} scene berurutan:
    1. HOOK   - 2 detik pertama. Harus bikin jempol berhenti. Boleh pertanyaan menusuk, pernyataan mengejutkan, atau kontradiksi. DILARANG membuka dengan menyebut nama produk.
    2. PAIN   - keresahan sehari-hari yang sangat spesifik dan bisa dibayangkan. Bukan pain umum.
    3. SOLUSI - produknya masuk sebagai jalan keluar. Sebut satu cara kerja atau satu keunggulan konkret saja.
    4. BUKTI  - satu manfaat yang paling kelihatan hasilnya. Boleh sentuh harga/promo kalau ada datanya.
    5. CTA    - {cta}. Tegas, satu kalimat, kasih alasan buru-buru yang masuk akal (stok, promo, harga naik).
    Kalau scene lebih sedikit, gabungkan bagian tengah. HOOK dan CTA wajib utuh.

    ATURAN BAHASA (ketat):
    - Bahasa Indonesia percakapan sehari-hari anak TikTok. Boleh "gue/lo" atau "aku/kamu", pilih satu dan konsisten.
    - DILARANG bahasa iklan korporat: "solusi terbaik", "kualitas premium", "hadir untuk Anda", "produk unggulan", "tingkatkan produktivitas".
    - Setiap scene 12 sampai 20 kata. Satu tarikan napas, satu ide.
    - Naskah ini dibaca mesin TTS: TANPA emoji, tanda kurung, simbol, tanda seru beruntun, atau huruf kapital semua.
    - Tulis angka jadi huruf kalau pendek, contoh "empat puluh lima ribu", supaya TTS-nya natural.
    - Jangan sebut kata AI, naskah, konten, atau video ini.
    - Jangan janjikan penghasilan pasti, klaim medis, atau garansi yang tidak ada di data produk.
    - Jangan mengarang angka rating, jumlah terjual, atau testimoni yang tidak diberikan.

    ANGLE YANG DIPAKAI: {angle}
    GAYA VISUAL VIDEONYA: {style}

    Balas HANYA JSON valid, tanpa markdown, tanpa penjelasan, persis bentuk ini:
    {{"hook_text": "3 sampai 5 kata teks besar di layar", "scenes": [{{"narration": "kalimat voiceover"}}]}}"""


    def _extract_json(raw: str) -> dict[str, Any]:
        raw = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.DOTALL)
        raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if not m:
                raise ValueError("Model tidak mengembalikan JSON yang bisa dibaca.")
            return json.loads(m.group(0))


    def list_models(provider: str, api_key: str) -> list[str]:
        """Ambil daftar model aktif langsung dari provider (anti model-decommissioned)."""
        if not api_key:
            return []
        try:
            if provider == "Groq":
                r = requests.get(
                    "https://api.groq.com/openai/v1/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=20,
                )
                r.raise_for_status()
                ids = [m["id"] for m in r.json().get("data", [])]
            else:
                r = requests.get(
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    headers={"x-goog-api-key": api_key},
                    params={"pageSize": 200},
                    timeout=20,
                )
                r.raise_for_status()
                ids = [
                    m["name"].split("/")[-1]
                    for m in r.json().get("models", [])
                    if "generateContent" in m.get("supportedGenerationMethods", [])
                ]
            ids = [i for i in ids if not any(k in i.lower() for k in _NON_TEXT)]
            return sorted(set(ids))
        except Exception:
            return []


    def _call_groq(api_key: str, model: str, system: str, user: str) -> str:
        base = {
            "model": model,
            "temperature": 0.95,
            "max_tokens": 1400,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        # extras dilepas satu per satu kalau model tidak mendukungnya
        attempts = [
            {**base, "response_format": {"type": "json_object"}, "reasoning_effort": "low"},
            {**base, "response_format": {"type": "json_object"}},
            base,
        ]
        last = ""
        for payload in attempts:
            r = requests.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=90,
            )
            if r.status_code == 200:
                msg = r.json()["choices"][0]["message"]
                return msg.get("content") or msg.get("reasoning") or ""
            last = r.text[:400]
            if r.status_code != 400:
                break
        raise RuntimeError(f"Groq error: {last}")


    def _call_gemini(api_key: str, model: str, system: str, user: str) -> str:
        r = requests.post(
            GEMINI_URL.format(model=model),
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            json={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {
                    "temperature": 0.95,
                    "maxOutputTokens": 1400,
                    "responseMimeType": "application/json",
                },
            },
            timeout=90,
        )
        if r.status_code != 200:
            raise RuntimeError(f"Gemini error {r.status_code}: {r.text[:400]}")
        data = r.json()
        return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])


    def generate_script(
        provider: str,
        api_key: str,
        model: str,
        product: str,
        audience: str,
        price_note: str,
        concept: str,
        angle: str,
        cta: str,
        n_scenes: int,
        target_dur: int,
    ) -> tuple[str, list[str]]:
        system = SCRIPT_SYSTEM.format(
            n=n_scenes,
            style=CONCEPT_BRIEF[concept],
            angle=ANGLES[angle],
            cta=CTA_TYPES[cta],
            target_dur=target_dur,
        )
        user = (
            f"PRODUK:\n{product.strip()}\n\n"
            f"TARGET PENONTON:\n{audience.strip() or 'pengguna Shopee Indonesia umur 18-30'}\n\n"
            f"HARGA / PROMO:\n{price_note.strip() or 'tidak disebutkan, jangan mengarang angka'}"
        )

        raw = (_call_groq if provider == "Groq" else _call_gemini)(api_key, model, system, user)
        data = _extract_json(raw)

        scenes = [
            str(s.get("narration", "")).strip()
            for s in data.get("scenes", [])
            if str(s.get("narration", "")).strip()
        ]
        if not scenes:
            raise ValueError("Naskah kosong. Coba generate ulang atau ganti model.")

        hook_text = str(data.get("hook_text", "")).strip()[:40]
        return hook_text, scenes[:n_scenes]


    # ──────────────────────────────────────────────────────────────────────────────
    # 2. VOICEOVER (edge-tts) + word timings untuk subtitle
    # ──────────────────────────────────────────────────────────────────────────────

    @dataclass
    class SceneAudio:
        mp3: bytes
        words: list[dict] = field(default_factory=list)  # {text, start, end} detik


    async def _tts_one(text: str, voice: str, rate: str, pitch: str) -> SceneAudio:
        comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
        buf = bytearray()
        words: list[dict] = []
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                buf.extend(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start = chunk["offset"] / 1e7
                words.append(
                    {"text": chunk["text"], "start": start, "end": start + chunk["duration"] / 1e7}
                )
        if not buf:
            raise RuntimeError("edge-tts tidak mengembalikan audio. Cek koneksi / teks naskah.")
        return SceneAudio(bytes(buf), words)


    async def _tts_all(texts: list[str], voice: str, rate: str, pitch: str) -> list[SceneAudio]:
        return [await _tts_one(t, voice, rate, pitch) for t in texts]


    def synthesize(texts: list[str], voice: str, rate: str, pitch: str) -> list[SceneAudio]:
        if edge_tts is None:
            raise RuntimeError("Library edge-tts belum terpasang. Jalankan: pip install edge-tts")
        return asyncio.run(_tts_all(texts, voice, rate, pitch))


    # ──────────────────────────────────────────────────────────────────────────────
    # 3. KOMPOSISI FRAME (Pillow) — anti distorsi, blur background, phone mockup
    # ──────────────────────────────────────────────────────────────────────────────

    def _mix(a: tuple, b: tuple, t: float) -> tuple:
        return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


    def _palette(img: Image.Image, n: int = 4) -> list[tuple]:
        small = img.convert("RGB").resize((80, 80))
        q = small.quantize(colors=n, method=Image.Quantize.MEDIANCUT)
        pal = q.getpalette() or []
        counts = sorted(q.getcolors() or [], reverse=True)
        out = [tuple(pal[i * 3 : i * 3 + 3]) for _, i in counts if len(pal) >= i * 3 + 3]
        return out or [(120, 120, 130)]


    def _vertical_gradient(size: tuple[int, int], top: tuple, bottom: tuple) -> Image.Image:
        w, h = size
        strip = Image.new("RGB", (1, h))
        px = strip.load()
        for y in range(h):
            px[0, y] = _mix(top, bottom, y / max(1, h - 1))
        return strip.resize((w, h), Image.BILINEAR)


    def _cover(img: Image.Image, w: int, h: int) -> Image.Image:
        return ImageOps.fit(img.convert("RGB"), (w, h), method=Image.LANCZOS, centering=(0.5, 0.4))


    def _contain(img: Image.Image, w: int, h: int) -> Image.Image:
        im = img.convert("RGB").copy()
        im.thumbnail((w, h), Image.LANCZOS)
        return im


    def _rounded(img: Image.Image, radius: int) -> Image.Image:
        im = img.convert("RGBA")
        mask = Image.new("L", im.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, im.size[0] - 1, im.size[1] - 1], radius, fill=255)
        im.putalpha(mask)
        return im


    def _shadow(size: tuple[int, int], radius: int, blur: int, alpha: int = 120) -> Image.Image:
        w, h = size
        pad = blur * 3
        layer = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
        ImageDraw.Draw(layer).rounded_rectangle(
            [pad, pad, pad + w, pad + h], radius, fill=(0, 0, 0, alpha)
        )
        return layer.filter(ImageFilter.GaussianBlur(blur))


    def _blur_backdrop(img: Image.Image, w: int, h: int, sigma: int, darken: float) -> Image.Image:
        """Blur di resolusi kecil lalu upscale — jauh lebih cepat & hasilnya lebih halus."""
        small = _cover(img, w // 6, h // 6).filter(ImageFilter.GaussianBlur(sigma))
        bg = small.resize((w, h), Image.BICUBIC)
        if darken > 0:
            dark = Image.new("RGB", (w, h), (0, 0, 0))
            bg = Image.blend(bg, dark, darken)
        return bg


    def _bottom_scrim(w: int, h: int, frac: float = 0.40, max_alpha: int = 165) -> Image.Image:
        """Gradasi gelap di bawah supaya subtitle selalu terbaca tanpa terlihat 'ditempel'."""
        sh = int(h * frac)
        strip = Image.new("L", (1, sh))
        px = strip.load()
        for y in range(sh):
            px[0, y] = int(max_alpha * ((y / max(1, sh - 1)) ** 1.7))
        alpha = strip.resize((w, sh), Image.BILINEAR)
        band = Image.new("RGBA", (w, sh), (0, 0, 0, 255))
        band.putalpha(alpha)
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        layer.paste(band, (0, h - sh), band)
        return layer


    def _grain(w: int, h: int, strength: int = 7) -> Image.Image:
        """Noise halus. Ini yang bikin frame gak terlihat 'flat hasil generator'."""
        noise = Image.effect_noise((w // 3, h // 3), 26).convert("L").resize((w, h), Image.BILINEAR)
        alpha = noise.point(lambda v: int(abs(v - 128) / 128 * strength))
        layer = Image.new("RGBA", (w, h), (255, 255, 255, 0))
        layer.putalpha(alpha)
        return layer


    def compose_minimalist(photo: Image.Image) -> Image.Image:
        W, H = CANVAS_W, CANVAS_H
        pal = _palette(photo)
        base = pal[0]
        top = _mix(base, (255, 255, 255), 0.72)
        bot = _mix(base, (255, 255, 255), 0.34)
        canvas = _vertical_gradient((W, H), top, bot).convert("RGBA")

        box_w, box_h = int(W * 0.80), int(H * 0.60)
        fg = _contain(photo, box_w, box_h)
        radius = int(W * 0.035)
        x = (W - fg.width) // 2
        y = int(H * 0.20)

        sh = _shadow(fg.size, radius, blur=int(W * 0.020), alpha=90)
        canvas.alpha_composite(sh, (x - sh.width // 2 + fg.width // 2, y - sh.height // 2 + fg.height // 2 + int(H * 0.008)))
        canvas.alpha_composite(_rounded(fg, radius), (x, y))

        canvas.alpha_composite(_bottom_scrim(W, H, 0.34, 120))
        return canvas.convert("RGB")


    def compose_flashy(photo: Image.Image) -> Image.Image:
        W, H = CANVAS_W, CANVAS_H
        canvas = _blur_backdrop(photo, W, H, sigma=26, darken=0.55).convert("RGBA")

        pal = _palette(photo)
        accent = _mix(pal[0], (255, 90, 0), 0.55)

        box_w, box_h = int(W * 0.90), int(H * 0.58)
        fg = _contain(photo, box_w, box_h)
        radius = int(W * 0.022)
        x = (W - fg.width) // 2
        y = int(H * 0.19)

        # garis aksen tipis di belakang foto — bikin frame terasa "didesain"
        bar = Image.new("RGBA", (W, int(H * 0.012)), (*accent, 235))
        canvas.alpha_composite(bar, (0, y - int(H * 0.035)))

        sh = _shadow(fg.size, radius, blur=int(W * 0.024), alpha=170)
        canvas.alpha_composite(sh, (x - sh.width // 2 + fg.width // 2, y - sh.height // 2 + fg.height // 2))

        framed = Image.new("RGBA", (fg.width + 14, fg.height + 14), (0, 0, 0, 0))
        ImageDraw.Draw(framed).rounded_rectangle(
            [0, 0, framed.width - 1, framed.height - 1], radius + 7, fill=(*accent, 255)
        )
        framed.alpha_composite(_rounded(fg, radius), (7, 7))
        canvas.alpha_composite(framed, (x - 7, y - 7))

        canvas.alpha_composite(_bottom_scrim(W, H, 0.42, 185))
        return canvas.convert("RGB")


    def compose_phone(photo: Image.Image) -> Image.Image:
        W, H = CANVAS_W, CANVAS_H
        canvas = _blur_backdrop(photo, W, H, sigma=30, darken=0.42).convert("RGBA")

        body_w = int(W * 0.60)
        body_h = int(body_w * 2.02)
        bx = (W - body_w) // 2
        by = int(H * 0.13)
        radius = int(body_w * 0.11)
        bezel = max(10, int(body_w * 0.022))

        sh = _shadow((body_w, body_h), radius, blur=int(W * 0.026), alpha=150)
        canvas.alpha_composite(sh, (bx - sh.width // 2 + body_w // 2, by - sh.height // 2 + body_h // 2))

        body = Image.new("RGBA", (body_w, body_h), (0, 0, 0, 0))
        d = ImageDraw.Draw(body)
        d.rounded_rectangle([0, 0, body_w - 1, body_h - 1], radius, fill=(26, 26, 30, 255))
        d.rounded_rectangle([0, 0, body_w - 1, body_h - 1], radius, outline=(105, 108, 118, 255), width=max(3, bezel // 6))

        sw, sh_ = body_w - bezel * 2, body_h - bezel * 2
        screen = _rounded(_cover(photo, sw, sh_), max(4, radius - bezel))
        body.alpha_composite(screen, (bezel, bezel))

        # dynamic island
        isl_w, isl_h = int(body_w * 0.30), int(body_w * 0.075)
        ImageDraw.Draw(body).rounded_rectangle(
            [(body_w - isl_w) // 2, bezel + int(body_w * 0.028),
             (body_w + isl_w) // 2, bezel + int(body_w * 0.028) + isl_h],
            isl_h // 2, fill=(8, 8, 10, 255),
        )

        canvas.alpha_composite(body, (bx, by))
        canvas.alpha_composite(_bottom_scrim(W, H, 0.38, 170))
        return canvas.convert("RGB")


    COMPOSERS = {"minimalist": compose_minimalist, "flashy": compose_flashy, "phone": compose_phone}


    def build_frame(photo: Image.Image, concept: str, add_grain: bool = True) -> Image.Image:
        frame = COMPOSERS[concept](photo)
        if add_grain:
            rgba = frame.convert("RGBA")
            rgba.alpha_composite(_grain(CANVAS_W, CANVAS_H, 6))
            frame = rgba.convert("RGB")
        return frame


    # ──────────────────────────────────────────────────────────────────────────────
    # 4. SUBTITLE (ASS) — timing dari word boundary edge-tts
    # ──────────────────────────────────────────────────────────────────────────────

    ASS_STYLES = {
        # BorderStyle 3 = opaque box; warna box diambil dari OutlineColour (&HAABBGGRR)
        "minimalist": {
            "sub": "Style: Sub,DejaVu Sans,56,&H00FFFFFF,&H00FFFFFF,&H96101014,&H00000000,-1,0,0,0,100,100,1,0,3,14,0,2,80,80,300,1",
            "hook": "Style: Hook,DejaVu Sans,74,&H00FFFFFF,&H00FFFFFF,&H96101014,&H00000000,-1,0,0,0,100,100,2,0,3,16,0,8,80,80,240,1",
        },
        "flashy": {
            "sub": "Style: Sub,DejaVu Sans,62,&H00FFFFFF,&H00FFFFFF,&H00000000,&HA0000000,-1,0,0,0,100,100,1,0,1,6,3,2,80,80,310,1",
            "hook": "Style: Hook,DejaVu Sans,82,&H0000E5FF,&H0000E5FF,&H00000000,&HA0000000,-1,0,0,0,100,100,2,0,1,7,4,8,70,70,230,1",
        },
        "phone": {
            "sub": "Style: Sub,DejaVu Sans,56,&H00FFFFFF,&H00FFFFFF,&H8C000000,&H00000000,-1,0,0,0,100,100,1,0,3,14,0,2,80,80,290,1",
            "hook": "Style: Hook,DejaVu Sans,70,&H00FFFFFF,&H00FFFFFF,&H8C000000,&H00000000,-1,0,0,0,100,100,2,0,3,15,0,8,80,80,230,1",
        },
    }


    def _ts(t: float) -> str:
        t = max(0.0, t)
        h, rem = divmod(t, 3600)
        m, s = divmod(rem, 60)
        return f"{int(h)}:{int(m):02d}:{s:05.2f}"


    def _chunk_words(words: list[dict], max_words: int = 4, max_chars: int = 26) -> list[list[dict]]:
        chunks: list[list[dict]] = []
        cur: list[dict] = []
        for w in words:
            cand = cur + [w]
            if cur and (len(cand) > max_words or len(" ".join(x["text"] for x in cand)) > max_chars):
                chunks.append(cur)
                cur = [w]
            else:
                cur = cand
        if cur:
            chunks.append(cur)
        return chunks


    def _esc(t: str) -> str:
        return t.replace("{", "(").replace("}", ")").replace("\n", " ").strip()


    def build_ass(
        scenes: list[SceneAudio],
        starts: list[float],
        concept: str,
        hook_text: str,
        hook_dur: float,
    ) -> str:
        styles = ASS_STYLES[concept]
        lines = [
            "[Script Info]",
            "ScriptType: v4.00+",
            f"PlayResX: {OUT_W}",
            f"PlayResY: {OUT_H}",
            "WrapStyle: 2",
            "ScaledBorderAndShadow: yes",
            "YCbCr Matrix: TV.709",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
            "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
            "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            styles["sub"],
            styles["hook"],
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        ]

        if hook_text:
            lines.append(
                f"Dialogue: 0,{_ts(0.15)},{_ts(hook_dur)},Hook,,0,0,0,,"
                f"{{\\fad(220,220)}}{_esc(hook_text.upper())}"
            )

        for sa, base in zip(scenes, starts):
            for grp in _chunk_words(sa.words):
                st_ = base + grp[0]["start"]
                en_ = base + grp[-1]["end"] + 0.10
                txt = _esc(" ".join(w["text"] for w in grp))
                if not txt:
                    continue
                lines.append(f"Dialogue: 0,{_ts(st_)},{_ts(en_)},Sub,,0,0,0,,{{\\fad(90,90)}}{txt}")

        return "\n".join(lines) + "\n"


    # ──────────────────────────────────────────────────────────────────────────────
    # 5. RENDER (FFmpeg)
    # ──────────────────────────────────────────────────────────────────────────────

    def _run(cmd: list[str], cwd: str) -> subprocess.CompletedProcess:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


    def scene_duration(sa: "SceneAudio", text: str) -> float:
        """Durasi audio per scene, dari timestamp WordBoundary edge-tts sendiri —
        tidak butuh ffprobe sama sekali. Fallback estimasi kasar kalau TTS tidak
        mengembalikan word boundary (jarang, teks terlalu pendek/aneh)."""
        if sa.words:
            return max(w["end"] for w in sa.words)
        return max(1.2, len(text.split()) / 2.3)  # ~2.3 kata/detik, estimasi aman


    def build_audio(workdir: str, n: int, pads: list[float]) -> str:
        """Gabung mp3 tiap scene + jeda napas jadi satu track WAV."""
        cmd = [FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error"]
        for i in range(n):
            cmd += ["-i", f"sc{i}.mp3"]
        parts = [f"[{i}:a]aresample=44100,apad=pad_dur={pads[i]:.3f}[a{i}];" for i in range(n)]
        joined = "".join(f"[a{i}]" for i in range(n))
        filt = "".join(parts) + f"{joined}concat=n={n}:v=0:a=1[aout]"
        cmd += ["-filter_complex", filt, "-map", "[aout]", "-c:a", "pcm_s16le", "-ar", "44100", "voice.wav"]
        p = _run(cmd, workdir)
        if p.returncode != 0:
            raise RuntimeError(f"Gagal merakit audio:\n{p.stderr[-800:]}")
        return "voice.wav"


    def _zoompan(idx: int, seg_dur: float) -> str:
        """Ken Burns lembut. Arah zoom selang-seling supaya gak monoton."""
        frames = max(2, int(round(seg_dur * FPS)))
        zmax = 1.10
        step = (zmax - 1.0) / frames
        xy = "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        if idx % 2 == 0:
            z = f"z='min(pzoom+{step:.6f},{zmax})'"
        else:
            z = f"z='if(eq(on,0),{zmax},max(pzoom-{step:.6f},1.001))'"
        return (
            f"[{idx}:v]scale={CANVAS_W}:{CANVAS_H},"
            f"zoompan={z}:{xy}:d=1:s={OUT_W}x{OUT_H}:fps={FPS},"
            f"setsar=1,format=yuv420p[v{idx}]"
        )


    def render_video(
        workdir: str,
        n: int,
        scene_durs: list[float],
        audio_file: str,
        ass_file: str,
        use_transitions: bool,
        crf: int = 21,
    ) -> str:
        total = sum(scene_durs)
        xf = XFADE_DUR if (use_transitions and n > 1) else 0.0
        seg_durs = [d + (xf if i < n - 1 else 0.0) for i, d in enumerate(scene_durs)]

        cmd = [FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error"]
        for i in range(n):
            cmd += ["-loop", "1", "-framerate", str(FPS), "-t", f"{seg_durs[i]:.3f}", "-i", f"frame{i}.png"]
        cmd += ["-i", audio_file]

        chains = [_zoompan(i, seg_durs[i]) for i in range(n)]

        if n == 1:
            last = "[v0]"
        elif xf > 0:
            prev = "[v0]"
            acc = 0.0
            for i in range(1, n):
                acc += scene_durs[i - 1]
                out = f"[x{i}]"
                chains.append(
                    f"{prev}[v{i}]xfade=transition=fade:duration={xf}:offset={acc:.3f}{out}"
                )
                prev = out
            last = prev
        else:
            joined = "".join(f"[v{i}]" for i in range(n))
            chains.append(f"{joined}concat=n={n}:v=1:a=0[cv]")
            last = "[cv]"

        chains.append(f"{last}ass=filename={ass_file}[vout]")
        filt = ";".join(chains)

        cmd += [
            "-filter_complex", filt,
            "-map", "[vout]", "-map", f"{n}:a",
            "-t", f"{total:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
            "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", str(FPS),
            "-movflags", "+faststart",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
            "output.mp4",
        ]

        p = _run(cmd, workdir)
        if p.returncode != 0:
            raise RuntimeError(p.stderr[-1500:])
        return "output.mp4"


    # ──────────────────────────────────────────────────────────────────────────────
    # 6. PIPELINE
    # ──────────────────────────────────────────────────────────────────────────────

    @st.cache_resource(show_spinner=False)
    def _ffmpeg_supports_ass() -> tuple[bool, str]:
        """Cek dukungan libass. SENGAJA lazy (dipanggil dari produce(), bukan di halaman
        awal) — subprocess FFmpeg yang jalan sebelum app selesai boot bisa bikin
        health-check Streamlit Cloud timeout dan keluar 'Oh no.' platform-level."""
        try:
            out = subprocess.run([FFMPEG_BIN, "-hide_banner", "-filters"],
                                 capture_output=True, text=True, timeout=20)
            ok = " ass " in out.stdout or "\nass" in out.stdout
            return ok, out.stdout[-500:] if not ok else ""
        except Exception as e:
            return False, str(e)

    def produce(
        photos: list[Image.Image],
        concept: str,
        hook_text: str,
        narrations: list[str],
        voice: str,
        rate: str,
        pitch: str,
        add_grain: bool,
        transitions: bool,
        progress,
    ) -> bytes:
        ass_ok, ass_detail = _ffmpeg_supports_ass()
        if not ass_ok:
            raise RuntimeError(
                "Binary FFmpeg dari imageio-ffmpeg di server ini tidak dikompilasi dengan "
                "libass (dibutuhkan buat burn-in subtitle). Coba pin imageio-ffmpeg==0.4.9 "
                "di requirements.txt, atau tambahkan `ffmpeg` lagi ke packages.txt.\n"
                f"Detail: {ass_detail}"
            )

        workdir = tempfile.mkdtemp(prefix="klipcuan_")
        try:
            n = len(narrations)

            progress(0.15, "Bikin voiceover natural (edge-tts)…")
            audios = synthesize(narrations, voice, rate, pitch)
            for i, sa in enumerate(audios):
                with open(os.path.join(workdir, f"sc{i}.mp3"), "wb") as f:
                    f.write(sa.mp3)

            progress(0.35, "Menyusun timeline & subtitle…")
            pads = [SCENE_PAD] * n
            pads[-1] = TAIL_PAD
            raw_durs = [scene_duration(audios[i], narrations[i]) for i in range(n)]
            scene_durs = [raw_durs[i] + pads[i] for i in range(n)]
            starts, acc = [], 0.0
            for d in scene_durs:
                starts.append(acc)
                acc += d

            build_audio(workdir, n, pads)

            hook_dur = min(3.2, max(1.8, scene_durs[0] - 0.4))
            with open(os.path.join(workdir, "subs.ass"), "w", encoding="utf-8") as f:
                f.write(build_ass(audios, starts, concept, hook_text, hook_dur))

            progress(0.55, "Menyusun frame visual (anti-distorsi)…")
            for i in range(n):
                frame = build_frame(photos[i % len(photos)], concept, add_grain)
                frame.save(os.path.join(workdir, f"frame{i}.png"), "PNG", optimize=False)

            progress(0.72, "Render video 9:16 dengan FFmpeg… (paling lama di sini)")
            try:
                render_video(workdir, n, scene_durs, "voice.wav", "subs.ass", transitions)
            except RuntimeError:
                # fallback: tanpa xfade (lebih ringan & lebih toleran ke build ffmpeg lama)
                st.info("Transisi halus gagal di server ini — otomatis dialihkan ke potongan biasa.")
                render_video(workdir, n, scene_durs, "voice.wav", "subs.ass", False)

            progress(0.98, "Membungkus MP4…")
            with open(os.path.join(workdir, "output.mp4"), "rb") as f:
                return f.read()
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


    # ──────────────────────────────────────────────────────────────────────────────
    # 7. UI
    # ──────────────────────────────────────────────────────────────────────────────

    st.set_page_config(page_title="KlipCuan — Screenshot Shopee jadi Video Keranjang Kuning",
                       page_icon="🛒", layout="centered")

    st.markdown(
        """
    <style>
      .block-container {padding-top: 2.4rem; max-width: 800px;}
      h1 {letter-spacing:-.02em;}
      .kc-sub {color:#8b8f98; font-size:.95rem; margin-top:-.6rem;}
      div.stButton > button {width:100%; height:3rem; font-weight:600; border-radius:.6rem;}
      .kc-step {font-size:.78rem; letter-spacing:.09em; text-transform:uppercase;
                color:#9aa0aa; font-weight:700; margin:1.6rem 0 .3rem;}
    </style>
    """,
        unsafe_allow_html=True,
    )

    st.title("KlipCuan")
    st.markdown(
        '<p class="kc-sub">Screenshot Shopee → video keranjang kuning. Khusus afiliator, bukan tools video umum.</p>',
        unsafe_allow_html=True,
    )

    if FFMPEG_INIT_ERROR is not None:
        st.error("Gagal menyiapkan FFmpeg (imageio-ffmpeg). Detail error di bawah:")
        st.exception(FFMPEG_INIT_ERROR)
        st.stop()

    if not FFMPEG_BIN or not os.path.exists(FFMPEG_BIN):
        st.error(f"Binary FFmpeg tidak ditemukan di path `{FFMPEG_BIN}`. Coba `pip install --force-reinstall imageio-ffmpeg`.")
        st.stop()

    # Cek dukungan libass SENGAJA tidak dijalankan di sini (halaman awal) — memanggil
    # subprocess FFmpeg sebelum app selesai boot bisa bikin health-check Streamlit Cloud
    # timeout dan malah keluar "Oh no." platform-level yang gak ada hubungannya sama kode
    # kita. Cek ini dijalankan belakangan, lazy, pas user klik Generate Video (lihat produce()).


    ss = st.session_state
    ss.setdefault("hook_text", "")
    ss.setdefault("narrations", [])
    ss.setdefault("video", None)


    @st.cache_data(show_spinner=False, ttl=1800)
    def cached_models(provider: str, key: str) -> list[str]:
        return list_models(provider, key)


    with st.sidebar:
        st.subheader("Konfigurasi AI")
        provider = st.radio("Penyedia naskah", ["Groq", "Gemini"], horizontal=True)
        secret_key = st.secrets.get("GROQ_API_KEY" if provider == "Groq" else "GEMINI_API_KEY", "")
        api_key = st.text_input("API Key", value=secret_key, type="password",
                                help="Groq: console.groq.com  •  Gemini: aistudio.google.com")

        fallback = DEFAULT_MODELS[provider]
        available = cached_models(provider, api_key) if api_key else []
        c1, c2 = st.columns([4, 1])
        with c1:
            if available:
                idx = available.index(fallback) if fallback in available else 0
                model = st.selectbox("Model (live dari API)", available, index=idx)
            else:
                model = st.text_input("Model", value=fallback)
        with c2:
            st.write("")
            if st.button("🔄", help="Muat ulang daftar model"):
                cached_models.clear()
                st.rerun()
        if api_key and not available:
            st.caption("Daftar model gagal dimuat — pakai isian manual.")

        st.divider()
        st.subheader("Voice & Tempo")
        voice_label = st.selectbox("Suara", list(VOICES.keys()))
        speed = st.slider("Kecepatan bicara", -15, 25, 8, 1, format="%d%%")
        pitch = st.slider("Pitch (Hz)", -20, 20, 0, 2)

        st.divider()
        st.subheader("Render")
        n_scenes = st.slider("Jumlah scene", 3, 6, 5)
        transitions = st.toggle("Transisi fade halus antar scene", value=True)
        add_grain = st.toggle("Film grain tipis (anti-plastik)", value=True)

    st.markdown('<div class="kc-step">Langkah 1 · Produk affiliate</div>', unsafe_allow_html=True)

    product = st.text_area(
        "Nama / deskripsi produk yang lo promosiin",
        placeholder="Contoh: Sunset projector lamp RGB, ada remote, 16 warna, cocok buat kamar kos biar aesthetic. Toko Star Official, rating 4.9.",
        height=110,
    )
    audience = st.text_input(
        "Target penonton",
        placeholder="Contoh: cewek 18-25 anak kos yang suka dekor kamar",
    )
    price_note = st.text_input(
        "Harga / promo (kosongkan kalau tidak mau disebut)",
        placeholder="Contoh: 45 ribu, lagi flash sale, gratis ongkir",
    )

    c1, c2 = st.columns(2)
    with c1:
        angle = st.selectbox("Angle naskah", list(ANGLES.keys()))
    with c2:
        cta = st.selectbox("Arah CTA", list(CTA_TYPES.keys()))

    concept_label = st.selectbox("Konsep / gaya video", list(CONCEPTS.keys()))
    concept = CONCEPTS[concept_label]

    files = st.file_uploader("Screenshot produk Shopee (1–3 gambar)",
                             type=["jpg", "jpeg", "png", "webp"], accept_multiple_files=True)

    photos: list[Image.Image] = []
    if files:
        for f in files[:3]:
            try:
                photos.append(ImageOps.exif_transpose(Image.open(io.BytesIO(f.getvalue()))).convert("RGB"))
            except Exception:
                st.warning(f"Gagal membaca {f.name}, dilewati.")
        if photos:
            st.image(photos, width=118)

    st.write("")
    ready = bool(product.strip() and photos and api_key)
    if st.button("✍️ Buat Naskah", type="secondary", disabled=not ready):
        try:
            with st.spinner("Nulis naskah…"):
                ss.hook_text, ss.narrations = generate_script(
                    provider, api_key, model, product, audience, price_note,
                    concept, angle, cta, n_scenes, int(n_scenes * 5.5),
                )
                ss.video = None
        except Exception as e:
            st.error("Gagal bikin naskah.")
            st.code(str(e)[:1200])

    if not api_key:
        st.caption("Isi API Key di sidebar dulu ya.")

    # ── Langkah 2: edit naskah lalu render ────────────────────────────────────────
    if ss.narrations:
        st.markdown('<div class="kc-step">Langkah 2 · Cek & edit naskah</div>', unsafe_allow_html=True)
        st.caption("Betulin nama produk yang salah baca TTS di sini sebelum render — jauh lebih cepat daripada render ulang.")

        ss.hook_text = st.text_input("Teks hook di layar", value=ss.hook_text, max_chars=40)
        edited = []
        for i, t in enumerate(ss.narrations):
            edited.append(st.text_area(f"Scene {i + 1}", value=t, height=72, key=f"nar{i}"))
        ss.narrations = [e.strip() for e in edited if e.strip()]

        st.markdown('<div class="kc-step">Langkah 3 · Render</div>', unsafe_allow_html=True)
        if st.button("🎬 Generate Video", type="primary", disabled=not photos):
            bar = st.progress(0.0, text="Menyiapkan…")

            def upd(v: float, msg: str):
                bar.progress(v, text=msg)

            try:
                ss.video = produce(
                    photos=photos,
                    concept=concept,
                    hook_text=ss.hook_text,
                    narrations=ss.narrations,
                    voice=VOICES[voice_label],
                    rate=f"{speed:+d}%",
                    pitch=f"{pitch:+d}Hz",
                    add_grain=add_grain,
                    transitions=transitions,
                    progress=upd,
                )
                bar.progress(1.0, text="Selesai.")
            except Exception as e:
                bar.empty()
                st.error("Gagal render video.")
                st.code(str(e)[:1800])

    if ss.video:
        st.success("Video siap. Cek dulu sebelum upload.")
        st.video(ss.video)
        st.download_button(
            "⬇️ Download MP4",
            data=ss.video,
            file_name=f"klipcuan_{concept}_{uuid.uuid4().hex[:6]}.mp4",
            mime="video/mp4",
            type="primary",
        )
except Exception as _fatal_err:  # noqa: BLE001
    import streamlit as _st_fatal
    try:
        _st_fatal.set_page_config(page_title="KlipCuan — Error", page_icon="⚠️")
    except Exception:
        pass
    _st_fatal.error("Terjadi error yang tidak tertangani. Detail lengkap di bawah ini:")
    _st_fatal.exception(_fatal_err)
