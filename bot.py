"""
Bot Discord de signaux de marché : actions, ETF, crypto, immobilier coté.

Outil d'information automatisé. Les signaux sont génériques, calculés par des
règles techniques publiques (voir README). Ce n'est PAS un conseil en
investissement personnalisé. Risque de perte en capital.
"""
import asyncio
import datetime as dt
import io
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
import discord
import matplotlib
import numpy as np
import pandas as pd
import yfinance as yf
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

matplotlib.use("Agg")
from matplotlib.dates import DateFormatter  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("fibot")

load_dotenv()


def _env(nom: str, defaut: float, conv=int):
    """Lit une variable d'environnement numérique ; vide ou absente = valeur par défaut."""
    brut = (os.getenv(nom) or "").strip().replace(",", ".")
    if not brut:
        return conv(defaut)
    try:
        return conv(brut)
    except ValueError:
        raise SystemExit(f"{nom} invalide dans .env : « {brut} » (un nombre est attendu)")


TOKEN = (os.getenv("DISCORD_TOKEN") or "").strip()
CHANNEL_ID = _env("CHANNEL_ID", 0)                  # salon des alertes
GUILD_ID = _env("GUILD_ID", 0)                      # optionnel : commandes dispo tout de suite
OWNER_ID = _env("OWNER_ID", 0)                      # si renseigné : bot privé, toi seul peux l'utiliser
BUDGET_MAX = _env("BUDGET_MAX", 50, float)          # euros max par achat
TAUX_PFU = _env("TAUX_PFU", 31.4, float)            # flat tax en %, à vérifier chaque année
PARIS = ZoneInfo("Europe/Paris")
# DATA_DIR permet de ranger data.json sur un volume persistant (Docker, hébergeur)
DATA_FILE = Path(os.getenv("DATA_DIR") or Path(__file__).parent) / "data.json"
MAX_ALERTES = 20        # alertes de prix par utilisateur
MAX_WATCH = 60          # actifs ajoutés avec /suivre
MAX_JOURNAL = 1000      # signaux conservés pour /performance

# ---------------------------------------------------------------- univers suivi
# Tickers au format Yahoo Finance. Un ticker introuvable est simplement ignoré.
THEMES = {
    "tech": ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "ASML.AS", "SAP.DE"],
    "ia": ["NVDA", "AMD", "TSM", "AVGO", "PLTR", "MU"],
    "materiaux": ["RIO", "BHP", "FCX", "ALB", "AI.PA", "MT.AS"],
    "energie": ["TTE.PA", "XOM", "ENPH", "NEE"],
    "crypto": ["BTC-EUR", "ETH-EUR", "SOL-EUR", "BNB-EUR", "XRP-EUR",
               "ADA-EUR", "AVAX-EUR", "LINK-EUR", "DOT-EUR"],
    # Foncières cotées (SIIC / REIT) : de l'immobilier accessible dès quelques euros
    "immobilier": ["URW.PA", "LI.PA", "GFC.PA", "COV.PA", "ICAD.PA", "MERY.PA",
                   "VNA.DE", "O", "PLD", "VNQ"],
    "etf": ["CW8.PA", "ESE.PA", "PUST.PA"],
}
INDICES = {"CAC 40": "^FCHI", "S&P 500": "^GSPC", "Nasdaq": "^IXIC",
           "Bitcoin": "BTC-EUR", "Or": "GC=F", "VIX": "^VIX"}
TAUX_10ANS = "^TNX"   # taux US 10 ans : quand il monte, l'immobilier coté souffre

# Noms lisibles pour les alertes (un ticker absent s'affiche tel quel)
NOMS = {
    "AAPL": "Apple", "MSFT": "Microsoft", "GOOGL": "Alphabet (Google)", "AMZN": "Amazon",
    "META": "Meta (Facebook)", "ASML.AS": "ASML", "SAP.DE": "SAP",
    "NVDA": "Nvidia", "AMD": "AMD", "TSM": "TSMC", "AVGO": "Broadcom", "PLTR": "Palantir",
    "MU": "Micron",
    "RIO": "Rio Tinto", "BHP": "BHP", "FCX": "Freeport-McMoRan", "ALB": "Albemarle",
    "AI.PA": "Air Liquide", "MT.AS": "ArcelorMittal",
    "TTE.PA": "TotalEnergies", "XOM": "ExxonMobil", "ENPH": "Enphase", "NEE": "NextEra Energy",
    "BTC-EUR": "Bitcoin", "ETH-EUR": "Ethereum", "SOL-EUR": "Solana", "BNB-EUR": "BNB",
    "XRP-EUR": "XRP", "ADA-EUR": "Cardano", "AVAX-EUR": "Avalanche", "LINK-EUR": "Chainlink",
    "DOT-EUR": "Polkadot",
    "URW.PA": "Unibail-Rodamco-Westfield", "LI.PA": "Klépierre", "GFC.PA": "Gecina",
    "COV.PA": "Covivio", "ICAD.PA": "Icade", "MERY.PA": "Mercialys", "VNA.DE": "Vonovia",
    "O": "Realty Income", "PLD": "Prologis", "VNQ": "ETF immobilier américain (Vanguard)",
    "CW8.PA": "ETF Monde (Amundi MSCI World)", "ESE.PA": "ETF S&P 500 (BNP Paribas)",
    "PUST.PA": "ETF Nasdaq-100 (Amundi)",
}
INDICES_EXPL = {"CAC 40": "grandes entreprises françaises",
                "S&P 500": "grandes entreprises américaines",
                "Nasdaq": "technologie américaine", "Bitcoin": "première crypto",
                "Or": "valeur refuge"}
PLACES_EURO = (".PA", ".AS", ".DE", ".MI", ".MC", ".BR", ".LS", ".F", ".VI", ".HE", ".IR")


def nom(t: str) -> str:
    """« **Apple** (`AAPL`) » si le nom est connu, sinon « **TICKER** »."""
    return f"**{NOMS[t]}** (`{t}`)" if t in NOMS else f"**{t}**"


def devise(t: str) -> str:
    """Devise de cotation, déduite du symbole Yahoo ; vide si on ne sait pas."""
    if t.endswith("-EUR") or t.endswith(PLACES_EURO):
        return "€"
    if t.endswith("-USD") or t.isalpha():
        return "$"
    return ""


LEGAL_COURT = "Signal automatique générique • Pas un conseil personnalisé • Risque de perte en capital • /legal"


# ---------------------------------------------------------------- stockage
def load() -> dict:
    d = {}
    if DATA_FILE.exists():
        try:
            d = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            # fichier abîmé : on le met de côté plutôt que de l'écraser ou de planter
            copie = DATA_FILE.with_name(f"data.corrompu-{int(time.time())}.json")
            log.error("data.json illisible (%s), copie conservée dans %s", exc, copie.name)
            try:
                DATA_FILE.replace(copie)
            except OSError:
                pass
    if not isinstance(d, dict):
        d = {}
    for k, v in (("consent", {}), ("positions", {}), ("alertes", []),
                 ("journal", []), ("watch", [])):
        d.setdefault(k, v)
    return d


def save(d: dict) -> None:
    """Écriture atomique : un arrêt brutal ne laisse jamais un data.json à moitié écrit."""
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA_FILE.parent, prefix=".data-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2, ensure_ascii=False)
        os.replace(tmp, DATA_FILE)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def today() -> dt.date:
    """Date du jour à Paris, quel que soit le fuseau du serveur qui héberge le bot."""
    return dt.datetime.now(PARIS).date()


TICKER_RE = re.compile(r"[A-Z0-9^][A-Z0-9.\-=^]{0,14}")


def norm(ticker: str) -> str | None:
    """Nettoie un symbole saisi par l'utilisateur ; None s'il n'a pas une forme valide."""
    t = ticker.upper().strip()
    return t if TICKER_RE.fullmatch(t) else None


def all_tickers() -> list[str]:
    return sorted({t for lst in THEMES.values() for t in lst} | set(load()["watch"]))


def kind(t: str) -> str:
    if t.endswith(("-EUR", "-USD")):
        return "crypto"
    if t in THEMES["immobilier"]:
        return "immo"
    return "etf" if t in THEMES["etf"] else "action"


# ---------------------------------------------------------------- données
_cache: dict[str, tuple[float, pd.DataFrame]] = {}
_echecs: dict[str, float] = {}            # tickers introuvables : pas de nouvel essai immédiat
_ctx_cache: tuple[float, dict] = (0.0, {})
_dl_lock = asyncio.Lock()                  # un seul téléchargement Yahoo à la fois
CACHE_TTL = 15 * 60
ECHEC_TTL = 2 * 60
logging.getLogger("yfinance").setLevel(logging.CRITICAL)   # très bavard ; on résume nous-mêmes


