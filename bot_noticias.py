import os
import time
import json
import html
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ============ CONFIGURACIÓN ============
TOKEN = os.environ.get("TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

ALERT_MINUTES = 30
MOVE_THRESHOLD_EUR = 0.15
MOVE_THRESHOLD_XAU = 0.20
CHECK_AFTER_MIN = 2
CHECK_WINDOW = 25
MAX_SIGNALS_PER_EVENT = 2          # permite 2 evaluaciones por noticia
CALENDAR_CACHE_SECONDS = 180       # 3 min
RATE_LIMIT_WAIT = 600
STATE_FILE = Path("bot_state.json")  # persistencia

# Forex Factory (feed oficial de la comunidad)
URL_FF_THIS = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
URL_FF_NEXT = "https://nfs.faireconomy.media/ff_calendar_nextweek.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

INVERSE_KEYWORDS = [
    "unemployment rate", "jobless claims", "unemployment claims", "continuing claims"
]

CONTEXTOS = [
    (["jolts", "job openings"],
     "📌 <b>JOLTS</b> mide la demanda de empleo. Dato fuerte = mercado laboral tenso = Fed más hawkish = USD alcista."),
    (["non-farm", "nfp", "employment change", "payrolls", "adp"],
     "📌 <b>Empleo (NFP/ADP)</b>: de los datos que más mueven al USD. Dato fuerte = economía sólida y Fed sin prisa por bajar tasas."),
    (["unemployment rate", "jobless claims", "unemployment claims", "continuing claims"],
     "📌 <b>Desempleo</b>: aquí un dato MÁS ALTO es negativo para el USD."),
    (["cpi", "pce", "inflation"],
     "📌 <b>Inflación</b>: dato más alto = Fed más hawkish = USD alcista a corto plazo."),
    (["ppi"],
     "📌 <b>PPI</b>: precios al productor, adelanta presión sobre la inflación."),
    (["retail sales"],
     "📌 <b>Ventas minoristas</b>: termómetro del consumo. Dato fuerte = USD positivo."),
    (["ism", "pmi"],
     "📌 <b>ISM/PMI</b>: por encima de 50 indica expansión. Dato fuerte = USD positivo."),
    (["gdp"],
     "📌 <b>PIB</b>: crecimiento de la economía. Dato fuerte = USD positivo."),
    (["consumer confidence", "sentiment"],
     "📌 <b>Confianza del consumidor</b>: dato fuerte = consumo robusto = USD positivo."),
    (["hpi", "house price", "case-shiller"],
     "📌 <b>Precios de vivienda</b>: impacto normalmente moderado/secundario."),
]

# ============ ESTADO PERSISTENTE ============
events_cache = []
last_calendar_fetch = 0
calendar_fail_count = 0
sent_pre = set()
sent_signal = {}          # key -> contador de señales enviadas
refs = {}                 # key -> {"eur": float|None, "xau": (precio, fuente)|None, "tarde": bool, "captured_at": ts}

def load_state():
    global sent_pre, sent_signal, refs
    if not STATE_FILE.exists():
        return
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        sent_pre = set(data.get("sent_pre", []))
        sent_signal = data.get("sent_signal", {})
        refs = data.get("refs", {})
        print(f"Estado cargado → pre:{len(sent_pre)} signals:{len(sent_signal)} refs:{len(refs)}")
    except Exception as e:
        print("Error cargando estado:", e)

def save_state():
    try:
        # Limpiar refs muy antiguas (> 6 h)
        now_ts = time.time()
        clean_refs = {
            k: v for k, v in refs.items()
            if now_ts - v.get("captured_at", 0) < 6 * 3600
        }
        data = {
            "sent_pre": list(sent_pre),
            "sent_signal": sent_signal,
            "refs": clean_refs,
        }
        STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print("Error guardando estado:", e)

# ============ TELEGRAM ============
def send(msg: str):
    if not TOKEN or not CHAT_ID:
        print("Sin TOKEN/CHAT_ID, mensaje no enviado")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=12,
        )
        if r.status_code != 200:
            print("Telegram error:", r.status_code, r.text[:180])
    except Exception as e:
        print("Error Telegram:", e)

# ============ PRECIOS ============
def yahoo_price(symbol: str):
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
            params={"interval": "1m", "range": "1d"},
            headers=HEADERS,
            timeout=10,
        )
        if r.status_code != 200:
            return None
        price = r.json()["chart"]["result"][0]["meta"].get("regularMarketPrice")
        return float(price) if price else None
    except Exception as e:
        print(f"Error precio {symbol}:", e)
        return None

