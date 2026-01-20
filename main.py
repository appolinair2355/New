import os
import asyncio
import re
import logging
import sys
from datetime import datetime, timedelta, timezone, time
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from aiohttp import web
from config import (
    API_ID, API_HASH, BOT_TOKEN, ADMIN_ID,
    SOURCE_CHANNEL_ID, PREDICTION_CHANNEL_ID, CONTROL_CHANNEL_ID,
    MIRROR_PAIRS, PORT
)

# --- Configuration et Initialisation ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

client = TelegramClient(StringSession(os.getenv('TELEGRAM_SESSION', '')), API_ID, API_HASH)

# --- Variables Globales d'État ---
CYCLE_RULE_1 = ['❤️', '♦️', '♣️', '♠️', '♦️', '❤️', '♠️', '♣️']
CYCLE_SIZE = 8
pair_sequence_index = 0

control_counts = {'♠️': 0, '❤️': 0, '♦️': 0, '♣️': 0}
mirror_override_suit = None
mirror_diff_thresholds = {'Miroirp': 10, 'Miroirs': 10}
waiting_for_diff = {} # user_id: mirror_key

stats = {'✅0️⃣': 0, '✅1️⃣': 0, '✅2️⃣': 0, '❌': 0, 'total': 0}
report_interval = 0
pending_predictions = {}
processed_messages = set()
current_game_number = 0
prediction_channel_ok = True
WAT_TZ = timezone(timedelta(hours=1))

# --- Utilitaires ---

def extract_game_number(message: str):
    match = re.search(r"#N\s*(\d+)\.?", message, re.IGNORECASE)
    return int(match.group(1)) if match else None

def extract_parentheses_groups(message: str):
    return re.findall(r"\(([^)]*)\)", message)

def normalize_suits(group_str: str) -> str:
    return group_str.replace('❤️', '♥').replace('♦️', '♦').replace('♣️', '♣').replace('♠️', '♠')

def has_suit_in_group(group_str: str, target_suit: str) -> bool:
    normalized_group = normalize_suits(group_str)
    normalized_target = normalize_suits(target_suit)
    return normalized_target in normalized_group

def get_prediction(numero):
    global pair_sequence_index, mirror_override_suit
    if numero < 6 or numero > 1436 or numero % 2 != 0 or numero % 10 == 0:
        return None
    
    # Si le système miroir a un dépassement (différence >= 10)
    if mirror_override_suit:
        costume = mirror_override_suit
        mirror_override_suit = None  # Reset après usage unique
        logger.info(f"🔄 OVERRIDE MIROIR activé pour #{numero}: {costume}")
    else:
        costume = CYCLE_RULE_1[pair_sequence_index % CYCLE_SIZE]
        
    return costume

# --- Actions ---

async def send_prediction(target_game: int, predicted_suit: str):
    global pair_sequence_index
    try:
        # Message de prédiction en une ligne simple
        msg = f"🎰 #{target_game} 🔮 {predicted_suit} 🛡️ ⏳"
        
        sent_msg = await client.send_message(PREDICTION_CHANNEL_ID, msg)
        pair_sequence_index += 1
        
        pending_predictions[target_game] = {
            'message_id': sent_msg.id,
            'suit': predicted_suit,
            'check_count': 0
        }
        logger.info(f"✅ Prédiction envoyée pour #{target_game}: {predicted_suit}")
    except Exception as e:
        logger.error(f"Erreur envoi prédiction: {e}")

async def update_status(game_number: int, status: str):
    if game_number not in pending_predictions: return
    pred = pending_predictions[game_number]
    try:
        # Message de résultat en une ligne simple
        msg = f"🎰 #{game_number} 🎯 {pred['suit']} 🏁 {status}"
        await client.edit_message(PREDICTION_CHANNEL_ID, pred['message_id'], msg)
        
        if status in stats:
            stats[status] += 1
            stats['total'] += 1
        del pending_predictions[game_number]
    except Exception as e:
        logger.error(f"Erreur mise à jour statut: {e}")

async def check_results(game_number: int, group: str):
    # N
    if game_number in pending_predictions:
        if has_suit_in_group(group, pending_predictions[game_number]['suit']):
            await update_status(game_number, '✅0️⃣')
        else:
            pending_predictions[game_number]['check_count'] = 1
    
    # N-1
    prev = game_number - 1
    if prev in pending_predictions and pending_predictions[prev]['check_count'] == 1:
        if has_suit_in_group(group, pending_predictions[prev]['suit']):
            await update_status(prev, '✅1️⃣')
        else:
            pending_predictions[prev]['check_count'] = 2
            
    # N-2
    prev2 = game_number - 2
    if prev2 in pending_predictions and pending_predictions[prev2]['check_count'] == 2:
        if has_suit_in_group(group, pending_predictions[prev2]['suit']):
            await update_status(prev2, '✅2️⃣')
        else:
            await update_status(prev2, '❌')

# --- Bilan ---

