#!/usr/bin/env python3
"""Génère chaque jour un carrousel TikTok Histoire et l'envoie en brouillon via Make.

Étapes : sujet + textes (Claude) -> images libres (Wikimedia Commons) -> montage 1080x1920
-> publication sur GitHub Pages -> webhook Make (rafraîchit le token TikTok et envoie le brouillon).
"""
import datetime as dt
import html
import io
import json
import os
import re
import subprocess
import sys
import textwrap
import time
import urllib.parse

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = "https://assad237.github.io/histoire-tiktok"
DONE_FILE = os.path.join(ROOT, "data", "sujets_traites.json")
UA = "HistoireAutoPoster/1.0 (https://assad237.github.io/histoire-tiktok/; mboulemarlon@gmail.com)"
W, H = 1080, 1920
FONT_DIR = "/usr/share/fonts/truetype/dejavu"
F_BOLD = os.path.join(FONT_DIR, "DejaVuSerif-Bold.ttf")
F_REG = os.path.join(FONT_DIR, "DejaVuSerif.ttf")
F_SANS = os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")
GOLD = (217, 178, 106)
CREAM = (245, 236, 217)

ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
MAKE_WEBHOOK = os.environ["MAKE_WEBHOOK_URL"]
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- Claude
def pick_model():
    m = os.environ.get("ANTHROPIC_MODEL", "").strip()
    if m:
        return m
    r = requests.get("https://api.anthropic.com/v1/models",
                     headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01"}, timeout=30)
    r.raise_for_status()
    ids = [x["id"] for x in r.json().get("data", [])]
    for fam in ("sonnet", "opus", "haiku"):
        for i in ids:  # la liste est triée du plus récent au plus ancien
            if fam in i:
                return i
    return ids[0]


def ask_claude(done):
    deja = "\n".join(f"- {t}" for t in done[-200:]) or "(aucun)"
    prompt = f"""Tu écris pour un compte TikTok francophone d'histoire (carrousels photo).
Choisis UN sujet historique précis, surprenant et vérifiable (événement, personnage, anecdote), différent de ceux déjà traités :
{deja}

Règles :
- Faits exacts uniquement, dates correctes, pas d'invention. En cas de doute sur un détail, ne le mets pas.
- 7 slides : slide 1 = accroche choc (question ou fait étonnant), slides 2-6 = récit chronologique avec tension, slide 7 = chute + appel à commenter.
- "surtitre" : 2 à 5 mots en majuscules (ex : "PARIS · 1793").
- "texte" : 90 à 170 caractères, phrases courtes, style oral et percutant.
- "recherche_image" : 2 à 5 mots-clés EN ANGLAIS pour trouver une image d'époque sur Wikimedia Commons (tableau, gravure, photo historique, carte) liée à la slide.
- "recherche_secours" : 2 à 4 mots-clés anglais généraux sur le sujet.
- "titre" : 60 caractères max. "description" : 2 phrases + 5 hashtags (#histoire #pourtoi etc.).

Réponds UNIQUEMENT avec ce JSON :
{{"sujet": "...", "titre": "...", "description": "...", "recherche_secours": "...",
 "slides": [{{"surtitre": "...", "texte": "...", "recherche_image": "..."}}]}}"""
    model = pick_model()
    log("Modèle Claude :", model)
    r = requests.post("https://api.anthropic.com/v1/messages",
                      headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                               "content-type": "application/json"},
                      json={"model": model, "max_tokens": 2500,
                            "messages": [{"role": "user", "content": prompt}]}, timeout=120)
    r.raise_for_status()
    txt = "".join(b.get("text", "") for b in r.json()["content"])
    m = re.search(r"\{.*\}", txt, re.S)
    data = json.loads(m.group(0))
    assert 5 <= len(data["slides"]) <= 10, "nombre de slides inattendu"
    return data


# ---------------------------------------------------------------- Wikimedia
FREE_PD = re.compile(r"public domain|^pd|cc0|no restrictions", re.I)
FREE_BY = re.compile(r"^cc[ -]by(-sa)?( \d|$)", re.I)