def get_eurusd():
    return yahoo_price("EURUSD=X")

def get_xauusd():
    for symbol in ("XAUUSD=X", "GC=F"):
        p = yahoo_price(symbol)
        if p:
            return (p, symbol)
    try:
        r = requests.get("https://biquote.io/api/XAUUSD", headers=HEADERS, timeout=10)
        if r.status_code == 200 and r.text.strip():
            d = r.json()
            bid, ask = float(d.get("bid", 0)), float(d.get("ask", 0))
            if bid > 0 and ask > 0:
                return ((bid + ask) / 2, "biquote")
    except Exception as e:
        print("Error XAUUSD biquote:", e)
    return None

# ============ ANÁLISIS DEL DATO ============
def parse_num(s):
    if s is None:
        return None
    s = str(s).strip().replace(",", "").replace("%", "")
    if not s or s == "—":
        return None
    mult = 1
    if s[-1] in "KMBT":
        mult = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[s[-1]]
        s = s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None

def analizar_dato(title, actual, forecast, previous):
    a, f = parse_num(actual), parse_num(forecast)
    if a is None or f is None:
        return None
    tl = title.lower()
    inverso = any(k in tl for k in INVERSE_KEYWORDS)
    sorpresa = ((a - f) / abs(f) * 100) if f != 0 else 0.0
    mag = abs(sorpresa)
    fuerza = "fuerte" if mag > 5 else "moderada" if mag > 2 else "leve" if mag > 0 else "nula"

    if a == f:
        sesgo, emoji = "neutral", "⚪"
        texto = f"El dato salió <b>en línea</b> con el forecast ({html.escape(str(actual))})."
    else:
        mejor = a > f
        usd_alcista = (mejor != inverso)
        sesgo = "alcista" if usd_alcista else "bajista"
        emoji = "🟢" if usd_alcista else "🔴"
        comp = "más alto" if mejor else "más bajo"
        texto = (
            f"El dato salió <b>{comp}</b> de lo esperado "
            f"(Actual {html.escape(str(actual))} vs Forecast {html.escape(str(forecast))}) "
            f"→ sesgo <b>{sesgo}</b> para el USD."
        )

    contexto = "📌 Dato económico de EE.UU. relevante para el dólar."
    for claves, txt in CONTEXTOS:
        if any(k in tl for k in claves):
            contexto = txt
            break

    bloque = (
        f"{emoji} <b>Análisis del dato</b>\n"
        f"{texto}\n"
        f"Sorpresa: <b>{fuerza}</b> ({sorpresa:+.1f}%) | Previous: {html.escape(str(previous))}\n"
        f"{contexto}"
    )
    return {"bloque": bloque, "sesgo": sesgo, "fuerza": fuerza}

def confianza(change_pct, threshold, fuerza_dato, alineado):
    m = abs(change_pct)
    if m >= threshold * 2.5:
        base = 72
    elif m >= threshold * 1.8:
        base = 65
    elif m >= threshold * 1.3:
        base = 58
    else:
        base = 52
    if alineado is True:
        base += {"fuerte": 8, "moderada": 5, "leve": 2}.get(fuerza_dato, 0)
    elif alineado is False:
        base -= 8
    return max(40, min(base, 85))

# ============ CALENDARIO (Forex Factory) ============
def refrescar_calendario():
    """Actualiza events_cache desde Forex Factory. Devuelve False si hay que saltar el ciclo."""
    global events_cache, last_calendar_fetch, calendar_fail_count
    now_ts = time.time()
    if events_cache and now_ts - last_calendar_fetch <= CALENDAR_CACHE_SECONDS:
        return True

    for url in (URL_FF_THIS, URL_FF_NEXT):
        try:
            r = requests.get(url, timeout=15, headers=HEADERS)
        except Exception as e:
            print(f"Error pidiendo calendario {url}:", e)
            continue

        if r.status_code == 429:
            print("Rate limit (429). Esperando 10 minutos...")
            time.sleep(RATE_LIMIT_WAIT)
            return False

        if r.status_code != 200 or not r.text.strip():
            print(f"Calendario status {r.status_code} → {url}")
            continue

        try:
            data = r.json()
            if not isinstance(data, list):
                continue
            # Combinar thisweek + nextweek sin duplicados
            existing_keys = {e.get("title", "") + "|" + e.get("date", "") for e in events_cache}
            nuevos = 0
            for e in data:
                key = e.get("title", "") + "|" + e.get("date", "")
                if key not in existing_keys:
                    events_cache.append(e)
                    existing_keys.add(key)
                    nuevos += 1
            last_calendar_fetch = now_ts
            calendar_fail_count = 0
            print(f"Calendario FF actualizado → total {len(events_cache)} eventos (+{nuevos})")
            return True
        except Exception as e:
            print("Error parseando calendario:", e, "|", r.text[:150])
            continue

    calendar_fail_count += 1
    if calendar_fail_count >= 3:
        send("⚠️ <b>Bot:</b> no pude actualizar el calendario de Forex Factory (3 fallos seguidos).")
        calendar_fail_count = 0
    time.sleep(60)
    return False

