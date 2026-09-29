import requests
import time
from datetime import datetime, timezone

# ============ CONFIGURA AQUÍ ============
TOKEN = "8941710007:AAHb02MS7IF8GX3gkVuF9X83sMND6QXMk7Y"          # ← pon tu token real
CHAT_ID = "1669799682"                 # ← pon tu chat id real
ALERT_MINUTES = 30
MOVE_THRESHOLD_EUR = 0.15
MOVE_THRESHOLD_XAU = 0.20
CHECK_AFTER_MIN = 5
CHECK_WINDOW = 25
# ========================================

URL_CAL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
URL_EUR = "https://open.er-api.com/v6/latest/USD"
URL_XAU = "https://biquote.io/api/XAUUSD"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# ============ CONTROL DE RATE LIMIT ============
last_calendar_fetch = 0
CALENDAR_CACHE_SECONDS = 300      # Solo pide el calendario cada 5 minutos
RATE_LIMIT_WAIT = 600             # Cuando hay 429 espera 10 minutos
events_cache = []

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
        r = requests.get(URL_EUR, timeout=10, headers=HEADERS)
        if r.status_code != 200 or not r.text.strip():
            return None
        return r.json()["rates"]["EUR"]
    except Exception as e:
        print("Error EURUSD:", e)
        return None

def get_xauusd():
    try:
        r = requests.get(URL_XAU, timeout=10, headers=HEADERS)
        if r.status_code != 200 or not r.text.strip():
            return None
        data = r.json()
        bid = float(data.get("bid", 0))
        ask = float(data.get("ask", 0))
        if bid > 0 and ask > 0:
            return (bid + ask) / 2
        return None
    except Exception as e:
        print("Error XAUUSD:", e)
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
    if not actual or not forecast:
        return None
    try:
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
send("✅ <b>Bot iniciado correctamente</b>\nMonitoreando High + Medium Impact USD\nEUR/USD + XAUUSD activos.")
print("Esperando eventos...")

while True:
    try:
        now_ts = time.time()

        # Solo pedimos el calendario cada 5 minutos (o si no hay caché)
        if now_ts - last_calendar_fetch > CALENDAR_CACHE_SECONDS or not events_cache:
            r = requests.get(URL_CAL, timeout=15, headers=HEADERS)

            if r.status_code == 429:
                print("Rate limit (429). Esperando 10 minutos...")
                time.sleep(RATE_LIMIT_WAIT)
                continue

            if r.status_code != 200:
                print(f"Calendario status {r.status_code}")
                time.sleep(90)
                continue

            if not r.text.strip():
                print("Calendario devolvió respuesta vacía")
                time.sleep(90)
                continue

            try:
                events_cache = r.json()
                last_calendar_fetch = now_ts
                print(f"Calendario actualizado → {len(events_cache)} eventos")
            except Exception as e:
                print(f"Error parseando JSON del calendario: {e}")
                print("Respuesta recibida (primeros 200 chars):", r.text[:200])
                time.sleep(90)
                continue

        events = events_cache   # Siempre usamos la caché

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

                # EUR/USD
                if current_eur and ref.get("eur"):
                    change_eur = ((current_eur - ref["eur"]) / ref["eur"]) * 100
                    if abs(change_eur) >= MOVE_THRESHOLD_EUR:
                        prob = calcular_probabilidad(change_eur, MOVE_THRESHOLD_EUR)
                        if change_eur <= -MOVE_THRESHOLD_EUR:
                            sesgo = "COMPRA USD / VENTA EURUSD"
                            emoji = "🟢"
                            texto = f"EUR/USD bajó <b>{abs(change_eur):.2f}%</b> → positivo para el dólar."
                        else:
                            sesgo = "VENTA USD / COMPRA EURUSD"
                            emoji = "🔴"
                            texto = f"EUR/USD subió <b>+{change_eur:.2f}%</b> → negativo para el dólar."
                        mensajes.append(
                            f"{emoji} <b>EUR/USD</b>\n{texto}\n"
                            f"Sesgo: <b>{sesgo}</b>\nProbabilidad aprox: <b>{prob}%</b>"
                        )

                # XAUUSD
                if current_xau and ref.get("xau"):
                    change_xau = ((current_xau - ref["xau"]) / ref["xau"]) * 100
                    if abs(change_xau) >= MOVE_THRESHOLD_XAU:
                        prob = calcular_probabilidad(change_xau, MOVE_THRESHOLD_XAU)
                        if change_xau >= MOVE_THRESHOLD_XAU:
                            sesgo = "COMPRA XAUUSD (Oro)"
                            emoji = "🟡"
                            texto = f"XAUUSD subió <b>+{change_xau:.2f}%</b> → oro alcista."
                        else:
                            sesgo = "VENTA XAUUSD (Oro)"
                            emoji = "🟠"
                            texto = f"XAUUSD bajó <b>{abs(change_xau):.2f}%</b> → oro bajista."
                        mensajes.append(
                            f"{emoji} <b>XAUUSD (Oro)</b>\n{texto}\n"
                            f"Sesgo: <b>{sesgo}</b>\nProbabilidad aprox: <b>{prob}%</b>"
                        )

                if mensajes:
                    cuerpo = "\n\n".join(mensajes)
                    msg_final = (
                        f"📊 <b>ANÁLISIS POST-NOTICIA</b>\n\n"
                        f"📌 Evento: <b>{title}</b> ({impact})\n"
                        f"⏱ {int(mins_after)} min después del release\n\n"
                        f"{bloque_dato}"
                        f"{cuerpo}\n\n"
                        f"⚠️ Solo es lectura de la reacción del precio. Confirma con análisis técnico."
                    )
                    send(msg_final)
                    sent_signal.add(key)
                    print(f"Señal enviada: {title}")

    except Exception as ex:
        print("Error general:", ex)

    time.sleep(45)