def strip_html(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def search_commons(query, used):
    params = {"action": "query", "format": "json", "generator": "search",
              "gsrsearch": f"{query} filetype:bitmap", "gsrnamespace": 6, "gsrlimit": 20,
              "prop": "imageinfo", "iiprop": "url|size|extmetadata|mime", "iiurlwidth": 1280}
    r = requests.get("https://commons.wikimedia.org/w/api.php", params=params,
                     headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    pages = sorted(r.json().get("query", {}).get("pages", {}).values(), key=lambda p: p.get("index", 99))
    best = None
    for p in pages:
        ii = (p.get("imageinfo") or [{}])[0]
        if ii.get("mime") not in ("image/jpeg", "image/png") or ii.get("width", 0) < 700:
            continue
        if ii.get("url") in used:
            continue
        meta = ii.get("extmetadata", {})
        lic = strip_html(meta.get("LicenseShortName", {}).get("value", ""))
        cand = {"url": ii.get("thumburl") or ii["url"], "orig": ii["url"], "license": lic,
                "artist": strip_html(meta.get("Artist", {}).get("value", ""))[:80],
                "page": ii.get("descriptionurl", "")}
        if FREE_PD.search(lic):
            return cand
        if best is None and FREE_BY.search(lic) and "nc" not in lic.lower() and "nd" not in lic.lower():
            best = cand
    return best


def fetch_image(url):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=60)
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content)).convert("RGB")


# ---------------------------------------------------------------- Montage
def cover(img, w, h):
    s = max(w / img.width, h / img.height)
    img = img.resize((int(img.width * s) + 1, int(img.height * s) + 1), Image.LANCZOS)
    x, y = (img.width - w) // 2, max(0, (img.height - h) // 3)
    return img.crop((x, y, x + w, y + h))


def wrap(draw, text, font, maxw):
    words, lines, cur = text.split(), [], ""
    for wd in words:
        t = f"{cur} {wd}".strip()
        if draw.textlength(t, font=font) > maxw and cur:
            lines.append(cur)
            cur = wd
        else:
            cur = t
    if cur:
        lines.append(cur)
    return lines


def render(slide, img, idx, total):
    base = Image.new("RGB", (W, H), (18, 12, 8))
    if img is not None:
        bg = cover(img, W, H)
        blur = bg.filter(ImageFilter.GaussianBlur(2))
        base.paste(blur)
    # assombrissement + dégradé vers le bas
    shade = Image.new("L", (W, H))
    sd = ImageDraw.Draw(shade)
    for y in range(H):
        a = 70 if y < H * 0.4 else int(70 + (y - H * 0.4) / (H * 0.6) * 165)
        sd.line([(0, y), (W, y)], fill=a)
    base = Image.composite(Image.new("RGB", (W, H), (10, 7, 5)), base, shade)
    d = ImageDraw.Draw(base)
    hook = idx == 0
    f_txt = ImageFont.truetype(F_BOLD, 76 if hook else 60)
    f_sur = ImageFont.truetype(F_SANS, 40)
    f_small = ImageFont.truetype(F_REG, 32)
    lines = wrap(d, slide["texte"], f_txt, W - 140)
    lh = int(f_txt.size * 1.28)
    block = lh * len(lines)
    y = (H - block) // 2 + 120 if hook else H - 260 - block
    sur = slide.get("surtitre", "").upper()
    sw = d.textlength(sur, font=f_sur)
    d.rectangle([(W - sw) / 2 - 24, y - 110, (W + sw) / 2 + 24, y - 40], outline=GOLD, width=3)
    d.text((W / 2, y - 75), sur, font=f_sur, fill=GOLD, anchor="mm")
    for i, ln in enumerate(lines):
        d.text((W / 2 + 3, y + i * lh + 3), ln, font=f_txt, fill=(0, 0, 0), anchor="ma")
        d.text((W / 2, y + i * lh), ln, font=f_txt, fill=CREAM, anchor="ma")
    d.text((W / 2, H - 150), f"{idx + 1} / {total}", font=f_small, fill=(190, 175, 150), anchor="mm")
    if hook:
        d.text((W / 2, H - 210), "Swipe →", font=f_small, fill=GOLD, anchor="mm")
    return base


