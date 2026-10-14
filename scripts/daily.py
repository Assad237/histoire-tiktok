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

Structure du carrousel : 4 à 7 slides AU TOTAL.
1. Slide 1 = le HOOK (champ "accroche").
2. Slides de récit (champ "recit") : 2 à 5 slides selon la richesse du sujet, jamais rallongé artificiellement. Récit chronologique avec tension ; la chute de l'histoire est dans la dernière slide du récit. AUCUN appel à s'abonner, liker ou commenter dans le récit.
3. Dernière slide = UNIQUEMENT l'appel à s'abonner (champ "cta") : une seule phrase courte (30 à 90 caractères) contenant le mot « abonne », sans aucun fait historique. Exemples : « Tu aimes l'Histoire ? Abonne-toi. », « Abonne-toi pour une histoire vraie chaque jour. »

LE HOOK (slide 1) décide de tout : il doit stopper le scroll sur TikTok en moins de 2 secondes.
- 5 à 14 mots, 85 caractères maximum : lisible d'un seul coup d'œil.
- Crée un manque d'information : promets quelque chose de fou mais vrai, SANS révéler la fin ni l'explication.
- Concret et frappant : une date, un chiffre, un contraste, un superlatif ou une situation absurde.
- Exemples du niveau attendu : « En 1859, un cochon a failli déclencher une guerre. » / « Ce pape a fait juger… un cadavre. » / « Il a survécu aux deux bombes atomiques. »
- Interdits : « Saviez-vous », « Découvrez », « Aujourd'hui », « Voici l'histoire de », les questions molles, les emojis, les hashtags.
- Écris d'abord 5 hooks différents dans "hooks_candidats", puis recopie le plus fort dans "accroche"."texte".

