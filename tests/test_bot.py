"""Tests hors ligne : aucune connexion à Discord ni à Yahoo Finance n'est nécessaire."""
import asyncio
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["GUILD_ID"] = ""          # une valeur vide ne doit pas faire planter l'import
import bot  # noqa: E402


def serie(n=400, pente=0.001, bruit=0.01, seed=1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(pente, bruit, n)))
    idx = pd.bdate_range(end="2026-01-30", periods=n)
    return pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99,
                         "Close": close, "Dividends": 0.0}, index=idx)


@pytest.fixture(autouse=True)
def donnees_isolees(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "DATA_FILE", tmp_path / "data.json")
    bot._cache.clear()
    bot._echecs.clear()


def test_env_vide_ou_invalide(monkeypatch):
    monkeypatch.setenv("X_TEST", "")
    assert bot._env("X_TEST", 7) == 7
    monkeypatch.setenv("X_TEST", " 31,4 ")
    assert bot._env("X_TEST", 0, float) == 31.4
    monkeypatch.setenv("X_TEST", "abc")
    with pytest.raises(SystemExit):
        bot._env("X_TEST", 0)


@pytest.mark.parametrize("brut,attendu", [
    (" nvda ", "NVDA"), ("tte.pa", "TTE.PA"), ("btc-eur", "BTC-EUR"), ("^fchi", "^FCHI"),
    ("GC=F", "GC=F"), ("", None), ("A B", None), ("../etc", None), ("X" * 30, None),
])
def test_norm(brut, attendu):
    assert bot.norm(brut) == attendu


def test_analyse_tendances():
    haut = bot.analyse(serie(pente=0.004, bruit=0.012))
    bas = bot.analyse(serie(pente=-0.004, bruit=0.012))
    assert -100 <= bas["score"] < haut["score"] <= 100
    assert bas["montant"] == 0
    assert haut["stop"] < haut["price"] < haut["objectif"]
    assert haut["montant"] <= bot.BUDGET_MAX


def test_analyse_historique_court_et_contexte():
    a = bot.analyse(serie(n=80), "crypto", {"fng": (10, "Peur extrême")})
    assert any("Historique trop court" in w for w in a["why"])
    assert any("Sentiment crypto" in w for w in a["why"])
    b = bot.analyse(serie(), "immo", {"taux": -0.5})
    assert any("Taux 10 ans en baisse" in w for w in b["why"])


def test_graphiques_png():
    df = serie(pente=0.004)
    a = bot.analyse(df)
    assert bot.chart_analyse("TEST", df, a).getvalue()[:4] == b"\x89PNG"
    assert bot.chart_top([("TEST", a, df)] * 4).getvalue()[:4] == b"\x89PNG"


def test_embeds_dans_les_limites_discord():
    a = bot.analyse(serie(pente=0.004))
    e = bot.embed_analyse("TEST", a)
    assert len(e) <= 6000 and len(e.description) <= 4096
    legal = bot.embed_legal()
    assert len(legal) <= 6000 and all(len(f.value) <= 1024 for f in legal.fields)


def test_stockage_atomique_et_fichier_corrompu():
    d = bot.load()
    d["watch"].append("NVDA")
    bot.save(d)
    assert json.loads(bot.DATA_FILE.read_text(encoding="utf-8"))["watch"] == ["NVDA"]
    assert [p.name for p in bot.DATA_FILE.parent.iterdir()] == ["data.json"]   # pas de .tmp oublié
    bot.DATA_FILE.write_text("{pas du json")
    assert bot.load()["watch"] == []
    assert any(p.name.startswith("data.corrompu-") for p in bot.DATA_FILE.parent.iterdir())


def test_cache_et_tickers_introuvables(monkeypatch):
    appels = []

    def faux(tickers):
        appels.append(list(tickers))
        return {t: serie() for t in tickers if t != "BIDON"}

    monkeypatch.setattr(bot, "_download", faux)

    async def scenario():
        a, b = await asyncio.gather(bot.get_data(["AAA", "BIDON"]), bot.get_data(["AAA", "BIDON"]))
        assert set(a) == set(b) == {"AAA"}
        await bot.get_data(["aaa", "BIDON"])
    asyncio.run(scenario())
    assert appels == [["AAA", "BIDON"]]      # un seul téléchargement, l'échec n'est pas retenté