def _download(tickers: list[str]) -> dict[str, pd.DataFrame]:
    data = yf.download(tickers, period="2y", interval="1d", group_by="ticker",
                       auto_adjust=True, actions=True, progress=False, threads=True)
    out = {}
    for t in tickers:
        try:
            df = data[t] if isinstance(data.columns, pd.MultiIndex) else data
            df = df.dropna(subset=["Close"])
        except KeyError:
            continue
        if len(df) >= 60:
            out[t] = df
    return out


async def get_data(tickers: list[str]) -> dict[str, pd.DataFrame]:
    tickers = list(dict.fromkeys(t.upper() for t in tickers))

    def manquants() -> list[str]:
        now = time.time()
        return [t for t in tickers
                if (t not in _cache or now - _cache[t][0] > CACHE_TTL)
                and now - _echecs.get(t, 0) > ECHEC_TTL]

    if manquants():
        async with _dl_lock:
            missing = manquants()      # une autre commande a pu les charger entre-temps
            if missing:
                try:
                    fresh = await asyncio.to_thread(_download, missing)
                except Exception as exc:
                    log.warning("Téléchargement impossible (%s) : %s", ", ".join(missing), exc)
                    fresh = None
                # yfinance ne lève pas d'erreur quand Yahoo est injoignable : un lot entier
                # revenu vide est traité comme une panne, pas comme des tickers inexistants.
                if fresh is not None and not fresh and len(missing) > 1:
                    log.warning("Yahoo Finance n'a rien renvoyé pour %d actifs (panne ou "
                                "limitation ?)", len(missing))
                    fresh = None
                if fresh is not None:      # panne réseau : on réessaiera au prochain appel
                    now = time.time()
                    for t in missing:
                        if t in fresh:
                            _cache[t] = (now, fresh[t])
                            _echecs.pop(t, None)
                        else:
                            _echecs[t] = now
    # en cas de panne, on sert les dernières données connues plutôt que rien
    return {t: _cache[t][1] for t in tickers if t in _cache}


FNG_FR = {"Extreme Fear": "Peur extrême", "Fear": "Peur", "Neutral": "Neutre",
          "Greed": "Avidité", "Extreme Greed": "Avidité extrême"}


async def get_ctx() -> dict:
    """Contexte de marché : sentiment crypto (Fear & Greed) et tendance des taux."""
    global _ctx_cache
    if time.time() < _ctx_cache[0]:
        return _ctx_cache[1]
    ctx = {"fng": None, "taux": None}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.get("https://api.alternative.me/fng/?limit=1") as r:
                d = (await r.json(content_type=None))["data"][0]
                ctx["fng"] = (int(d["value"]), FNG_FR.get(d["value_classification"],
                                                           d["value_classification"]))
    except Exception as exc:
        log.warning("Fear & Greed indisponible : %s", exc)
    tnx = (await get_data([TAUX_10ANS])).get(TAUX_10ANS)
    if tnx is not None and len(tnx) > 63:
        ctx["taux"] = float(tnx["Close"].iloc[-1] - tnx["Close"].iloc[-63])
    # contexte complet : valable 1 h ; incomplet : nouvel essai dans 5 min
    complet = ctx["fng"] is not None and ctx["taux"] is not None
    _ctx_cache = (time.time() + (3600 if complet else 300), ctx)
    return ctx


# ---------------------------------------------------------------- analyse
def analyse(df: pd.DataFrame, k: str = "action", ctx: dict | None = None) -> dict:
    """Score de -100 à +100, décision, mise indicative et raisons."""
    ctx = ctx or {}
    close = df["Close"].astype(float)
    price = float(close.iloc[-1])
    sma50 = float(close.rolling(50).mean().iloc[-1])
    sma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else float("nan")

    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi_s = (100 - 100 / (1 + gain / loss.replace(0, np.nan))).fillna(100)
    rsi = float(rsi_s.iloc[-1])

    macd = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    hist = macd - macd.ewm(span=9, adjust=False).mean()
    h_now, h_prev = float(hist.iloc[-1]), float(hist.iloc[-4])

    mom3m = price / float(close.iloc[-63]) - 1 if len(close) > 63 else 0.0
    vol = float(close.pct_change().tail(60).std() * np.sqrt(252))
    drawdown = price / float(close.tail(252).max()) - 1

    tr = pd.concat([df["High"] - df["Low"], (df["High"] - close.shift()).abs(),
                    (df["Low"] - close.shift()).abs()], axis=1).max(axis=1)
    atr = float(tr.rolling(14).mean().iloc[-1])

    rendement = 0.0
    if "Dividends" in df:
        rendement = float(df["Dividends"].fillna(0).tail(252).sum()) / price

    score, why = 0, []
    plus, moins = [], []       # les mêmes raisons, en langage courant, pour les alertes
    if not np.isnan(sma200):
        if price > sma200:
            score += 20; why.append("✅ Au-dessus de la moyenne 200 j (tendance longue haussière)")
            plus.append("la tendance de fond est à la hausse depuis plusieurs mois")
        else:
            score -= 20; why.append("❌ Sous la moyenne 200 j (tendance longue baissière)")
            moins.append("la tendance de fond est à la baisse depuis plusieurs mois")
        if sma50 > sma200:
            score += 15; why.append("✅ Moyenne 50 j > 200 j")
        else:
            score -= 15; why.append("❌ Moyenne 50 j < 200 j")
    else:
        why.append("ℹ️ Historique trop court pour la moyenne 200 j")

    if rsi < 30:
        score += 25; why.append(f"✅ RSI {rsi:.0f} : survendu, rebond possible")
        plus.append("le cours a beaucoup baissé ces derniers jours, un rebond est possible")
    elif rsi < 45:
        score += 10; why.append(f"✅ RSI {rsi:.0f} : repli, point d'entrée correct")
        plus.append("le cours vient de reculer un peu, ce qui donne un prix d'entrée correct")
    elif rsi > 80:
        score -= 30; why.append(f"❌ RSI {rsi:.0f} : très suracheté")
        moins.append("le cours est monté beaucoup trop vite, le risque de rechute est élevé")
    elif rsi > 70:
        score -= 20; why.append(f"❌ RSI {rsi:.0f} : suracheté")
        moins.append("le cours est monté très vite, une pause ou un recul est probable")
    else:
        why.append(f"➖ RSI {rsi:.0f} : neutre")

    if h_now > 0 and h_now > h_prev:
        score += 15; why.append("✅ MACD positif et en hausse")
        plus.append("l'élan des derniers jours est positif et se renforce")
    elif h_now < 0 and h_now < h_prev:
        score -= 15; why.append("❌ MACD négatif et en baisse")
        moins.append("l'élan des derniers jours est négatif et s'aggrave")
    elif h_now > h_prev:
        score += 5; why.append("➖ MACD en redressement")
    else:
        score -= 5; why.append("➖ MACD en essoufflement")

    if mom3m > 0.10:
        score += 10; why.append(f"✅ {mom3m:+.0%} sur 3 mois")
        plus.append(f"il a gagné {abs(mom3m):.0%} en 3 mois")
    elif mom3m < -0.10:
        score -= 10; why.append(f"❌ {mom3m:+.0%} sur 3 mois")
        moins.append(f"il a perdu {abs(mom3m):.0%} en 3 mois")

    # --- facteurs propres à la crypto : sentiment de marché (contrarien)
    if k == "crypto" and ctx.get("fng"):
        v, lab = ctx["fng"]
        if v <= 25:
            score += 10; why.append(f"✅ Sentiment crypto : {lab} ({v}/100), zone historiquement favorable")
            plus.append("les investisseurs crypto ont très peur, ce qui a souvent été un bon moment pour acheter")
        elif v >= 75:
            score -= 10; why.append(f"❌ Sentiment crypto : {lab} ({v}/100), marché euphorique")
            moins.append("le marché crypto est euphorique, ce qui précède souvent une baisse")
        else:
            why.append(f"➖ Sentiment crypto : {lab} ({v}/100)")
    # --- facteurs propres à l'immobilier coté : taux d'intérêt et rendement
    if k == "immo":
        t = ctx.get("taux")
        if t is not None:
            if t <= -0.30:
                score += 10; why.append(f"✅ Taux 10 ans en baisse ({pts(t)} pt sur 3 mois) : favorable à l'immobilier")
                plus.append("les taux d'intérêt baissent, ce qui aide l'immobilier")
            elif t >= 0.30:
                score -= 10; why.append(f"❌ Taux 10 ans en hausse ({pts(t)} pt sur 3 mois) : pèse sur l'immobilier")
                moins.append("les taux d'intérêt montent, ce qui pénalise l'immobilier")
        if rendement >= 0.05 and not np.isnan(sma200) and price > sma200:
            score += 5
        if rendement > 0:
            why.append("💰 Rendement du dividende sur 12 mois : " + f"{rendement:.1%}".replace(".", ","))
    if drawdown < -0.30:
        why.append(f"ℹ️ {drawdown:.0%} sous son plus haut d'un an")

    score = int(max(-100, min(100, score)))
    if score >= 40:
        emoji, label, part = "🟢", "ACHAT FORT", 1.0
    elif score >= 20:
        emoji, label, part = "🟢", "ACHAT", 0.6
    elif score > -20:
        emoji, label, part = "🟡", "ATTENDRE", 0.0
    elif score > -40:
        emoji, label, part = "🟠", "ALLÉGER", 0.0
    else:
        emoji, label, part = "🔴", "VENDRE", 0.0
    if vol > 0.60:          # actif très volatil : mise réduite
        part *= 0.6
    montant = round(BUDGET_MAX * part / 5) * 5

    return {"price": price, "score": score, "emoji": emoji, "label": label,
            "decision": f"{emoji} {label}", "montant": montant, "rsi": rsi, "vol": vol,
            "mom3m": mom3m, "why": why, "stop": price - 2 * atr, "objectif": price + 3 * atr,
            "rendement": rendement, "sma50": close.rolling(50).mean(),
            "sma200": close.rolling(200).mean(), "rsi_s": rsi_s, "kind": k,
            "plus": plus, "moins": moins}