Règles de rédaction :
- Faits exacts uniquement, dates correctes, pas d'invention. En cas de doute sur un détail, ne le mets pas.
- "surtitre" : 2 à 5 mots en majuscules (ex : "PARIS · 1793").
- "texte" des slides de récit : 90 à 170 caractères, phrases courtes, style oral et percutant.
- "recherche_image" : 2 à 5 mots-clés EN ANGLAIS pour trouver une image d'époque sur Wikimedia Commons (tableau, gravure, photo historique, carte) liée à la slide.
- "recherche_secours" : 2 à 4 mots-clés anglais généraux sur le sujet.
- "titre" : 60 caractères max. "description" : 2 phrases + 5 hashtags (#histoire #pourtoi etc.).

Réponds UNIQUEMENT avec ce JSON :
{{"sujet": "...", "titre": "...", "description": "...", "recherche_secours": "...",
 "hooks_candidats": ["...", "...", "...", "...", "..."],
 "accroche": {{"surtitre": "...", "texte": "...", "recherche_image": "..."}},
 "recit": [{{"surtitre": "...", "texte": "...", "recherche_image": "..."}}],
 "cta": "..."}}"""
    prompt += "\n\nIMPORTANT : JSON strictement valide. N'utilise JAMAIS de guillemets droits (\") à l'intérieur des textes : utilise les guillemets français « »."
    model = pick_model()
    log("Modèle Claude :", model)
    last_err = None
    for attempt in range(1, 4):  # jusqu'à 3 essais si la réponse n'est pas un JSON valide
        try:
            r = requests.post("https://api.anthropic.com/v1/messages",
                              headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                                       "content-type": "application/json"},
                              json={"model": model, "max_tokens": 3000,
                                    "messages": [{"role": "user", "content": prompt}]}, timeout=120)
            r.raise_for_status()
            txt = "".join(b.get("text", "") for b in r.json()["content"])
            m = re.search(r"\{.*\}", txt, re.S)
            data = json.loads(m.group(0))
            return check_slides(data)
        except (ValueError, AttributeError, KeyError, AssertionError) as e:
            last_err = e
            log(f"Réponse de Claude inutilisable (essai {attempt}/3) : {e}")
            time.sleep(3)
    raise RuntimeError(f"Claude n'a pas renvoyé de JSON valide après 3 essais : {last_err}")


ABONNE = re.compile(r"abonn", re.I)
CTA_DEFAUT = "Tu aimes l'Histoire ? Abonne-toi pour une histoire vraie chaque jour."


def sans_cta(texte):
    """Retire d'un texte de récit toute phrase qui appelle à s'abonner."""
    phrases = re.split(r"(?<=[.!?…])\s+", texte.strip())
    return " ".join(p for p in phrases if not ABONNE.search(p)).strip()


def check_slides(data):
    """Construit : 1 hook + 2 à 5 slides de récit + 1 slide CTA d'abonnement (4 à 7 au total)."""
    hook = data["accroche"]
    assert hook.get("texte", "").strip(), "accroche vide"
    recit = [r for r in data["recit"] if r.get("texte", "").strip()]
    assert len(recit) >= 2, f"récit trop court ({len(recit)} slides)"
    if len(recit) > 5:  # on garde le début du récit et sa chute
        recit = recit[:4] + [recit[-1]]
    for r in recit:
        r["texte"] = sans_cta(r["texte"]) or r["texte"]
    cta = (data.get("cta") or "").strip()
    if not ABONNE.search(cta) or len(cta) > 120:
        log("CTA absent ou invalide, CTA par défaut utilisé.")
        cta = CTA_DEFAUT
    data["cta"] = cta
    data["slides"] = ([dict(hook, type="hook")] + [dict(r, type="recit") for r in recit]
                      + [{"type": "cta", "surtitre": "ABONNE-TOI", "texte": cta}])
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


# Zone sûre TikTok (1080x1920) : l'interface masque le haut (onglets), le bas (pseudo, description)
# et la colonne de droite (boutons). Tout le texte reste dans ce cadre.
SAFE_LEFT, SAFE_RIGHT, SAFE_TOP, SAFE_BOTTOM = 80, 940, 260, 1440
CX = (SAFE_LEFT + SAFE_RIGHT) // 2
TEXT_W = SAFE_RIGHT - SAFE_LEFT - 80


def render(slide, img):
    kind = slide.get("type", "recit")
    base = Image.new("RGB", (W, H), (18, 12, 8))
    if img is not None:
        bg = cover(img, W, H)
        base.paste(bg.filter(ImageFilter.GaussianBlur(14 if kind == "cta" else 1)))
    dark = 150 if kind == "cta" else 85
    base = Image.blend(base, Image.new("RGB", (W, H), (10, 7, 5)), dark / 255)
    d = ImageDraw.Draw(base)
    f_sur = ImageFont.truetype(F_SANS, 38)
    size = {"hook": 92, "cta": 80}.get(kind, 60)
    max_h = SAFE_BOTTOM - SAFE_TOP - 260
    while True:  # réduit la police si le texte est trop long pour tenir dans la zone sûre
        f_txt = ImageFont.truetype(F_BOLD, size)
        lines = wrap(d, slide["texte"], f_txt, TEXT_W)
        lh = int(size * 1.25)
        block = lh * len(lines)
        if block <= max_h or size <= 40:
            break
        size -= 4
    sur = slide.get("surtitre", "").upper()
    extra = 70 if kind == "hook" else 0  # place pour « Swipe → »
    total_h = 90 + block + extra
    top = SAFE_TOP + (SAFE_BOTTOM - SAFE_TOP - total_h) // 2
    # panneau sombre semi-transparent derrière le texte
    panel = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(panel).rounded_rectangle(
        [SAFE_LEFT, top - 40, SAFE_RIGHT, top + total_h + 30], radius=28, fill=(8, 5, 3, 165))
    base = Image.alpha_composite(base.convert("RGBA"), panel).convert("RGB")
    d = ImageDraw.Draw(base)
    if sur:
        sw = d.textlength(sur, font=f_sur)
        d.rectangle([CX - sw / 2 - 22, top, CX + sw / 2 + 22, top + 62], outline=GOLD, width=3)
        d.text((CX, top + 31), sur, font=f_sur, fill=GOLD, anchor="mm")
    y = top + 90
    color = GOLD if kind == "cta" else CREAM
    for i, ln in enumerate(lines):
        d.text((CX + 3, y + i * lh + 3), ln, font=f_txt, fill=(0, 0, 0), anchor="ma")
        d.text((CX, y + i * lh), ln, font=f_txt, fill=color, anchor="ma")
    if kind == "hook":
        d.text((CX, y + block + 35), "Swipe →", font=ImageFont.truetype(F_SANS, 36), fill=GOLD, anchor="mm")
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
    first_img = None
    for i, s in enumerate(data["slides"]):
        img, found = None, None
        if s["type"] == "cta":
            img = first_img  # fond : l'image du hook, floutée et assombrie
        else:
            for q in (s.get("recherche_image"), data.get("recherche_secours")):
                if q and not found:
                    try:
                        found = search_commons(q, used)
                    except Exception as e:  # noqa
                        log("Recherche échouée", q, e)
            if found:
                try:
                    img = fetch_image(found["url"])
                    used.add(found["url"])
                    if not FREE_PD.search(found["license"]):
                        credits.append(f"{found['artist'] or 'Wikimedia Commons'} ({found['license']})")
                except Exception as e:  # noqa
                    log("Téléchargement échoué", e)
            if first_img is None and img is not None:
                first_img = img
        log(f"Slide {i+1} ({s['type']}): image {'OK' if img else 'aucune (fond uni)'}")
        path = os.path.join(out_dir, f"slide{i+1}.jpg")
        render(s, img).save(path, "JPEG", quality=90)
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