def analizar_dato_profundo(title, actual, forecast, previous):
    """
    Análisis fundamental más profundo del dato.
    Devuelve un texto completo listo para Telegram.
    """
    if not actual or not forecast:
        return None

    try:
        a = float(str(actual).replace("%", "").replace("K", "").replace("M", "").replace(",", "").replace("B", "").strip())
        f = float(str(forecast).replace("%", "").replace("K", "").replace("M", "").replace(",", "").replace("B", "").strip())
        p = float(str(previous).replace("%", "").replace("K", "").replace("M", "").replace(",", "").replace("B", "").strip()) if previous and previous != "—" else None
    except:
        return None

    diferencia = a - f
    porcentaje_sorpresa = (diferencia / abs(f)) * 100 if f != 0 else 0

    # Determinamos dirección del sesgo
    if a > f:
        sesgo_dato = "alcista"
        fuerza = "fuerte" if abs(porcentaje_sorpresa) > 5 else "moderada" if abs(porcentaje_sorpresa) > 2 else "leve"
        emoji_dato = "🟢"
        texto_sorpresa = f"El dato salió **mejor** de lo esperado (Actual {actual} vs Forecast {forecast})"
    elif a < f:
        sesgo_dato = "bajista"
        fuerza = "fuerte" if abs(porcentaje_sorpresa) > 5 else "moderada" if abs(porcentaje_sorpresa) > 2 else "leve"
        emoji_dato = "🔴"
        texto_sorpresa = f"El dato salió **peor** de lo esperado (Actual {actual} vs Forecast {forecast})"
    else:
        sesgo_dato = "neutral"
        fuerza = "nula"
        emoji_dato = "⚪"
        texto_sorpresa = f"El dato salió **en línea** con el Forecast ({actual})"

    # Análisis específico por tipo de noticia
    titulo_lower = title.lower()

    if "jolts" in titulo_lower or "job openings" in titulo_lower:
        contexto = (
            "📌 <b>JOLTS Job Openings</b> mide la demanda de empleo.\n"
            "• Dato fuerte → Mercado laboral tenso → Más presión inflacionaria → Fed más hawkish → USD alcista\n"
            "• Dato débil → Enfriamiento del mercado laboral → Posible Fed más dovish → USD bajista"
        )
        if sesgo_dato == "alcista":
            impacto = "Soporta un escenario de tasas altas por más tiempo."
        elif sesgo_dato == "bajista":
            impacto = "Aumenta las probabilidades de que la Fed sea más cautelosa / dovish."
        else:
            impacto = "No cambia significativamente las expectativas de la Fed."

    elif "consumer confidence" in titulo_lower or "confianza del consumidor" in titulo_lower:
        contexto = (
            "📌 <b>Consumer Confidence</b> mide el optimismo del consumidor estadounidense.\n"
            "• Dato fuerte → Consumo robusto → Economía fuerte → USD positivo\n"
            "• Dato débil → Posible enfriamiento del consumo → Presión bajista para el USD"
        )
        if sesgo_dato == "alcista":
            impacto = "Refuerza la idea de resiliencia económica de EE.UU."
        elif sesgo_dato == "bajista":
            impacto = "Genera dudas sobre la fortaleza del consumidor y el crecimiento."
        else:
            impacto = "Impacto limitado en expectativas de política monetaria."

    elif "hpi" in titulo_lower or "house price" in titulo_lower or "case-shiller" in titulo_lower:
        contexto = (
            "📌 Precios de vivienda. Impacto generalmente moderado.\n"
            "Datos fuertes suelen ser ligeramente positivos para el USD (riqueza del consumidor)."
        )
        impacto = "Impacto secundario. Suele ser menos prioritario que empleo o inflación."

    else:
        # Genérico para otros datos USD
        contexto = "📌 Dato económico de EE.UU. relevante para el dólar."
        if sesgo_dato == "alcista":
            impacto = "Soporta un sesgo alcista para el USD."
        elif sesgo_dato == "bajista":
            impacto = "Soporta un sesgo bajista para el USD."
        else:
            impacto = "Impacto neutral."

    # Texto final del análisis fundamental
    analisis = (
        f"{emoji_dato} <b>Análisis del Dato:</b>\n"
        f"{texto_sorpresa}\n"
        f"Sorpresa: <b>{fuerza}</b> ({porcentaje_sorpresa:+.1f}%)\n\n"
        f"{contexto}\n\n"
        f"<b>Implicación:</b> {impacto}"
    )

    return analisis, sesgo_dato, fuerza