async def classement(tickers: list[str]) -> list[tuple[str, dict]]:
    data, ctx = await get_data(tickers), await get_ctx()
    res = [(t, analyse(df, kind(t), ctx)) for t, df in data.items()]
    return sorted(res, key=lambda x: x[1]["score"], reverse=True)


# ---------------------------------------------------------------- mise en forme
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.",
        "oct.", "nov.", "déc."]
ICONE = {"crypto": "🪙", "immo": "🏠", "etf": "🧺", "action": "📈"}


def fmt(x: float) -> str:
    s = f"{x:,.2f}" if abs(x) >= 1 else f"{x:.4f}"
    return s.replace(",", " ").replace(".", ",")


def pct(x: float) -> str:
    return f"{x:+.1%}".replace(".", ",").replace("-", "−")


def prix(x: float, d: str = "") -> str:
    return f"{fmt(x)} {d}".strip()


def pts(x: float) -> str:
    return f"{x:+.2f}".replace(".", ",").replace("-", "−")


def jauge(score: int) -> str:
    n = round((score + 100) / 20)
    return "▰" * n + "▱" * (10 - n)


def couleur(score: int) -> int:
    return 0x23A55A if score >= 20 else 0xF23F43 if score <= -20 else 0xF0B232


def date_fr(d: dt.datetime) -> str:
    return f"{JOURS[d.weekday()].capitalize()} {d.day} {MOIS[d.month - 1]} • {d:%H:%M}"


# ---------------------------------------------------------------- graphiques
BG, PANEL, GRID = "#1e1f22", "#2b2d31", "#3a3c42"
TXT, MUTED = "#f2f3f5", "#949ba4"
C_GREEN, C_RED, C_YEL, C_BLUE, C_VIOLET = "#23a55a", "#f23f43", "#f0b232", "#5865f2", "#b48cf2"


def _hex(score: int) -> str:
    return C_GREEN if score >= 20 else C_RED if score <= -20 else C_YEL


def _style(ax):
    ax.set_facecolor(PANEL)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.tick_params(colors=MUTED, labelsize=9, length=0)
    ax.grid(color=GRID, linewidth=0.6, alpha=0.7)
    ax.yaxis.tick_right()


def _png(fig) -> io.BytesIO:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=160, facecolor=BG)
    buf.seek(0)
    return buf


def chart_analyse(ticker: str, df: pd.DataFrame, a: dict) -> io.BytesIO:
    n = 180
    close = df["Close"].astype(float).tail(n)
    col = _hex(a["score"])
    fig = Figure(figsize=(9, 5.4))
    gs = fig.add_gridspec(2, 1, height_ratios=[3.2, 1], hspace=0.08,
                          left=0.03, right=0.92, top=0.84, bottom=0.07)
    ax, ax2 = fig.add_subplot(gs[0]), fig.add_subplot(gs[1])
    _style(ax); _style(ax2)

    ax.plot(close.index, close, color=col, linewidth=2.2, zorder=3)
    ax.fill_between(close.index, close, close.min() * 0.98, color=col, alpha=0.12)
    ax.plot(close.index, a["sma50"].tail(n), color=C_BLUE, linewidth=1.2, label="Moy. 50 j")
    ax.plot(close.index, a["sma200"].tail(n), color=C_VIOLET, linewidth=1.2,
            linestyle="--", label="Moy. 200 j")
    if a["montant"] > 0:
        ax.axhline(a["stop"], color=C_RED, linewidth=1, linestyle=":")
        ax.axhline(a["objectif"], color=C_GREEN, linewidth=1, linestyle=":")
        ax.text(close.index[0], a["stop"], " Stop", color=C_RED, fontsize=8, va="bottom")
        ax.text(close.index[0], a["objectif"], " Objectif", color=C_GREEN, fontsize=8, va="bottom")
    ax.scatter([close.index[-1]], [close.iloc[-1]], color=col, s=45, zorder=4,
               edgecolors=BG, linewidths=1.5)
    ax.legend(loc="lower right", frameon=False, labelcolor=MUTED, fontsize=8, ncols=2)
    ax.set_xticklabels([])
    ax.margins(x=0.01)

    rsi = a["rsi_s"].tail(n)
    ax2.plot(rsi.index, rsi, color=TXT, linewidth=1.2)
    ax2.axhspan(70, 100, color=C_RED, alpha=0.12)
    ax2.axhspan(0, 30, color=C_GREEN, alpha=0.12)
    ax2.set_ylim(0, 100); ax2.set_yticks([30, 70])
    ax2.xaxis.set_major_formatter(DateFormatter("%d/%m"))
    ax2.margins(x=0.01)
    ax2.text(0.005, 0.82, "RSI 14", transform=ax2.transAxes, color=MUTED, fontsize=8)

    fig.text(0.03, 0.925, ticker, color=TXT, fontsize=20, fontweight="bold")
    fig.text(0.03, 0.875, f"{fmt(a['price'])}   {pct(a['mom3m'])} sur 3 mois",
             color=MUTED, fontsize=10)
    fig.text(0.92, 0.925, a["label"], color=col, fontsize=17, fontweight="bold", ha="right")
    fig.text(0.92, 0.875, f"Score {a['score']:+d} / 100", color=MUTED, fontsize=10, ha="right")
    return _png(fig)


def chart_top(items: list[tuple[str, dict, pd.DataFrame]]) -> io.BytesIO:
    """Planche de mini-cartes : une par opportunité."""
    items = items[:6]
    cols = 3 if len(items) > 2 else max(len(items), 1)
    rows = (len(items) + cols - 1) // cols
    fig = Figure(figsize=(3.3 * cols, 2.3 * rows))
    fig.subplots_adjust(left=0.02, right=0.98, top=0.97, bottom=0.03, wspace=0.06, hspace=0.1)
    for i, (t, a, df) in enumerate(items):
        ax = fig.add_subplot(rows, cols, i + 1)
        ax.set_facecolor(PANEL)
        for s in ax.spines.values():
            s.set_visible(False)
        ax.set_xticks([]); ax.set_yticks([])
        c = df["Close"].astype(float).tail(90).to_numpy()
        col = _hex(a["score"])
        x = np.arange(len(c))
        lo, hi = c.min(), c.max()
        y = (c - lo) / (hi - lo or 1) * 0.5 + 0.06     # courbe dans la moitié basse
        ax.plot(x, y, color=col, linewidth=2)
        ax.fill_between(x, y, 0, color=col, alpha=0.15)
        ax.set_ylim(0, 1.05); ax.set_xlim(0, len(c) - 1)
        ax.text(0.06, 0.86, t, transform=ax.transAxes, color=TXT, fontsize=14, fontweight="bold")
        ax.text(0.94, 0.87, pct(a["mom3m"]), transform=ax.transAxes, fontsize=10, ha="right",
                color=C_GREEN if a["mom3m"] >= 0 else C_RED)
        sub = a["label"] + (f"  •  {a['montant']:.0f} €" if a["montant"] else "")
        ax.text(0.06, 0.72, sub, transform=ax.transAxes, color=col, fontsize=9.5, fontweight="bold")
    return _png(fig)