def test_panne_reseau_sert_le_cache(monkeypatch):
    monkeypatch.setattr(bot, "_download", lambda t: {x: serie() for x in t})
    asyncio.run(bot.get_data(["AAA"]))
    bot._cache["AAA"] = (0.0, bot._cache["AAA"][1])            # données périmées

    def panne(_):
        raise ConnectionError("hors ligne")
    monkeypatch.setattr(bot, "_download", panne)
    assert "AAA" in asyncio.run(bot.get_data(["AAA"]))
    assert "AAA" not in bot._echecs


def test_etat_position():
    a = bot.analyse(serie(pente=0.004))
    assert bot.etat_position(-0.20, a).startswith("🔴")
    assert bot.etat_position(0.05, {**a, "score": -25}).startswith("🟠")
    assert bot.etat_position(0.05, {**a, "score": 0, "montant": 0}) == "🟡 CONSERVER"


# ------------------------------------------------------------ mode webhook (GitHub Actions)
import datetime as dt  # noqa: E402

import alertes_webhook as aw  # noqa: E402

UTC = dt.timezone.utc


@pytest.mark.parametrize("cron,jour,attendu", [
    ("15 7 * * *", dt.datetime(2026, 7, 1, 7, 40, tzinfo=UTC), "tout"),      # été, mercredi, retard
    ("15 8 * * *", dt.datetime(2026, 7, 1, 8, 15, tzinfo=UTC), "non"),       # été : 10h15, doublon
    ("15 7 * * *", dt.datetime(2026, 12, 1, 7, 15, tzinfo=UTC), "non"),      # hiver : 8h15
    ("15 8 * * *", dt.datetime(2026, 12, 1, 8, 15, tzinfo=UTC), "tout"),     # hiver, mardi
    ("45 13 * * *", dt.datetime(2026, 7, 1, 13, 45, tzinfo=UTC), "tout"),
    ("30 18 * * *", dt.datetime(2026, 7, 1, 18, 30, tzinfo=UTC), "crypto"),  # 20h30 : crypto
    ("30 19 * * *", dt.datetime(2026, 12, 1, 19, 30, tzinfo=UTC), "crypto"),
    ("15 7 * * *", dt.datetime(2026, 7, 4, 7, 15, tzinfo=UTC), "crypto"),    # samedi
])
def test_creneau_heure_de_paris(cron, jour, attendu):
    assert aw.contenu_du_creneau(cron, jour) == attendu


def test_contenu_demande(monkeypatch):
    monkeypatch.setenv("CRON", "")
    monkeypatch.setenv("CONTENU", "")
    assert aw.contenu_demande() == "tout"
    monkeypatch.setenv("CONTENU", "crypto")
    assert aw.contenu_demande() == "crypto"


def test_webhook_construit_une_alerte(monkeypatch):
    monkeypatch.setattr(bot, "_download", lambda ts: {
        t: serie(pente=0.003, seed=i) for i, t in enumerate(ts)})

    async def ctx():
        return {"fng": (20, "Peur extrême"), "taux": -0.4}
    monkeypatch.setattr(bot, "get_ctx", ctx)
    e, fichier = asyncio.run(aw.construire("tout"))
    assert e.title.endswith("Signaux du marché") and len(e) <= 6000
    assert "/legal" not in e.footer.text and "Pas un conseil" in e.footer.text
    assert fichier is not None and fichier.filename == "top.png"
    e, _ = asyncio.run(aw.construire("crypto"))
    assert e.title.endswith("Point crypto")


def test_webhook_refuse_d_envoyer_sans_cours(monkeypatch):
    monkeypatch.setattr(bot, "_download", lambda ts: {})

    async def sans_attente(_):
        return None
    monkeypatch.setattr(aw.asyncio, "sleep", sans_attente)
    with pytest.raises(RuntimeError, match="Cours indisponibles"):
        asyncio.run(aw.construire("crypto"))