def calcular_probabilidad_mejorada(change_pct, threshold, fuerza_dato):
    """Probabilidad más realista combinando movimiento de precio + fuerza del dato"""
    fuerza_precio = abs(change_pct)

    base = 48
    if fuerza_precio >= threshold * 2.5:
        base = 75
    elif fuerza_precio >= threshold * 1.8:
        base = 68
    elif fuerza_precio >= threshold * 1.3:
        base = 60
    elif fuerza_precio >= threshold:
        base = 55

    # Bonus / penalización por fuerza del dato
    if fuerza_dato == "fuerte":
        base += 8
    elif fuerza_dato == "moderada":
        base += 4
    elif fuerza_dato == "leve":
        base += 1

    return min(base, 85)  # Máximo 85% para no generar falsa confianza

# ========== DENTRO DEL LOOP (reemplaza la parte de mensajes) ==========

# ... (después de obtener current_eur y current_xau)

analisis_result = analizar_dato_profundo(title, actual, forecast, previous)

bloque_dato = ""
sesgo_dato = "neutral"
fuerza_dato = "nula"

if analisis_result:
    bloque_dato, sesgo_dato, fuerza_dato = analisis_result
    bloque_dato += "\n\n"

mensajes = []

# ----- EUR/USD -----
if current_eur and ref.get("eur"):
    change_eur = ((current_eur - ref["eur"]) / ref["eur"]) * 100
    if abs(change_eur) >= MOVE_THRESHOLD_EUR:
        prob = calcular_probabilidad_mejorada(change_eur, MOVE_THRESHOLD_EUR, fuerza_dato)

        if change_eur <= -MOVE_THRESHOLD_EUR:
            sesgo = "COMPRA USD / VENTA EURUSD"
            emoji = "🟢"
            texto = f"EUR/USD bajó <b>{abs(change_eur):.2f}%</b> → reacción positiva para el dólar."
            consejo = "Busca ventas en pullbacks a zonas de resistencia (si el técnico confirma)."
        else:
            sesgo = "VENTA USD / COMPRA EURUSD"
            emoji = "🔴"
            texto = f"EUR/USD subió <b>+{change_eur:.2f}%</b> → reacción negativa para el dólar."
            consejo = "Busca compras en pullbacks a zonas de soporte (si el técnico confirma)."

        mensajes.append(
            f"{emoji} <b>EUR/USD</b>\n"
            f"{texto}\n"
            f"Sesgo: <b>{sesgo}</b>\n"
            f"Probabilidad aprox: <b>{prob}%</b>\n"
            f"💡 {consejo}"
        )

# ----- XAUUSD -----
if current_xau and ref.get("xau"):
    change_xau = ((current_xau - ref["xau"]) / ref["xau"]) * 100
    if abs(change_xau) >= MOVE_THRESHOLD_XAU:
        prob = calcular_probabilidad_mejorada(change_xau, MOVE_THRESHOLD_XAU, fuerza_dato)

        if change_xau >= MOVE_THRESHOLD_XAU:
            sesgo = "COMPRA XAUUSD (Oro)"
            emoji = "🟡"
            texto = f"XAUUSD subió <b>+{change_xau:.2f}%</b> → oro alcista."
            consejo = "Busca compras en retrocesos a soportes clave."
        else:
            sesgo = "VENTA XAUUSD (Oro)"
            emoji = "🟠"
            texto = f"XAUUSD bajó <b>{abs(change_xau):.2f}%</b> → oro bajista."
            consejo = "Busca ventas en rebotes a resistencias."

        mensajes.append(
            f"{emoji} <b>XAUUSD (Oro)</b>\n"
            f"{texto}\n"
            f"Sesgo: <b>{sesgo}</b>\n"
            f"Probabilidad aprox: <b>{prob}%</b>\n"
            f"💡 {consejo}"
        )

if mensajes:
    cuerpo = "\n\n".join(mensajes)

    # Mensaje final más completo
    msg_final = (
        f"📊 <b>ANÁLISIS POST-NOTICIA PROFUNDO</b>\n\n"
        f"📌 Evento: <b>{title}</b> ({impact})\n"
        f"⏱ {int(mins_after)} min después del release\n\n"
        f"{bloque_dato}"
        f"{cuerpo}\n\n"
        f"⚠️ <b>Importante:</b>\n"
        f"• Este es solo el sesgo fundamental de la reacción inicial.\n"
        f"• Combínalo siempre con tu análisis técnico (estructura, zonas, momentum).\n"
        f"• Cuidado con 'Buy the rumor, Sell the news'.\n"
        f"• Espera confirmación en M5/M15 antes de entrar."
    )

    send(msg_final)
    sent_signal.add(key)
    print(f"Señal enviada: {title}")
