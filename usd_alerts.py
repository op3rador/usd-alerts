import requests
import time
from datetime import datetime, timezone

# ============ CONFIGURA AQUÍ ============
TOKEN = "8941710007:AAHb02MS7IF8GX3gkVuF9X83sMND6QXMk7Y"
CHAT_ID = "1669799682"
ALERT_MINUTES = 30
MOVE_THRESHOLD_EUR = 0.15      # % mínimo en EURUSD para señal
MOVE_THRESHOLD_XAU = 0.20      # % mínimo en XAUUSD para señal (oro se mueve más)
CHECK_AFTER_MIN = 5
CHECK_WINDOW = 25
# ========================================

URL_CAL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
URL_EUR = "https://open.er-api.com/v6/latest/USD"
URL_XAU = "https://biquote.io/api/XAUUSD"

sent_pre = set()
sent_signal = set()
price_at_event = {}   # key → {"eur": float, "xau": float}

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
        r = requests.get(URL_EUR, timeout=10)
        return r.json()["rates"]["EUR"]
    except:
        return None

def get_xauusd():
    try:
        r = requests.get(URL_XAU, timeout=10)
        data = r.json()
        bid = float(data.get("bid", 0))
        ask = float(data.get("ask", 0))
        if bid > 0 and ask > 0:
            return (bid + ask) / 2
        return None
    except:
        return None

def calcular_probabilidad(change_pct, threshold):
    fuerza = abs(change_pct)
    if fuerza >= threshold * 2.5:
        return 78
    elif fuerza >= threshold * 1.8:
        return 68
    elif fuerza >= threshold * 1.3:
        return 58
    else:
        return 48

def analizar_dato(actual, forecast, previous):
    """Intenta interpretar Actual vs Forecast (si existen)"""
    if not actual or not forecast:
        return None
    try:
        # Limpia símbolos comunes (%, K, M, etc.)
        a = float(str(actual).replace("%", "").replace("K", "").replace("M", "").replace(",", "").strip())
        f = float(str(forecast).replace("%", "").replace("K", "").replace("M", "").replace(",", "").strip())
        if a > f:
            return "El dato salió **mejor** de lo esperado (Actual > Forecast) → sesgo alcista para el USD."
        elif a < f:
            return "El dato salió **peor** de lo esperado (Actual < Forecast) → sesgo bajista para el USD."
        else:
            return "El dato salió **en línea** con el Forecast."
    except:
        return None

print("Bot USD High/Medium + EURUSD + XAUUSD iniciado...")
print("Esperando eventos...")