# ---------------------------------------------------------------- Git / Pages
def sh(*cmd):
    log("$", " ".join(cmd))
    subprocess.run(cmd, cwd=ROOT, check=True)


def publish_and_wait(rel_paths, check_url):
    sh("git", "config", "user.name", "histoire-bot")
    sh("git", "config", "user.email", "histoire-bot@users.noreply.github.com")
    sh("git", "add", *rel_paths)
    sh("git", "commit", "-m", f"Carrousel du {dt.date.today().isoformat()}")
    sh("git", "push")
    tok, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if tok and repo:
        r = requests.post(f"https://api.github.com/repos/{repo}/pages/builds",
                          headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"})
        log("Demande de build Pages :", r.status_code)
    for _ in range(60):
        if requests.get(check_url, timeout=20).status_code == 200:
            log("Images en ligne :", check_url)
            return
        time.sleep(10)
    raise RuntimeError("Les images ne sont pas en ligne après 10 minutes")


# ---------------------------------------------------------------- Main
def main():
    done = json.load(open(DONE_FILE)) if os.path.exists(DONE_FILE) else []
    data = ask_claude([d["sujet"] for d in done])
    log("Sujet :", data["sujet"])
    day = dt.datetime.utcnow().strftime("%Y-%m-%d-%H%M")
    out_dir = os.path.join(ROOT, "images", day)
    os.makedirs(out_dir, exist_ok=True)
    used, credits, files = set(), [], []
    total = len(data["slides"])
    for i, s in enumerate(data["slides"]):
        found = None
        for q in (s.get("recherche_image"), data.get("recherche_secours")):
            if q and not found:
                try:
                    found = search_commons(q, used)
                except Exception as e:  # noqa
                    log("Recherche échouée", q, e)
        img = None
        if found:
            try:
                img = fetch_image(found["url"])
                used.add(found["url"])
                if not FREE_PD.search(found["license"]):
                    credits.append(f"{found['artist'] or 'Wikimedia Commons'} ({found['license']})")
            except Exception as e:  # noqa
                log("Téléchargement échoué", e)
        log(f"Slide {i+1}: image {'OK ' + found['license'] if img else 'aucune (fond uni)'}")
        path = os.path.join(out_dir, f"slide{i+1}.jpg")
        render(s, img, i, total).save(path, "JPEG", quality=88)
        files.append(f"images/{day}/slide{i+1}.jpg")
        time.sleep(1)

    desc = data["description"].strip()
    if credits:
        desc += "\n\nImages : Wikimedia Commons – " + "; ".join(dict.fromkeys(credits))
    done.append({"date": day, "sujet": data["sujet"], "titre": data["titre"]})
    os.makedirs(os.path.dirname(DONE_FILE), exist_ok=True)
    json.dump(done, open(DONE_FILE, "w"), ensure_ascii=False, indent=1)
    json.dump(data, open(os.path.join(out_dir, "contenu.json"), "w"), ensure_ascii=False, indent=1)

    if DRY_RUN:
        log("DRY_RUN : pas de publication.")
        return
    urls = [f"{SITE}/{f}" for f in files]
    publish_and_wait([f"images/{day}", "data/sujets_traites.json"], urls[-1])

    body = {"post_info": {"title": data["titre"][:90], "description": desc[:4000]},
            "source_info": {"source": "PULL_FROM_URL", "photo_cover_index": 0, "photo_images": urls},
            "post_mode": "MEDIA_UPLOAD", "media_type": "PHOTO"}
    r = requests.post(MAKE_WEBHOOK, json={"body": json.dumps(body, ensure_ascii=False)}, timeout=60)
    log("Webhook Make :", r.status_code, r.text[:200])
    r.raise_for_status()


if __name__ == "__main__":
    main()