# ---------------------------------------------------------------- embeds
def embed_analyse(ticker: str, a: dict) -> discord.Embed:
    e = discord.Embed(color=couleur(a["score"]),
                      title=f"{ICONE[a['kind']]}  {ticker}  ·  {a['decision']}",
                      description=f"`{jauge(a['score'])}`  **{a['score']:+d} / 100**\n\n"
                                  + "\n".join(a["why"]))
    e.add_field(name="Cours", value=f"**{fmt(a['price'])}**")
    e.add_field(name="3 mois", value=pct(a["mom3m"]))
    e.add_field(name="Volatilité", value=f"{a['vol']:.0%} / an")
    if a["montant"] > 0:
        e.add_field(name="💶 Mise indicative", value=f"**{a['montant']:.0f} €**")
        e.add_field(name="🛑 Stop", value=fmt(a["stop"]))
        e.add_field(name="🎯 Objectif", value=fmt(a["objectif"]))
    e.set_image(url="attachment://analyse.png")
    e.set_footer(text=LEGAL_COURT)
    return e


def note(score: int) -> str:
    return f"{score:+d}/100".replace("-", "−")


def signaux(n: int, sens: str) -> str:
    return f"**{n} {'signal' if n == 1 else 'signaux'} {sens}**"


def bloc_achat(t: str, a: dict) -> str:
    """Un signal d'achat expliqué pas à pas : combien, à quel prix, quand sortir, pourquoi."""
    d, p = devise(t), a["price"]
    l = [f"{ICONE[a['kind']]} {nom(t)}",
         f"{a['emoji']} **Signal d'{'achat fort' if a['score'] >= 40 else 'achat'}** · "
         f"note {note(a['score'])}",
         f"💶 Mise indicative : **{a['montant']:.0f} €**",
         f"📍 Cours actuel : **{prix(p, d)}** ({pct(a['mom3m'])} en 3 mois)",
         f"🛑 Stop : **{prix(a['stop'], d)}** ({pct(a['stop'] / p - 1)}) → revendre si le cours "
         "descend jusque-là",
         f"🎯 Objectif : **{prix(a['objectif'], d)}** ({pct(a['objectif'] / p - 1)}) → encaisser "
         "le gain à ce niveau"]
    if a["kind"] == "immo" and a["rendement"] > 0:
        l.append("💰 Dividende : " + f"{a['rendement']:.1%}".replace(".", ",")
                 + " par an (revenu versé aux actionnaires)")
    if a["plus"]:
        l.append("💡 Pourquoi : " + " ; ".join(a["plus"][:3]) + ".")
    if a["moins"]:
        l.append("⚠️ À surveiller : " + " ; ".join(a["moins"][:2]) + ".")
    return "\n".join(l)


def bloc_vente(t: str, a: dict) -> str:
    fort = a["score"] <= -40
    l = [f"{ICONE[a['kind']]} {nom(t)}",
         f"{a['emoji']} **{'Signal de vente' if fort else "Signal d'allègement"}** · "
         f"note {note(a['score'])} → "
         + ("vendre la position" if fort else "en vendre une partie"),
         f"📍 Cours actuel : **{prix(a['price'], devise(t))}** ({pct(a['mom3m'])} en 3 mois)"]
    if a["moins"]:
        l.append("💡 Pourquoi : " + " ; ".join(a["moins"][:3]) + ".")
    return "\n".join(l)


async def embed_marche(titre: str, n_achats: int, n_ventes: int) -> discord.Embed:
    """Premier message : résumé en une phrase, puis l'évolution des marchés expliquée."""
    data, ctx = await get_data(list(INDICES.values())), await get_ctx()
    now = dt.datetime.now(PARIS)
    if n_achats and n_ventes:
        bref = (f"{signaux(n_achats, "d'achat")} et {signaux(n_ventes, 'de vente')}. "
                "Le détail est dans les messages suivants.")
    elif n_achats:
        bref = f"{signaux(n_achats, "d'achat")}, aucun signal de vente. Détail juste en dessous."
    elif n_ventes:
        bref = (f"**Rien à acheter**, et {signaux(n_ventes, 'de vente')} si tu possèdes "
                "ces actifs. Détail juste en dessous.")
    else:
        bref = "**Rien à faire aujourd'hui** : aucun actif ne déclenche de signal."
    e = discord.Embed(color=0x5865F2, title=f"📊  {titre}",
                      description=f"**{date_fr(now)}**\n\n📌 **En bref** : {bref}")

    lignes, sens = [], []
    for nom_i, t in INDICES.items():
        if t not in data or nom_i == "VIX":
            continue
        c = data[t]["Close"]
        j, m = float(c.iloc[-1] / c.iloc[-2] - 1), float(c.iloc[-1] / c.iloc[-22] - 1)
        if nom_i in ("CAC 40", "S&P 500", "Nasdaq"):
            sens.append(j >= 0)
        lignes.append(f"{'🟢' if j >= 0 else '🔴'} **{nom_i}** ({INDICES_EXPL[nom_i]}) : "
                      f"**{pct(j)}** sur la dernière séance, {pct(m)} sur un mois")
    if lignes:
        if sens and all(sens):
            lignes.append("➜ Séance positive pour les bourses.")
        elif sens and not any(sens):
            lignes.append("➜ Séance négative pour les bourses.")
        elif sens:
            lignes.append("➜ Séance contrastée : pas de direction nette.")
        e.add_field(name="🌍  Évolution des marchés", value="\n".join(lignes), inline=False)

    climat = []
    if ctx.get("fng"):
        v, lab = ctx["fng"]
        expl = ("les investisseurs ont très peur. Ces périodes ont souvent été de bons moments "
                "pour acheter, mais les prix peuvent encore baisser" if v <= 25 else
                "les investisseurs sont inquiets" if v < 45 else
                "ni peur ni euphorie" if v <= 55 else
                "les investisseurs sont confiants" if v < 75 else
                "le marché est euphorique, ce qui précède souvent une baisse")
        climat.append(f"🌡️ **Crypto : {lab}** ({v}/100) → {expl}.")
    if ctx.get("taux") is not None:
        t = ctx["taux"]
        if t > 0.1:
            climat.append(f"🏦 **Taux d'intérêt en hausse** ({pts(t)} pt en 3 mois) → emprunter "
                          "coûte plus cher, c'est défavorable à l'immobilier coté.")
        elif t < -0.1:
            climat.append(f"🏦 **Taux d'intérêt en baisse** ({pts(t)} pt en 3 mois) → emprunter "
                          "coûte moins cher, c'est favorable à l'immobilier coté.")
        else:
            climat.append("🏦 **Taux d'intérêt stables** → pas d'effet particulier sur l'immobilier coté.")
    if climat:
        e.add_field(name="🧭  Climat", value="\n".join(climat), inline=False)
    if now.weekday() >= 5:
        e.add_field(name="ℹ️  Week-end", inline=False, value=(
            "Les bourses sont fermées : pour les actions, ce sont les chiffres de vendredi, et un "
            "ordre passé maintenant ne serait exécuté qu'à la réouverture lundi. Seules les "
            "cryptos cotent en continu."))
    e.set_footer(text=LEGAL_COURT)
    return e


LEXIQUE = (
    "**Note** : de −100 à +100, synthèse de plusieurs indicateurs. À partir de +20 : signal "
    "d'achat. En dessous de −20 : signal de vente. Entre les deux : attendre.\n"
    "**Mise indicative** : montant suggéré, jamais plus de {budget:.0f} € par achat. Si une action "
    "coûte plus cher que la mise, il faut un courtier qui permet d'acheter une fraction d'action. "
    "Les cours en $ sont convertis en euros par le courtier.\n"
    "**Stop** : prix plancher fixé à l'avance. S'il est atteint, on revend pour limiter la perte.\n"
    "**Objectif** : prix visé pour encaisser son gain.\n"
    "**Pas de signal** : ne rien faire est une décision normale, et la plus fréquente.")


def _pages(blocs: list[str], limite: int = 3800) -> list[str]:
    """Assemble des blocs en une ou plusieurs descriptions, sous la limite d'un embed Discord."""
    pages, page = [], ""
    for b in blocs:
        b = b[:limite]
        if page and len(page) + len(b) + 2 > limite:
            pages.append(page)
            page = ""
        page += ("\n\n" if page else "") + b
    return pages + [page] if page else pages


