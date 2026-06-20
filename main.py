import os
import datetime
from threading import Thread

import discord
import dotenv
import firebase_admin
from discord.ext import commands, tasks
from firebase_admin import credentials, firestore
from flask import Flask

dotenv.load_dotenv()

# ==========================================
# CONFIGURATION
# ==========================================
GUILD_ID = 1443696372611027118

# Salon où sont annoncés les anniversaires du jour (avec @everyone)
GENERAL_CHANNEL_ID = 1443696373609398353

# Salon où vit le message récapitulatif (édité, jamais renvoyé)
BDAY_CHANNEL_ID = 1517287433685962902

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="+", intents=intents)

# Anniversaires déjà fêtés aujourd'hui (évite les doublons en cas de redémarrage)
anniversaires_fetes_aujourdhui = []
dernier_jour_verifie = None


# ==========================================
# CONNEXION FIREBASE
# ==========================================
cred = credentials.Certificate("firebase-creds.json")
firebase_admin.initialize_app(cred)
db = firestore.client()
print("🔥 Connexion à Firebase Firestore réussie.")


# ==========================================
# HELPERS
# ==========================================

async def update_birthday_list_message():
    """
    Construit l'embed listant tous les anniversaires et l'édite dans
    BDAY_CHANNEL_ID. L'ID du message est gardé dans Firebase
    (collection "config", document "birthday_list") pour survivre aux
    redémarrages. S'il est introuvable (jamais créé, ou supprimé), un
    nouveau message est créé et son ID est sauvegardé.
    """
    bday_channel = bot.get_channel(BDAY_CHANNEL_ID)
    if bday_channel is None:
        print(f"⚠️ Impossible de trouver le salon avec l'ID {BDAY_CHANNEL_ID}")
        return

    # Récupère tous les anniversaires enregistrés
    users_ref = db.collection("anniversaires").stream()
    anniversaires = [user.to_dict() for user in users_ref]

    # Tri par date (format MM-JJ, donc le tri textuel suffit)
    anniversaires.sort(key=lambda d: d.get("date", ""))

    embed = discord.Embed(
        title="📅 Liste des anniversaires",
        color=discord.Color.gold()
    )

    if anniversaires:
        lignes = [
            f"**{data.get('username', 'Inconnu')}**\n{data.get('readable_date', '??/??')}"
            for data in anniversaires
        ]
        embed.description = "\n\n".join(lignes)
    else:
        embed.description = "Aucun anniversaire enregistré pour l'instant."

    embed.set_footer(text=f"{len(anniversaires)} anniversaire(s) enregistré(s)")

    # Récupère l'ID du message déjà posté (s'il existe)
    config_ref = db.collection("config").document("birthday_list")
    config_doc = config_ref.get()
    message_id = config_doc.to_dict().get("message_id") if config_doc.exists else None

    if message_id:
        try:
            message = await bday_channel.fetch_message(message_id)
            await message.edit(embed=embed)
            return
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            # Message supprimé / inaccessible : on en recrée un nouveau ci-dessous
            pass

    new_message = await bday_channel.send(embed=embed)
    config_ref.set({"message_id": new_message.id})


# ==========================================
# COMMANDES DU BOT
# ==========================================

@bot.command(name="anniv")
async def register_birthday(ctx, date_str: str):
    """Enregistre un anniversaire au format JJ/MM (ex: +anniv 25/12)"""
    try:
        date_obj = datetime.datetime.strptime(date_str, "%d/%m")
        db_format = date_obj.strftime("%m-%d")        # ex: "12-25" pour Firebase
        readable_format = date_obj.strftime("%d/%m")  # ex: "25/12" pour l'affichage
    except ValueError:
        await ctx.send("❌ Format invalide ! Utilise le format `JJ/MM` (ex: `+anniv 14/07`).")
        return

    user_id = str(ctx.author.id)

    db.collection("anniversaires").document(user_id).set({
        "username": ctx.author.name,
        "date": db_format,
        "readable_date": readable_format,
        "user_mention": ctx.author.mention
    })

    embed = discord.Embed(
        title="🎂 Anniversaire Enregistré !",
        description=f"L'anniversaire de {ctx.author.mention} a bien été ajouté.",
        color=discord.Color.gold()
    )
    embed.add_field(name="Utilisateur", value=ctx.author.name, inline=True)
    embed.add_field(name="Date", value=readable_format, inline=True)
    embed.set_thumbnail(url=ctx.author.display_avatar.url)
    embed.set_footer(text="✨")

    await ctx.send(embed=embed)

    # Met à jour (édite) le message récapitulatif des anniversaires
    await update_birthday_list_message()


# ==========================================
# TÂCHE AUTOMATIQUE (VÉRIFICATION DES ANNIV)
# ==========================================

@tasks.loop(hours=1)  # Vérifie toutes les heures
async def check_birthdays():
    global dernier_jour_verifie, anniversaires_fetes_aujourdhui

    maintenant = datetime.datetime.now()
    aujourdhui_str = maintenant.strftime("%m-%d")
    jour_actuel = maintenant.day

    # Si on change de jour, on réinitialise la liste des fêtés
    if dernier_jour_verifie != jour_actuel:
        dernier_jour_verifie = jour_actuel
        anniversaires_fetes_aujourdhui = []

    general_channel = bot.get_channel(GENERAL_CHANNEL_ID)
    if general_channel is None:
        print(f"⚠️ Impossible de trouver le salon avec l'ID {GENERAL_CHANNEL_ID}")
        return

    users_ref = db.collection("anniversaires").where("date", "==", aujourdhui_str).stream()

    for user in users_ref:
        user_id = user.id
        data = user.to_dict()

        if user_id not in anniversaires_fetes_aujourdhui:
            anniversaires_fetes_aujourdhui.append(user_id)

            embed = discord.Embed(
                title="🎉 JOYEUX ANNIVERSAIRE ! 🥳",
                description=f"Aujourd'hui, c'est l'anniversaire de {data['user_mention']} ! 🎂✨\n",
                color=discord.Color.gold()
            )
            embed.set_footer(text="🎉")

            await general_channel.send(content="@everyone 📢", embed=embed)


@check_birthdays.before_loop
async def before_check_birthdays():
    # Attend que le bot soit complètement connecté avant la première itération
    await bot.wait_until_ready()


# ==========================================
# ÉVÉNEMENTS DU BOT
# ==========================================

@bot.event
async def on_ready():
    print(f"✅ Bot Discord connecté en tant que : {bot.user.name}")
    if not check_birthdays.is_running():
        check_birthdays.start()


# ==========================================
# KEEP ALIVE (FLASK FOR RENDER)
# ==========================================
app = Flask('')


@app.route('/')
def home():
    return "Bot is alive and healthy!", 200


def run():
    port = int(os.environ.get('PORT', 3000))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)


def keep_alive():
    t = Thread(target=run)
    t.daemon = True
    t.start()


# ==========================================
# STARTER
# ==========================================
if __name__ == "__main__":
    keep_alive()

    TOKEN = os.environ.get("DISCORD_TOKEN")
    if not TOKEN:
        raise RuntimeError("❌ La variable d'environnement DISCORD_TOKEN est manquante.")

    bot.run(TOKEN)