async def send_stats_report():
    total = stats['total']
    if total == 0: return
    wins = stats['✅0️⃣'] + stats['✅1️⃣'] + stats['✅2️⃣']
    msg = f"""📊 **BILAN DES PRÉDICTIONS**

✅ Taux de réussite : {(wins/total)*100:.1f}%
❌ Taux de perte : {(stats['❌']/total)*100:.1f}%

Détails :
✅0️⃣ : {stats['✅0️⃣']}
✅1️⃣ : {stats['✅1️⃣']}
✅2️⃣ : {stats['✅2️⃣']}
❌ : {stats['❌']}

Total prédictions : {total}"""
    await client.send_message(PREDICTION_CHANNEL_ID, msg)

async def report_task_loop():
    while True:
        if report_interval > 0:
            await asyncio.sleep(report_interval * 60)
            await send_stats_report()
        else:
            await asyncio.sleep(60)

# --- Handlers ---

@client.on(events.NewMessage(chats=SOURCE_CHANNEL_ID))
@client.on(events.MessageEdited(chats=SOURCE_CHANNEL_ID))
async def handle_source(event):
    global current_game_number, pair_sequence_index
    text = event.message.message
    game_num = extract_game_number(text)
    if not game_num: return
    
    current_game_number = game_num
    
    # Synchronisation forcée de l'index du cycle basée sur le numéro de jeu
    calculated_index = 0
    for n in range(6, game_num + 1, 2):
        if n % 10 == 0:
            continue
        calculated_index += 1
    
    pair_sequence_index = calculated_index

    # Déclenchement prédiction (si impair -> prédit suivant pair)
    if game_num % 2 != 0:
        target = game_num + 1
        # Vérification si une prédiction est déjà en cours ou a été traitée pour ce numéro cible
        if target not in pending_predictions and target not in processed_messages:
            pred_suit = get_prediction(target)
            if pred_suit:
                # Marquer comme traité AVANT l'envoi pour éviter les doublons dus aux éditions de messages
                processed_messages.add(target)
                await send_prediction(target, pred_suit)

    # Vérification résultat (si finalisé)
    if '⏰' not in text and ('✅' in text or '🔰' in text):
        groups = extract_parentheses_groups(text)
        if groups:
            await check_results(game_num, groups[0])

@client.on(events.NewMessage(chats=CONTROL_CHANNEL_ID))
@client.on(events.MessageEdited(chats=CONTROL_CHANNEL_ID))
async def handle_control(event):
    global control_counts, mirror_override_suit
    text = event.message.message
    if "Compteur instantané" not in text: return
    
    # Extraction des scores (ex: ♠️ : 20)
    for suit in control_counts.keys():
        match = re.search(fr"{suit}\s*:\s*(\d+)", text)
        if match:
            control_counts[suit] = int(match.group(1))
            
    # Logique des miroirs
    checked_pairs = set()
    for s1, s2 in MIRROR_PAIRS.items():
        if s1 in checked_pairs: continue
        val1 = control_counts.get(s1, 0)
        val2 = control_counts.get(s2, 0)
        diff = abs(val1 - val2)
        
        # Déterminer quel seuil utiliser
        mirror_key = 'Miroirp' if (s1 == '♠️' or s1 == '♦️') else 'Miroirs'
        threshold = mirror_diff_thresholds.get(mirror_key, 10)
        
        if diff >= threshold:
            # On prend le plus faible du miroir
            mirror_override_suit = s1 if val1 < val2 else s2
            logger.info(f"⚠️ Alerte Miroir {mirror_key} ({s1}/{s2}): Différence {diff} >= {threshold}. Prochain costume: {mirror_override_suit}")
            break # On prend la première différence trouvée
        checked_pairs.add(s1)
        checked_pairs.add(s2)

@client.on(events.NewMessage(pattern=r'/dif'))
async def set_dif_start(event):
    if event.sender_id != ADMIN_ID: return
    waiting_for_diff[event.sender_id] = 'Miroirp'
    await event.reply("Entrez la différence pour **Miroirp** (♠️ ↔ ♦️) :")

@client.on(events.NewMessage())
async def handle_all_messages(event):
    global mirror_diff_thresholds
    if event.sender_id in waiting_for_diff:
        try:
            val = int(event.message.message)
            current_step = waiting_for_diff[event.sender_id]
            
            if current_step == 'Miroirp':
                mirror_diff_thresholds['Miroirp'] = val
                waiting_for_diff[event.sender_id] = 'Miroirs'
                await event.reply(f"✅ Miroirp réglé à {val}.\nMaintenant, entrez la différence pour **Miroirs** (❤️ ↔ ♣️) :")
            elif current_step == 'Miroirs':
                mirror_diff_thresholds['Miroirs'] = val
                del waiting_for_diff[event.sender_id]
                await event.reply(f"✅ Miroirs réglé à {val}.\nConfiguration terminée.")
        except ValueError:
            await event.reply("Veuillez entrer un nombre valide.")

@client.on(events.NewMessage(pattern=r'/inv (\d+)'))
async def set_inv(event):
    global report_interval
    if event.sender_id == ADMIN_ID:
        report_interval = int(event.pattern_match.group(1))
        await event.reply(f"✅ Intervalle : {report_interval} min")

# --- Startup ---

async def main():
    await client.start(bot_token=BOT_TOKEN)
    asyncio.create_task(report_task_loop())
    
    # Simple web server for health checks
    app = web.Application()
    app.router.add_get('/', lambda r: web.Response(text="Bot is running"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', PORT).start()
    
    logger.info("Bot opérationnel")
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
