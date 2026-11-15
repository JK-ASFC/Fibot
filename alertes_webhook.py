"""
Mode « alertes seules » : calcule les signaux et les poste dans un salon Discord via un
webhook, puis s'arrête. Prévu pour un workflow GitHub planifié (.github/workflows/alertes.yml) :
aucun serveur à héberger, mais pas de commandes / ni de suivi de positions.

    DISCORD_WEBHOOK_URL=... python alertes_webhook.py            # signaux tous thèmes
    DISCORD_WEBHOOK_URL=... CONTENU=crypto python alertes_webhook.py
"""
import asyncio
import datetime as dt
import os
import sys
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")
# Horaires d'envoi, heure de Paris → True si point crypto seul (le soir, marchés actions fermés)
CRENEAUX = {(9, 15): False, (15, 45): False, (20, 30): True}


def contenu_du_creneau(cron: str, maintenant: dt.datetime | None = None) -> str:
    """
    GitHub planifie en UTC et ignore l'heure d'été : le workflow se déclenche donc à deux heures
    UTC par créneau, et on ne garde que celle qui tombe juste à Paris aujourd'hui.
    On raisonne sur l'heure prévue (le cron), pas sur l'heure réelle : GitHub démarre souvent
    en retard. Renvoie "tout", "crypto" ou "non".
    """
    minute, heure = (int(x) for x in cron.split()[:2])
    utc = (maintenant or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    local = utc.replace(hour=heure, minute=minute, second=0, microsecond=0).astimezone(PARIS)
    crypto = CRENEAUX.get((local.hour, local.minute))
    if crypto is None:
        return "non"
    return "crypto" if crypto or local.weekday() >= 5 else "tout"     # week-end : crypto seule


def contenu_demande() -> str:
    """Lancement manuel : CONTENU (tout / crypto). Lancement planifié : déduit de CRON."""
    cron = (os.getenv("CRON") or "").strip()
    if cron:
        return contenu_du_creneau(cron)
    return "crypto" if (os.getenv("CONTENU") or "").strip().lower() == "crypto" else "tout"


async def construire(contenu: str):
    """Messages de l'alerte (embed, fichier). Lève RuntimeError si les cours sont indisponibles."""
    import bot      # import tardif : la vérification du créneau n'a pas besoin des dépendances

    crypto = contenu == "crypto"
    tickers = bot.THEMES["crypto"] if crypto else bot.all_tickers()
    for essai in range(3):
        data = await bot.get_data(tickers)
        if len(data) * 2 >= len(tickers):
            break
        bot._echecs.clear()
        if essai < 2:
            await asyncio.sleep(20)
    else:
        # Sans cours, l'alerte afficherait « aucun signal » à tort : mieux vaut échouer clairement.
        raise RuntimeError(f"Cours indisponibles ({len(data)}/{len(tickers)} actifs reçus) : "
                           "Yahoo Finance est injoignable ou limite cette machine.")
    messages, _vue, _achats = await bot.build_alerte(
        "Point crypto" if crypto else "Signaux du marché", tickers)
    for e, _ in messages:      # pas de commandes en mode webhook : on retire le renvoi vers /legal
        e.set_footer(text=(e.footer.text or "").replace(" • /legal", ""))
    return messages


async def main() -> int:
    contenu = contenu_demande()
    if contenu == "non":
        print("Hors créneau à l'heure de Paris : rien à envoyer.")
        return 0
    url = (os.getenv("DISCORD_WEBHOOK_URL") or "").strip()
    if not url:
        print("DISCORD_WEBHOOK_URL manquant : crée un webhook dans les paramètres du salon et "
              "ajoute-le dans les secrets du dépôt (voir README).", file=sys.stderr)
        return 1

    import aiohttp
    import discord

    try:
        messages = await construire(contenu)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        try:
            webhook = discord.Webhook.from_url(url, session=session)
        except ValueError:
            print("DISCORD_WEBHOOK_URL n'a pas la forme d'un webhook Discord "
                  "(https://discord.com/api/webhooks/…).", file=sys.stderr)
            return 1
        try:
            for e, fichier in messages:
                await webhook.send(embed=e, username="Fibot", wait=True,
                                   **({"file": fichier} if fichier else {}))
        except discord.NotFound:
            print("Webhook introuvable : il a été supprimé ou l'URL est incomplète.", file=sys.stderr)
            return 1
    print(f"Alerte envoyée ({contenu}).")
    return 0


if __name__ == "__main__":
    if "--creneau" in sys.argv:        # utilisé par le workflow avant d'installer les dépendances
        print(contenu_demande())
        sys.exit(0)
    sys.exit(asyncio.run(main()))