async def build_alerte(titre: str, tickers: list[str]):
    """
    Construit l'alerte en trois messages (parfois quatre) : évolution des marchés, quoi
    acheter, quoi vendre.
    Renvoie (messages, vue, achats), où messages est une liste de (embed, fichier ou None).
    """
    res = await classement(tickers)
    data = await get_data(tickers)
    achats = [(t, a) for t, a in res if a["montant"] > 0]
    ventes = [(t, a) for t, a in reversed(res) if a["score"] <= -20][:4]

    def top(kinds, n):
        return [(t, a) for t, a in achats if a["kind"] in kinds][:n]

    sections = [("📈  ACTIONS & ETF", top(("action", "etf"), 3)),
                ("🪙  CRYPTO", top(("crypto",), 2)),
                ("🏠  IMMOBILIER COTÉ", top(("immo",), 2))]
    montres = [x for _, lst in sections for x in lst]
    messages = [(await embed_marche(titre, len(montres), len(ventes)), None)]

    # --- quoi acheter
    if montres:
        blocs = ["Chaque fiche dit **combien** miser, **à quel prix** et **quand sortir**. "
                 "Rien n'oblige à tout suivre : un seul achat à la fois suffit."]
        for nom_s, lst in sections:
            if lst:
                blocs.append(f"__**{nom_s}**__")
                blocs += [bloc_achat(t, a) for t, a in lst]
        pages = _pages(blocs)
        for k, page in enumerate(pages[:-1]):      # rare : trop de signaux pour un seul message
            e = discord.Embed(color=0x23A55A, description=page,
                              title="✅  Quoi faire : les signaux d'achat" + (" (suite)" if k else ""))
            e.set_footer(text=LEGAL_COURT)
            messages.append((e, None))
        e = discord.Embed(color=0x23A55A, description=pages[-1],
                          title="✅  Quoi faire : les signaux d'achat"
                                + (" (suite)" if len(pages) > 1 else ""))
        buf = await asyncio.to_thread(chart_top, [(t, a, data[t]) for t, a in montres])
        fichier = discord.File(buf, filename="top.png")
        e.set_image(url="attachment://top.png")
    else:
        e, fichier = discord.Embed(
            color=0xF0B232, title="⏸️  Quoi faire : rien à acheter",
            description="Aucun actif suivi ne remplit les critères d'achat aujourd'hui. "
                        "Ne rien faire est aussi une décision : mieux vaut attendre un bon "
                        "signal que d'acheter par impatience."), None
    e.set_footer(text=f"Max {BUDGET_MAX:.0f} € par achat • {LEGAL_COURT}")
    messages.append((e, fichier))

    # --- quoi vendre + aide à la lecture
    if ventes:
        blocs = ["**Ne te concerne que si tu possèdes déjà ces actifs.** Si tu n'en as pas : "
                 "rien à faire, simplement éviter d'en acheter pour l'instant."]
        blocs += [bloc_vente(t, a) for t, a in ventes]
        e = discord.Embed(color=0xF23F43, title="🔻  Si tu en possèdes : à vendre ou alléger",
                          description=_pages(blocs)[0])
    else:
        e = discord.Embed(color=0x4E5058, title="🔻  Rien à vendre",
                          description="Aucun actif suivi ne déclenche de signal de vente.")
    e.add_field(name="📖  Comment lire ces alertes", inline=False,
                value=LEXIQUE.format(budget=BUDGET_MAX))
    e.set_footer(text=LEGAL_COURT)
    messages.append((e, None))

    vue = AlerteView([(t, a) for t, a in montres + ventes])
    return messages, vue, achats


async def envoyer_alerte(send, messages, vue=None) -> None:
    """Envoie les messages dans l'ordre ; le menu interactif va sous le dernier."""
    for i, (e, fichier) in enumerate(messages):
        kw = {"embed": e}
        if fichier:
            kw["file"] = fichier
        if vue is not None and i == len(messages) - 1:
            kw["view"] = vue
        await send(**kw)


def embed_legal() -> discord.Embed:
    e = discord.Embed(title="⚖️  Mentions légales et avertissements", color=0x4E5058)
    e.add_field(name="Nature du service", inline=False, value=(
        "Outil d'information automatisé. Les signaux sont **génériques** : ils résultent de "
        "règles techniques publiques (moyennes mobiles, RSI, MACD, momentum, sentiment, taux) "
        "appliquées à l'identique pour tous. Ils ne tiennent compte ni de ta situation "
        "financière, ni de tes objectifs, ni de ta tolérance au risque. Ce n'est **pas un "
        "conseil en investissement personnalisé**. L'exploitant du bot n'est ni conseiller en "
        "investissements financiers (CIF), ni prestataire agréé, et n'est pas enregistré "
        "auprès de l'AMF ou de l'ORIAS."))
    e.add_field(name="Risques", inline=False, value=(
        "Investir comporte un **risque de perte en capital**, partielle ou totale. Les "
        "performances passées ne préjugent pas des performances futures. Les crypto-actifs "
        "sont très volatils. N'investis que l'argent dont tu n'as pas besoin. Tu restes seul "
        "responsable de tes décisions."))
    e.add_field(name="Transparence", inline=False, value=(
        "L'exploitant du bot peut détenir les actifs cités. Aucune rémunération n'est perçue "
        "des émetteurs ou plateformes. Données : Yahoo Finance (possiblement différées, usage "
        "personnel) et alternative.me. Elles peuvent contenir des erreurs."))
    e.add_field(name="Tes données (RGPD)", inline=False, value=(
        "Le bot enregistre localement ton identifiant Discord, ton acceptation, tes positions "
        "et tes alertes de prix, uniquement pour fonctionner. `/mesdonnees` pour les "
        "consulter, `/supprimer` pour tout effacer."))
    e.add_field(name="Avant d'investir", inline=False, value=(
        "Vérifie que ta plateforme est autorisée (listes blanches et noires de l'AMF sur "
        "amf-france.org). Service réservé aux personnes majeures."))
    return e


# ---------------------------------------------------------------- vues interactives
def autorise(inter: discord.Interaction) -> bool:
    return not OWNER_ID or inter.user.id == OWNER_ID


def introuvable(saisie: str) -> str:
    return (f"Ticker `{saisie[:20]}` introuvable ou historique insuffisant. Utilise le symbole "
            "Yahoo Finance, par exemple `NVDA`, `TTE.PA`, `BTC-EUR`.")


async def envoyer_analyse(inter: discord.Interaction, saisie: str, ephemeral: bool):
    ticker = norm(saisie)
    data = await get_data([ticker]) if ticker else {}
    if ticker not in data:
        return await inter.followup.send(introuvable(saisie), ephemeral=ephemeral)
    a = analyse(data[ticker], kind(ticker), await get_ctx())
    buf = await asyncio.to_thread(chart_analyse, ticker, data[ticker], a)
    await inter.followup.send(embed=embed_analyse(ticker, a), ephemeral=ephemeral,
                              file=discord.File(buf, filename="analyse.png"))


class AlerteView(discord.ui.View):
    def __init__(self, items: list[tuple[str, dict]] = ()):
        super().__init__(timeout=None)
        opts = [discord.SelectOption(label=t, emoji=a["emoji"],
                                     description=f"{a['label']} · score {a['score']:+d}")
                for t, a in items[:25]] or [discord.SelectOption(label="—")]
        sel = discord.ui.Select(custom_id="sig:detail", options=opts,
                                placeholder="🔎 Voir le détail et le graphique d'un actif")
        sel.callback = self.detail
        self.add_item(sel)
        btn = discord.ui.Button(label="Mentions légales", emoji="⚖️", custom_id="sig:legal",
                                style=discord.ButtonStyle.secondary)
        btn.callback = self.legal
        self.add_item(btn)
        self.add_item(discord.ui.Button(label="Vérifier une plateforme (AMF)",
                                        url="https://www.amf-france.org"))

    async def detail(self, inter: discord.Interaction):
        if not autorise(inter):
            return await inter.response.send_message("Ce bot est privé.", ephemeral=True)
        if str(inter.user.id) not in load()["consent"]:
            return await demander_consentement(inter)
        ticker = inter.data["values"][0]
        if ticker == "—":
            return await inter.response.send_message("Aucun actif dans cette alerte.", ephemeral=True)
        await inter.response.defer(ephemeral=True, thinking=True)
        await envoyer_analyse(inter, ticker, ephemeral=True)

    async def legal(self, inter: discord.Interaction):
        await inter.response.send_message(embed=embed_legal(), ephemeral=True)