# ============ LÓGICA POR EVENTO ============
def alerta_previa(key, title, impact, forecast, previous, mins):
    if not (0 < mins <= ALERT_MINUTES) or key in sent_pre:
        return
    emoji = "🔴" if impact == "High" else "🟠"
    txt = "HIGH IMPACT" if impact == "High" else "MEDIUM IMPACT"
    send(
        f"{emoji} <b>USD {txt} en {int(mins)} min</b>\n"
        f"📌 {html.escape(title)}\n"
        f"Forecast: {html.escape(str(forecast))}\n"
        f"Previous: {html.escape(str(previous))}\n\n"
        f"⏳ Mediré la reacción en EUR/USD y XAUUSD.\n"
        f"<i>Fuente: Forex Factory</i>"
    )
    sent_pre.add(key)
    save_state()
    print(f"Alerta previa ({impact}): {title}")

def capturar_referencia(key, mins):
    """
    Empieza a muestrear ~3 min antes del release.
    Guarda el último precio válido justo antes (o el más cercano posible).
    """
    mins_after = -mins
    if mins > 3.5 or mins_after > CHECK_WINDOW:
        return
    if key in refs and not refs[key].get("tarde", True):
        # ya tenemos una referencia buena (tomada antes del release)
        return

    eur, xau = get_eurusd(), get_xauusd()
    if not (eur or xau):
        return

    tarde = mins_after > 0.8   # si ya pasó más de ~50 s del release
    refs[key] = {
        "eur": eur,
        "xau": xau,
        "tarde": tarde,
        "captured_at": time.time(),
    }
    save_state()
    print(f"Ref capturada → EUR: {eur} | XAU: {xau} | tarde={tarde}")

