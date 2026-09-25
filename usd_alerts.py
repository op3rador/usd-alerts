import requests
import time
from datetime import datetime, timezone

# ============ CONFIGURA AQUÍ ============
TOKEN = "8941710007:AAHb02MS7IF8GX3gkVuF9X83sMND6QXMk7Y"
CHAT_ID = "1669799682"
ALERT_MINUTES = 30
MOVE_THRESHOLD = 0.15
CHECK_AFTER_MIN = 5
CHECK_WINDOW = 25
# ========================================

URL_CAL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
URL_PRICE = "https://open.er-api.com/v6/latest/USD"

sent_pre = set()
sent_signal = set()
price_at_event = {}

def send(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        print("Error Telegram:", e)

def get_eurusd():
    try:
        r = requests.get(URL_PRICE, timeout=10)
        data = r.json()
        return data["rates"]["EUR"]
    except:
        return None

print("Bot USD High Impact + Señales iniciado...")
print("Esperando eventos...")

while True:
    try:
        r = requests.get(URL_CAL, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        events = r.json()
        now = datetime.now(timezone.utc)

        for e in events:
            if e.get("country") != "USD" or e.get("impact") != "High":
                continue

            title = e.get("title", "")
            date_str = e.get("date", "")
            key = title + "|" + date_str

            try:
                dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            except:
                continue

            mins = (dt - now).total_seconds() / 60

            # Alerta 30 min antes
            if 0 < mins <= ALERT_MINUTES and key not in sent_pre:
                send(
                    f"🔴 <b>USD HIGH IMPACT en {int(mins)} min</b>\n"
                    f"📌 {title}\n"
                    f"Forecast: {e.get('forecast') or '—'}\n"
                    f"Previous: {e.get('previous') or '—'}"
                )
                sent_pre.add(key)
                print(f"Alerta previa: {title}")

            # Monitoreo después de la noticia
            mins_after = -mins
            if CHECK_AFTER_MIN <= mins_after <= CHECK_WINDOW:
                if key not in price_at_event:
                    px = get_eurusd()
                    if px:
                        price_at_event[key] = px
                        print(f"Precio referencia: {title} → {px}")
                else:
                    if key not in sent_signal:
                        current = get_eurusd()
                        if current and price_at_event[key]:
                            change_pct = ((current - price_at_event[key]) / price_at_event[key]) * 100
                            if change_pct <= -MOVE_THRESHOLD:
                                send(
                                    f"🟢 <b>POSIBLE COMPRA USD</b>\n"
                                    f"Después de: {title}\n"
                                    f"EUR/USD bajó {abs(change_pct):.2f}% → Dólar fortalecido\n"
                                    f"Revisa el gráfico antes de entrar."
                                )
                                sent_signal.add(key)
                                print(f"Señal COMPRA: {title}")
                            elif change_pct >= MOVE_THRESHOLD:
                                send(
                                    f"🔴 <b>POSIBLE VENTA USD</b>\n"
                                    f"Después de: {title}\n"
                                    f"EUR/USD subió {change_pct:.2f}% → Dólar debilitado\n"
                                    f"Revisa el gráfico antes de entrar."
                                )
                                sent_signal.add(key)
                                print(f"Señal VENTA: {title}")

    except Exception as ex:
        print("Error:", ex)

    time.sleep(45)