class ConsentView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="J'ai lu et j'accepte", emoji="✅", custom_id="consent:ok",
                       style=discord.ButtonStyle.success)
    async def ok(self, inter: discord.Interaction, _):
        d = load()
        d["consent"][str(inter.user.id)] = dt.datetime.now(PARIS).isoformat(timespec="seconds")
        save(d)
        await inter.response.edit_message(
            content="✅ Conditions acceptées. Relance ta commande.", embed=None, view=None)


async def demander_consentement(inter: discord.Interaction):
    await inter.response.send_message(
        content="Avant la première utilisation, lis et accepte ces conditions :",
        embed=embed_legal(), view=ConsentView(), ephemeral=True)


class Tree(app_commands.CommandTree):
    """Contrôle d'accès : bot privé (OWNER_ID) + acceptation des mentions légales."""

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if not autorise(inter):
            await inter.response.send_message("Ce bot est privé.", ephemeral=True)
            return False
        nom = inter.command.name if inter.command else ""
        if nom in ("legal", "supprimer") or str(inter.user.id) in load()["consent"]:
            return True
        await demander_consentement(inter)
        return False

    async def on_error(self, inter: discord.Interaction, error: app_commands.AppCommandError):
        """Toute erreur imprévue est journalisée et l'utilisateur reçoit une réponse claire."""
        if isinstance(error, app_commands.CheckFailure):
            return
        log.exception("Erreur dans /%s", inter.command.name if inter.command else "?",
                      exc_info=error)
        msg = "⚠️ Une erreur est survenue. Réessaie dans un instant."
        try:
            if inter.response.is_done():
                await inter.followup.send(msg, ephemeral=True)
            else:
                await inter.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


# ---------------------------------------------------------------- bot
class Bot(discord.Client):
    def __init__(self):
        super().__init__(
            intents=discord.Intents.default(),
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
            activity=discord.Activity(type=discord.ActivityType.watching, name="les marchés • /aide"))
        self.tree = Tree(self)
        self._pret = False

    async def setup_hook(self):
        self.add_view(AlerteView())
        self.add_view(ConsentView())
        try:
            if GUILD_ID:
                g = discord.Object(id=GUILD_ID)
                self.tree.copy_global_to(guild=g)
                await self.tree.sync(guild=g)
            else:
                await self.tree.sync()
                log.info("Commandes synchronisées globalement : elles peuvent mettre jusqu'à 1 h "
                         "à apparaître. Renseigne GUILD_ID pour les avoir tout de suite.")
        except discord.Forbidden:
            log.error("Impossible d'enregistrer les commandes sur le serveur %s : le bot n'y est "
                      "pas invité (ou sans le scope applications.commands), ou GUILD_ID est faux.",
                      GUILD_ID)
        if CHANNEL_ID:
            alertes.start()
            surveillance.start()
        else:
            log.warning("CHANNEL_ID vide : alertes automatiques désactivées, commandes seules.")

    async def on_ready(self):
        log.info("Connecté : %s • %d serveur(s)", self.user, len(self.guilds))
        if self._pret:          # on_ready est rappelé à chaque reconnexion
            return
        self._pret = True
        perms = discord.Permissions(view_channel=True, send_messages=True,
                                    embed_links=True, attach_files=True)
        log.info("Lien d'invitation : %s", discord.utils.oauth_url(
            self.user.id, permissions=perms, scopes=("bot", "applications.commands")))
        if CHANNEL_ID:
            chan = await salon()
            if chan is not None:
                moi = chan.guild.me if getattr(chan, "guild", None) else None
                p = chan.permissions_for(moi) if moi else None
                if p and not (p.send_messages and p.embed_links and p.attach_files):
                    log.error("Permissions manquantes dans #%s : il faut Envoyer des messages, "
                              "Intégrer des liens et Joindre des fichiers.", chan)


async def salon():
    """Salon des alertes ; None (avec un message clair dans les logs) s'il est inaccessible."""
    chan = bot.get_channel(CHANNEL_ID)
    if chan is None:
        try:
            chan = await bot.fetch_channel(CHANNEL_ID)
        except discord.HTTPException as exc:
            log.error("Salon CHANNEL_ID=%s inaccessible (%s). Vérifie l'identifiant et que le bot "
                      "voit ce salon.", CHANNEL_ID, exc)
            return None
    return chan


async def ac_ticker(inter: discord.Interaction, courant: str) -> list[app_commands.Choice[str]]:
    """Suggestions pendant la saisie : actifs suivis (la saisie libre reste possible)."""
    c = courant.upper().strip()
    connus = all_tickers()
    choix = [t for t in connus if t.startswith(c)] + [t for t in connus if c in t and not t.startswith(c)]
    return [app_commands.Choice(name=t, value=t) for t in choix[:25]]


bot = Bot()
THEME_CHOICES = [app_commands.Choice(name=k, value=k) for k in THEMES]


@bot.tree.command(name="analyse", description="Analyse complète d'un actif avec graphique")
@app_commands.describe(ticker="Symbole Yahoo Finance, ex : NVDA, TTE.PA, BTC-EUR, URW.PA")
@app_commands.autocomplete(ticker=ac_ticker)
async def analyse_cmd(inter: discord.Interaction, ticker: str):
    await inter.response.defer()
    await envoyer_analyse(inter, ticker, ephemeral=False)


@bot.tree.command(description="Les meilleurs signaux du moment, tous thèmes ou un seul")
@app_commands.choices(theme=THEME_CHOICES)
async def top(inter: discord.Interaction, theme: app_commands.Choice[str] | None = None):
    await inter.response.defer()
    tickers = THEMES[theme.value] if theme else all_tickers()
    messages, vue, _ = await build_alerte(
        f"Signaux — {theme.value if theme else 'tous thèmes'}", tickers)
    await envoyer_alerte(inter.followup.send, messages, vue)


@bot.tree.command(description="Météo des marchés : indices, or, bitcoin, sentiment, taux")
async def marche(inter: discord.Interaction):
    await inter.response.defer()
    data = await get_data(list(INDICES.values()))
    e = discord.Embed(title="🌍  Météo des marchés", color=0x5865F2,
                      description=f"**{date_fr(dt.datetime.now(PARIS))}**")
    for nom, t in INDICES.items():
        if t in data:
            c = data[t]["Close"]
            j, m = float(c.iloc[-1] / c.iloc[-2] - 1), float(c.iloc[-1] / c.iloc[-22] - 1)
            e.add_field(name=f"{'🟢' if j >= 0 else '🔴'} {nom}",
                        value=f"**{fmt(float(c.iloc[-1]))}**\nJour {pct(j)}\nMois {pct(m)}")
    ctx = await get_ctx()
    if ctx.get("fng"):
        e.add_field(name="🌡️ Sentiment crypto", inline=False,
                    value=f"`{jauge(ctx['fng'][0] * 2 - 100)}`  **{ctx['fng'][1]}** ({ctx['fng'][0]}/100)")
    e.set_footer(text=LEGAL_COURT)
    await inter.followup.send(embed=e)


@bot.tree.command(description="Simule un investissement mensuel régulier (DCA) sur 2 ans")
@app_commands.describe(ticker="Symbole", mensuel="Montant investi chaque mois en €")
@app_commands.autocomplete(ticker=ac_ticker)
async def dca(inter: discord.Interaction, ticker: str, mensuel: float = 50.0):
    await inter.response.defer()
    saisie, ticker = ticker, norm(ticker)
    data = await get_data([ticker]) if ticker else {}
    if ticker not in data:
        return await inter.followup.send(introuvable(saisie))
    mensuel = min(max(mensuel, 1), BUDGET_MAX)
    close = data[ticker]["Close"].astype(float)
    achats = close.groupby([close.index.year, close.index.month]).first()
    investi = mensuel * len(achats)
    valeur = float((mensuel / achats).sum() * close.iloc[-1])
    e = discord.Embed(title=f"🗓️  DCA sur {ticker}", color=couleur(40 if valeur >= investi else -40),
                      description=f"**{mensuel:.0f} € par mois** pendant {len(achats)} mois, "
                                  "achetés le premier jour de cotation du mois.")
    e.add_field(name="Investi", value=f"{investi:.0f} €")
    e.add_field(name="Valeur aujourd'hui", value=f"**{valeur:.0f} €**")
    e.add_field(name="Résultat", value=f"{valeur - investi:+.0f} € ({pct(valeur / investi - 1)})")
    e.set_footer(text="Simulation sur le passé, hors frais et impôts. " + LEGAL_COURT)
    await inter.followup.send(embed=e)