while True:
    try:
        r = requests.get(URL_CAL, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        events = r.json()
        now = datetime.now(timezone.utc)

        for e in events:
            if e.get("country") != "USD" or e.get("impact") not in ("High", "Medium"):
                continue

            title = e.get("title", "")
            impact = e.get("impact", "")
            date_str = e.get("date", "")
            forecast = e.get("forecast") or "—"
            previous = e.get("previous") or "—"
            actual = e.get("actual") or None
            key = title + "|" + date_str

            try:
                dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            except:
                continue

            mins = (dt - now).total_seconds() / 60

            # ---------- ALERTA PREVIA ----------
            if 0 < mins <= ALERT_MINUTES and key not in sent_pre:
                impact_emoji = "🔴" if impact == "High" else "🟠"
                impact_text = "HIGH IMPACT" if impact == "High" else "MEDIUM IMPACT"

                send(
                    f"{impact_emoji} <b>USD {impact_text} en {int(mins)} min</b>\n"
                    f"📌 {title}\n"
                    f"Forecast: {forecast}\n"
                    f"Previous: {previous}\n\n"
                    f"⏳ Monitorearé reacción en EUR/USD y XAUUSD..."
                )
                sent_pre.add(key)
                print(f"Alerta previa ({impact}): {title}")

            # ---------- MONITOREO POST-NOTICIA ----------
            mins_after = -mins
            if CHECK_AFTER_MIN <= mins_after <= CHECK_WINDOW:

                # Guardar precios de referencia la primera vez
                if key not in price_at_event:
                    eur = get_eurusd()
                    xau = get_xauusd()
                    if eur or xau:
                        price_at_event[key] = {"eur": eur, "xau": xau}
                        print(f"Precios ref → EUR: {eur} | XAU: {xau}")
                    continue

                if key in sent_signal:
                    continue

                ref = price_at_event[key]
                current_eur = get_eurusd()
                current_xau = get_xauusd()

                # --- Análisis del dato (si hay Actual) ---
                analisis_dato = analizar_dato(actual, forecast, previous)
                bloque_dato = ""
                if analisis_dato:
                    bloque_dato = (
                        f"📋 <b>Dato publicado:</b>\n"
                        f"Actual: {actual} | Forecast: {forecast} | Previous: {previous}\n"
                        f"{analisis_dato}\n\n"
                    )
                elif actual:
                    bloque_dato = (
                        f"📋 <b>Dato publicado:</b>\n"
                        f"Actual: {actual} | Forecast: {forecast} | Previous: {previous}\n\n"
                    )

                mensajes = []

                # ===== EUR/USD =====
                if current_eur and ref.get("eur"):
                    change_eur = ((current_eur - ref["eur"]) / ref["eur"]) * 100
                    if abs(change_eur) >= MOVE_THRESHOLD_EUR:
                        prob = calcular_probabilidad(change_eur, MOVE_THRESHOLD_EUR)

                        if change_eur <= -MOVE_THRESHOLD_EUR:
                            sesgo = "COMPRA USD / VENTA EURUSD"
                            emoji = "🟢"
                            texto = (
                                f"EUR/USD bajó <b>{abs(change_eur):.2f}%</b> → "
                                f"el mercado ve la noticia como <b>positiva para el dólar</b>."
                            )
                        else:
                            sesgo = "VENTA USD / COMPRA EURUSD"
                            emoji = "🔴"
                            texto = (
                                f"EUR/USD subió <b>+{change_eur:.2f}%</b> → "
                                f"el mercado ve la noticia como <b>negativa para el dólar</b>."
                            )

                        mensajes.append(
                            f"{emoji} <b>EUR/USD</b>\n"
                            f"{texto}\n"
                            f"Sesgo: <b>{sesgo}</b>\n"
                            f"Probabilidad aprox: <b>{prob}%</b>"
                        )

                # ===== XAUUSD (Oro) =====
                if current_xau and ref.get("xau"):
                    change_xau = ((current_xau - ref["xau"]) / ref["xau"]) * 100
                    if abs(change_xau) >= MOVE_THRESHOLD_XAU:
                        prob = calcular_probabilidad(change_xau, MOVE_THRESHOLD_XAU)

                        if change_xau >= MOVE_THRESHOLD_XAU:
                            sesgo = "COMPRA XAUUSD (Oro)"
                            emoji = "🟡"
                            texto = (
                                f"XAUUSD subió <b>+{change_xau:.2f}%</b> → "
                                f"el oro reaccionó al alza (posible debilidad del USD o flight-to-safety)."
                            )
                        else:
                            sesgo = "VENTA XAUUSD (Oro)"
                            emoji = "🟠"
                            texto = (
                                f"XAUUSD bajó <b>{abs(change_xau):.2f}%</b> → "
                                f"el oro se debilita (suele ocurrir cuando el USD se fortalece)."
                            )

                        mensajes.append(
                            f"{emoji} <b>XAUUSD (Oro)</b>\n"
                            f"{texto}\n"
                            f"Sesgo: <b>{sesgo}</b>\n"
                            f"Probabilidad aprox: <b>{prob}%</b>"
                        )

                # Enviar solo si hubo al menos una señal
                if mensajes:
                    cuerpo = "\n\n".join(mensajes)
                    msg_final = (
                        f"📊 <b>ANÁLISIS POST-NOTICIA</b>\n\n"
                        f"📌 Evento: <b>{title}</b> ({impact})\n"
                        f"⏱ {int(mins_after)} min después del release\n\n"
                        f"{bloque_dato}"
                        f"{cuerpo}\n\n"
                        f"⚠️ Solo es lectura de la reacción del precio. "
                        f"Confirma siempre con tu análisis técnico."
                    )
                    send(msg_final)
                    sent_signal.add(key)
                    print(f"Señal enviada: {title}")

    except Exception as ex:
        print("Error:", ex)

    time.sleep(45)