def evaluar_reaccion(key, title, impact, actual, forecast, previous, mins):
    mins_after = -mins
    if key not in refs:
        return
    if not (CHECK_AFTER_MIN <= mins_after <= CHECK_WINDOW):
        return

    count = sent_signal.get(key, 0)
    if count >= MAX_SIGNALS_PER_EVENT:
        return

    # Evitar señales demasiado seguidas (mínimo 6 min entre ellas)
    if count >= 1 and mins_after < 10:
        return

    ref = refs[key]
    analisis = analizar_dato(title, actual, forecast, previous)
    sesgo_dato = analisis["sesgo"] if analisis else "neutral"
    fuerza_dato = analisis["fuerza"] if analisis else "nula"

    if analisis:
        bloque_dato = analisis["bloque"] + "\n\n"
    else:
        bloque_dato = "📋 El dato todavía no aparece (solo veo la reacción del precio).\n\n"

    mensajes = []

    # ----- EUR/USD -----
    cur_eur = get_eurusd()
    if cur_eur and ref.get("eur"):
        ch = (cur_eur - ref["eur"]) / ref["eur"] * 100
        if abs(ch) >= MOVE_THRESHOLD_EUR:
            baja = ch < 0
            alineado = None if sesgo_dato == "neutral" else ((sesgo_dato == "alcista") == baja)
            conf = confianza(ch, MOVE_THRESHOLD_EUR, fuerza_dato, alineado)
            if baja:
                emoji, sesgo = "🟢", "COMPRA USD / VENTA EURUSD"
                texto = f"EUR/USD bajó <b>{abs(ch):.2f}%</b> → reacción positiva para el dólar."
                consejo = "Busca ventas en pullbacks a resistencias (si el técnico confirma)."
            else:
                emoji, sesgo = "🔴", "VENTA USD / COMPRA EURUSD"
                texto = f"EUR/USD subió <b>+{ch:.2f}%</b> → reacción negativa para el dólar."
                consejo = "Busca compras en pullbacks a soportes (si el técnico confirma)."
            aviso = "\n⚠️ El precio va CONTRA el sesgo del dato (posible reacción falsa)." if alineado is False else ""
            mensajes.append(
                f"{emoji} <b>EUR/USD</b>\n{texto}\nSesgo: <b>{sesgo}</b>\n"
                f"Confianza (heurística): <b>{conf}%</b>{aviso}\n💡 {consejo}"
            )

    # ----- XAUUSD -----
    cur_xau = get_xauusd()
    ref_xau = ref.get("xau")
    if cur_xau and ref_xau and cur_xau[1] == ref_xau[1]:
        ch = (cur_xau[0] - ref_xau[0]) / ref_xau[0] * 100
        if abs(ch) >= MOVE_THRESHOLD_XAU:
            sube = ch > 0
            alineado = None if sesgo_dato == "neutral" else ((sesgo_dato == "bajista") == sube)
            conf = confianza(ch, MOVE_THRESHOLD_XAU, fuerza_dato, alineado)
            if sube:
                emoji, sesgo = "🟡", "COMPRA XAUUSD (Oro)"
                texto = f"XAUUSD subió <b>+{ch:.2f}%</b> → oro alcista."
                consejo = "Busca compras en retrocesos a soportes clave."
            else:
                emoji, sesgo = "🟠", "VENTA XAUUSD (Oro)"
                texto = f"XAUUSD bajó <b>{abs(ch):.2f}%</b> → oro bajista."
                consejo = "Busca ventas en rebotes a resistencias."
            aviso = "\n⚠️ El precio va CONTRA el sesgo del dato (posible reacción falsa)." if alineado is False else ""
            mensajes.append(
                f"{emoji} <b>XAUUSD (Oro)</b>\n{texto}\nSesgo: <b>{sesgo}</b>\n"
                f"Confianza (heurística): <b>{conf}%</b>{aviso}\n💡 {consejo}"
            )

    if not mensajes:
        return

    nota_tarde = (
        "\n⚠️ El bot arrancó tarde: la referencia se tomó después del release, "
        "el movimiento inicial pudo perderse."
        if ref.get("tarde") else ""
    )

    msg = (
        f"📊 <b>ANÁLISIS POST-NOTICIA</b> (#{count + 1})\n\n"
        f"📌 Evento: <b>{html.escape(title)}</b> ({impact})\n"
        f"⏱ {int(mins_after)} min después del release{nota_tarde}\n"
        f"<i>Fuente calendario: Forex Factory</i>\n\n"
        f"{bloque_dato}"
        + "\n\n".join(mensajes)
        + "\n\n⚠️ <b>Importante:</b>\n"
          "• Es solo el sesgo de la reacción inicial, no una señal de entrada.\n"
          "• Combínalo con tu análisis técnico (estructura, zonas, momentum).\n"
          "• Cuidado con 'Buy the rumor, sell the news'.\n"
          "• Espera confirmación en M5/M15 antes de entrar."
    )
    send(msg)
    sent_signal[key] = count + 1
    save_state()
    print(f"Señal #{count + 1} enviada: {title}")

# ============ POLLING ADAPTATIVO ============
def get_sleep_seconds(now: datetime) -> int:
    """Más rápido cerca de eventos, más lento cuando no hay nada."""
    nearest = 9999
    for e in events_cache:
        if e.get("country") != "USD" or e.get("impact") not in ("High", "Medium"):
            continue
        try:
            dt = datetime.fromisoformat(e.get("date", "").replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            mins = abs((dt - now).total_seconds() / 60)
            if mins < nearest:
                nearest = mins
        except Exception:
            continue

    if nearest <= 5:
        return 8
    if nearest <= 15:
        return 12
    if nearest <= 40:
        return 20
    return 55

# ============ MAIN ============
def main():
    if not TOKEN or not CHAT_ID:
        print("Faltan las variables de entorno TOKEN y/o CHAT_ID.")
        return

    load_state()
    print("Bot USD High/Medium + EURUSD + XAUUSD (Forex Factory) iniciado...")
    send(
        "✅ <b>Bot iniciado correctamente</b>\n"
        "Monitoreando High + Medium Impact USD\n"
        "Fuente: <b>Forex Factory</b>\n"
        "EUR/USD + XAUUSD activos."
    )

    while True:
        try:
            if not refrescar_calendario():
                time.sleep(30)
                continue

            now = datetime.now(timezone.utc)

            for e in events_cache:
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
                except Exception:
                    continue
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)

                mins = (dt - now).total_seconds() / 60

                alerta_previa(key, title, impact, forecast, previous, mins)
                capturar_referencia(key, mins)
                evaluar_reaccion(key, title, impact, actual, forecast, previous, mins)

            sleep_s = get_sleep_seconds(now)
            time.sleep(sleep_s)

        except Exception as ex:
            print("Error general:", ex)
            time.sleep(30)

if __name__ == "__main__":
    main()