@bot.tree.command(description="Enregistre un achat pour suivre ta position")
@app_commands.describe(ticker="Symbole", prix="Prix d'achat unitaire", montant="Montant investi en €")
@app_commands.autocomplete(ticker=ac_ticker)
async def achat(inter: discord.Interaction, ticker: str, prix: float, montant: float):
    saisie, ticker = ticker, norm(ticker)
    if not ticker:
        return await inter.response.send_message(introuvable(saisie), ephemeral=True)
    if montant > BUDGET_MAX:
        return await inter.response.send_message(
            f"Ta règle : {BUDGET_MAX:.0f} € max par achat. Achat non enregistré.", ephemeral=True)
    if prix <= 0 or montant <= 0:
        return await inter.response.send_message("Prix et montant doivent être positifs.", ephemeral=True)
    d = load()
    d["positions"].setdefault(str(inter.user.id), []).append(
        {"ticker": ticker, "prix": prix, "montant": montant, "date": today().isoformat()})
    save(d)
    await inter.response.send_message(
        f"✅ Achat noté : {montant:.0f} € de {ticker} à {fmt(prix)}.", ephemeral=True)


@bot.tree.command(description="Retire une position (vendue)")
async def vente(inter: discord.Interaction, ticker: str):
    d, uid, t = load(), str(inter.user.id), ticker.upper().strip()[:20]
    avant = d["positions"].get(uid, [])
    apres = [x for x in avant if x["ticker"] != t]
    if len(apres) == len(avant):
        return await inter.response.send_message(f"Aucune position sur {t}.", ephemeral=True)
    if apres:
        d["positions"][uid] = apres
    else:
        d["positions"].pop(uid, None)
    save(d)
    await inter.response.send_message(f"✅ {t} retiré.", ephemeral=True)


def etat_position(pnl: float, a: dict) -> str:
    if pnl <= -0.15:
        return "🔴 VENDRE · perte > 15 %, règle de coupe"
    if a["score"] <= -40:
        return "🔴 VENDRE · signal très négatif"
    if pnl >= 0.20 and a["rsi"] > 75:
        return "🟠 SÉCURISER LA MOITIÉ · suracheté"
    if a["score"] <= -20:
        return "🟠 ALLÉGER"
    if a["montant"] > 0:
        return f"🟢 CONSERVER · renfort possible {a['montant']:.0f} €"
    return "🟡 CONSERVER"


@bot.tree.command(description="Tes positions : gain/perte, signal et impôt estimé")
async def positions(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    mine = load()["positions"].get(str(inter.user.id), [])
    if not mine:
        return await inter.followup.send("Aucune position. Utilise `/achat` après chaque achat.")
    data, ctx = await get_data([x["ticker"] for x in mine]), await get_ctx()
    lignes, total, valeur = [], 0.0, 0.0
    for x in mine:
        total += x["montant"]
        if x["ticker"] not in data:
            valeur += x["montant"]
            lignes.append(f"**{x['ticker']}** · données indisponibles")
            continue
        a = analyse(data[x["ticker"]], kind(x["ticker"]), ctx)
        pnl = a["price"] / x["prix"] - 1
        valeur += x["montant"] * (1 + pnl)
        lignes.append(f"{ICONE[a['kind']]} **{x['ticker']}** · {x['montant']:.0f} € → "
                      f"**{pct(pnl)}**\n╰ {etat_position(pnl, a)}")
    gain = valeur - total
    e = discord.Embed(title="💼  Tes positions", description="\n".join(lignes),
                      color=couleur(40 if gain >= 0 else -40))
    e.add_field(name="Investi", value=f"{total:.0f} €")
    e.add_field(name="Valeur", value=f"**{valeur:.0f} €**")
    e.add_field(name="Résultat", value=f"{gain:+.0f} € ({pct(valeur / total - 1)})")
    if gain > 0:
        e.add_field(name="Impôt estimé si tu vends tout", inline=False,
                    value=f"~{gain * TAUX_PFU / 100:.0f} € (flat tax {TAUX_PFU:g} %, hors PEA). Voir `/fiscalite`.")
    e.set_footer(text=LEGAL_COURT)
    await inter.followup.send(embed=e)


@bot.tree.command(description="Crée une alerte de prix : le bot te prévient quand le seuil est franchi")
@app_commands.describe(ticker="Symbole", seuil="Prix à surveiller")
@app_commands.autocomplete(ticker=ac_ticker)
async def alerte(inter: discord.Interaction, ticker: str, seuil: float):
    await inter.response.defer(ephemeral=True)
    if not CHANNEL_ID:
        return await inter.followup.send("Les alertes de prix nécessitent un salon : `CHANNEL_ID` "
                                         "n'est pas configuré sur ce bot.")
    if seuil <= 0:
        return await inter.followup.send("Le seuil doit être positif.")
    saisie, ticker = ticker, norm(ticker)
    data = await get_data([ticker]) if ticker else {}
    if ticker not in data:
        return await inter.followup.send(introuvable(saisie))
    prix = float(data[ticker]["Close"].iloc[-1])
    d = load()
    if sum(a["uid"] == inter.user.id for a in d["alertes"]) >= MAX_ALERTES:
        return await inter.followup.send(
            f"Tu as déjà {MAX_ALERTES} alertes actives. Retires-en avec `/stopalerte`.")
    d["alertes"].append({"uid": inter.user.id, "ticker": ticker, "seuil": seuil,
                         "sens": "haut" if seuil > prix else "bas"})
    save(d)
    await inter.followup.send(
        f"🔔 Alerte créée : {ticker} {'≥' if seuil > prix else '≤'} {fmt(seuil)} (cours actuel {fmt(prix)}).")


@bot.tree.command(description="Supprime tes alertes de prix sur un actif")
@app_commands.describe(ticker="Symbole dont tu veux retirer les alertes")
async def stopalerte(inter: discord.Interaction, ticker: str):
    d, t = load(), ticker.upper().strip()[:20]
    garde = [a for a in d["alertes"] if not (a["uid"] == inter.user.id and a["ticker"] == t)]
    n = len(d["alertes"]) - len(garde)
    if n:
        d["alertes"] = garde
        save(d)
    await inter.response.send_message(
        f"🔕 {n} alerte(s) supprimée(s) sur {t}." if n else f"Aucune alerte sur {t}.", ephemeral=True)


@bot.tree.command(description="Ajoute un actif à la liste suivie par les alertes")
async def suivre(inter: discord.Interaction, ticker: str):
    await inter.response.defer(ephemeral=True)
    saisie, ticker = ticker, norm(ticker)
    if not ticker or ticker not in await get_data([ticker]):
        return await inter.followup.send(introuvable(saisie))
    d = load()
    if ticker in all_tickers():
        return await inter.followup.send(f"{ticker} est déjà suivi.")
    if len(d["watch"]) >= MAX_WATCH:
        return await inter.followup.send(f"Liste pleine ({MAX_WATCH} actifs ajoutés).")
    d["watch"].append(ticker)
    save(d)
    await inter.followup.send(f"✅ {ticker} est maintenant suivi.")


@bot.tree.command(description="Bilan des signaux d'achat passés du bot : ont-ils vraiment marché ?")
async def performance(inter: discord.Interaction):
    await inter.response.defer()
    j = load()["journal"]
    if not j:
        return await inter.followup.send("Pas encore d'historique : il se remplit à chaque alerte automatique.")
    data = await get_data([x["ticker"] for x in j])
    perfs = [(x, float(data[x["ticker"]]["Close"].iloc[-1]) / x["prix"] - 1)
             for x in j if x["ticker"] in data]
    if not perfs:
        return await inter.followup.send("Données indisponibles pour le moment.")
    vals = [p for _, p in perfs]
    best, worst = max(perfs, key=lambda x: x[1]), min(perfs, key=lambda x: x[1])
    moy = float(np.mean(vals))
    e = discord.Embed(title="🧾  Bilan des signaux d'achat", color=couleur(40 if moy >= 0 else -40),
                      description=f"{len(perfs)} signaux depuis le {j[0]['date']}")
    e.add_field(name="Performance moyenne", value=f"**{pct(moy)}**")
    e.add_field(name="Signaux gagnants", value=f"{sum(v > 0 for v in vals) / len(vals):.0%}")
    e.add_field(name="​", value="​")
    e.add_field(name="Meilleur", value=f"{best[0]['ticker']} {pct(best[1])}")
    e.add_field(name="Pire", value=f"{worst[0]['ticker']} {pct(worst[1])}")
    e.set_footer(text="Affiché tel quel, bons et mauvais signaux compris. " + LEGAL_COURT)
    await inter.followup.send(embed=e)


@bot.tree.command(description="Estimation de l'impôt sur une plus-value (France)")
@app_commands.describe(gain="Plus-value réalisée en €")
async def fiscalite(inter: discord.Interaction, gain: float):
    e = discord.Embed(title="🧮  Fiscalité des plus-values (France)", color=0x4E5058)
    e.add_field(name="Estimation", inline=False,
                value=f"Gain de {gain:.0f} € → impôt ~**{max(gain, 0) * TAUX_PFU / 100:.0f} €** "
                      f"au prélèvement forfaitaire unique de {TAUX_PFU:g} %.")
    e.add_field(name="À savoir", inline=False, value=(
        "• **Compte-titres** : flat tax sur les gains nets de l'année (moins-values déductibles).\n"
        "• **PEA** : pas d'impôt sur le revenu après 5 ans, prélèvements sociaux seulement.\n"
        "• **Crypto** : imposée à la conversion en euros, pas lors d'un échange crypto contre "
        "crypto. Petites cessions annuelles exonérées sous un seuil. Comptes sur plateformes "
        "étrangères à déclarer.\n"
        "• Tout se déclare chaque année avec ta déclaration de revenus."))
    e.set_footer(text="Estimation indicative, pas un conseil fiscal. Règles et taux à vérifier sur impots.gouv.fr.")
    await inter.response.send_message(embed=e, ephemeral=True)


@bot.tree.command(description="Mentions légales, risques et données personnelles")
async def legal(inter: discord.Interaction):
    await inter.response.send_message(embed=embed_legal(), ephemeral=True)


@bot.tree.command(description="Affiche toutes les données que le bot conserve sur toi")
async def mesdonnees(inter: discord.Interaction):
    d, uid = load(), str(inter.user.id)
    export = {"acceptation": d["consent"].get(uid), "positions": d["positions"].get(uid, []),
              "alertes": [a for a in d["alertes"] if a["uid"] == inter.user.id]}
    buf = io.BytesIO(json.dumps(export, indent=2, ensure_ascii=False).encode())
    await inter.response.send_message("Voici tes données :", ephemeral=True,
                                      file=discord.File(buf, filename="mes_donnees.json"))


@bot.tree.command(description="Efface toutes tes données (positions, alertes, acceptation)")
async def supprimer(inter: discord.Interaction):
    d, uid = load(), str(inter.user.id)
    d["consent"].pop(uid, None); d["positions"].pop(uid, None)
    d["alertes"] = [a for a in d["alertes"] if a["uid"] != inter.user.id]
    save(d)
    await inter.response.send_message("🗑️ Toutes tes données ont été effacées.", ephemeral=True)


@bot.tree.command(description="Liste des thèmes et actifs suivis")
async def themes(inter: discord.Interaction):
    e = discord.Embed(title="🗂️  Actifs suivis", color=0x5865F2)
    for k, v in THEMES.items():
        e.add_field(name=k.capitalize(), value=" · ".join(f"`{t}`" for t in v), inline=False)
    perso = load()["watch"]
    if perso:
        e.add_field(name="Ajoutés avec /suivre", value=" · ".join(f"`{t}`" for t in perso), inline=False)
    await inter.response.send_message(embed=e)


@bot.tree.command(description="Toutes les commandes du bot")
async def aide(inter: discord.Interaction):
    e = discord.Embed(title="📖  Commandes", color=0x5865F2, description=(
        "**Signaux**\n`/top` · `/analyse` · `/marche` · `/themes` · `/suivre`\n\n"
        "**Ton portefeuille**\n`/achat` · `/vente` · `/positions` · `/alerte` · `/stopalerte`\n\n"
        "**Outils**\n`/dca` · `/performance` · `/fiscalite`\n\n"
        "**Légal et données**\n`/legal` · `/mesdonnees` · `/supprimer`"))
    await inter.response.send_message(embed=e, ephemeral=True)


# ---------------------------------------------------------------- tâches automatiques
@tasks.loop(time=[dt.time(9, 15, tzinfo=PARIS), dt.time(15, 45, tzinfo=PARIS),
                  dt.time(20, 30, tzinfo=PARIS)])
async def alertes():
    """9h15 ouverture Paris, 15h45 ouverture US, 20h30 point crypto. Week-end : crypto seule."""
    chan = await salon()
    if chan is None:
        return
    now = dt.datetime.now(PARIS)
    crypto_seule = now.weekday() >= 5 or now.hour >= 20
    try:
        messages, vue, achats = await build_alerte(
            "Point crypto" if crypto_seule else "Signaux du marché",
            THEMES["crypto"] if crypto_seule else all_tickers())
        await envoyer_alerte(chan.send, messages, vue)
        d, limite = load(), (today() - dt.timedelta(days=7)).isoformat()
        for t, a in achats:      # journal pour /performance, un signal par actif et par semaine
            if not any(x["ticker"] == t and x["date"] >= limite for x in d["journal"]):
                d["journal"].append({"ticker": t, "date": today().isoformat(),
                                     "prix": a["price"]})
        d["journal"] = d["journal"][-MAX_JOURNAL:]
        save(d)
    except Exception:  # ne jamais tuer la boucle
        log.exception("Erreur pendant l'alerte automatique")


@tasks.loop(minutes=20)
async def surveillance():
    """Alertes de prix et signaux de vente sur les positions enregistrées."""
    chan = await salon()
    if chan is None:
        return
    parties, notifiees = [], []      # ce qui a réellement été envoyé pendant ce passage
    try:
        d = load()
        tick = [a["ticker"] for a in d["alertes"]] + \
               [x["ticker"] for lst in d["positions"].values() for x in lst]
        if not tick:
            return
        data, ctx = await get_data(tick), await get_ctx()
        for al in d["alertes"]:
            df = data.get(al["ticker"])
            prix = float(df["Close"].iloc[-1]) if df is not None else None
            if prix and ((al["sens"] == "haut" and prix >= al["seuil"]) or
                         (al["sens"] == "bas" and prix <= al["seuil"])):
                e = discord.Embed(color=0xF0B232, title=f"🔔  {al['ticker']} a franchi {fmt(al['seuil'])}",
                                  description=f"Cours actuel : **{fmt(prix)}**")
                e.set_footer(text=LEGAL_COURT)
                await chan.send(content=f"<@{al['uid']}>", embed=e)
                parties.append(al)
        jour = today().isoformat()
        for uid, lst in d["positions"].items():
            for x in lst:
                if x["ticker"] not in data or x.get("notif") == jour:
                    continue
                a = analyse(data[x["ticker"]], kind(x["ticker"]), ctx)
                pnl = a["price"] / x["prix"] - 1
                etat = etat_position(pnl, a)
                if etat.startswith(("🔴", "🟠")):
                    e = discord.Embed(color=0xF23F43, title=f"⚠️  {x['ticker']} · position à revoir",
                                      description=f"{etat}\nDepuis ton achat : **{pct(pnl)}** · "
                                                  f"cours {fmt(a['price'])}")
                    e.set_footer(text=LEGAL_COURT)
                    await chan.send(content=f"<@{uid}>", embed=e)
                    notifiees.append((uid, {k: v for k, v in x.items() if k != "notif"}))
    except Exception:
        log.exception("Erreur pendant la surveillance")
    finally:
        # On relit le fichier avant d'écrire : une commande (/achat, /alerte…) a pu le modifier
        # pendant les téléchargements, et il ne faut pas écraser ce qu'elle a enregistré.
        # Ce qui est déjà parti est noté même si un envoi suivant a échoué.
        if parties or notifiees:
            d, jour = load(), today().isoformat()
            for al in parties:
                if al in d["alertes"]:
                    d["alertes"].remove(al)
            for uid, ref in notifiees:
                for x in d["positions"].get(uid, []):
                    if {k: v for k, v in x.items() if k != "notif"} == ref:
                        x["notif"] = jour
            save(d)


@alertes.before_loop
@surveillance.before_loop
async def _attente():
    await bot.wait_until_ready()


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN manquant : copie .env.example en .env et colle ton token.")
    try:
        bot.run(TOKEN, log_handler=None)
    except discord.LoginFailure:
        raise SystemExit("Token refusé par Discord : régénère-le (onglet Bot → Reset Token) "
                         "et mets-le à jour dans .env.")
