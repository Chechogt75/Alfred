#!/usr/bin/env python3
"""
Alfred - Asistente Personal de Agenda Diaria
"""

import os
import time
import json
import datetime
import requests
import anthropic
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN", "")
ANTHROPIC_API_KEY    = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID     = os.environ.get("TELEGRAM_CHAT_ID", "")
CALENDAR_ID          = os.environ.get("CALENDAR_ID", "primary")

BOGOTA_OFFSET = datetime.timezone(datetime.timedelta(hours=-5))


def get_google_credentials():
    creds = Credentials(
        token=None,
        refresh_token=GOOGLE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/calendar.readonly",
                "https://www.googleapis.com/auth/gmail.readonly"])
    creds.refresh(Request())
    return creds


def get_calendar_events(creds):
    service = build("calendar", "v3", credentials=creds)

    # Usamos MAÃANA (hoy + 1 dia) porque Alfred envia el reporte la noche anterior
    hoy          = datetime.datetime.now(BOGOTA_OFFSET)
    # Compensar delays de GitHub Actions (hasta 6h): si el cron corre entre 0h-6h Bogota
    # es porque se atraso desde las 8 PM — retrocedemos al dia anterior para calcular manana
    if hoy.hour < 6:
        hoy -= datetime.timedelta(days=1)
    manana        = hoy + datetime.timedelta(days=1)
    if manana.weekday() == 5:    # sabado -> lunes
        manana += datetime.timedelta(days=2)
    elif manana.weekday() == 6:  # domingo -> lunes
        manana += datetime.timedelta(days=1)
    start_of_day = manana.replace(hour=0,  minute=0,  second=0,  microsecond=0)
    end_of_day   = manana.replace(hour=23, minute=59, second=59, microsecond=0)

    print(f"  Rango (manana): {start_of_day.isoformat()} -> {end_of_day.isoformat()}")

    # Obtener TODOS los calendarios, incluyendo los ocultos y con paginacion
    all_calendars = []
    page_token = None
    while True:
        cal_list = service.calendarList().list(
            showHidden=True,
            pageToken=page_token
        ).execute()
        all_calendars.extend(cal_list.get("items", []))
        page_token = cal_list.get("nextPageToken")
        if not page_token:
            break

    print(f"  Calendarios encontrados ({len(all_calendars)}): {[(c.get('summary','?'), c['id'][:40]) for c in all_calendars]}")

    all_events = []
    seen_ids   = set()
    for cal in all_calendars:
        cal_id   = cal["id"]
        cal_name = cal.get("summary", "")

        # Saltar festivos, contactos y directorio
        if any(skip in cal_id.lower() for skip in ["holiday", "contacts", "directory"]):
            print(f"  Saltando: {cal_name}")
            continue

        try:
            result = service.events().list(
                calendarId=cal_id,
                timeMin=start_of_day.isoformat(),
                timeMax=end_of_day.isoformat(),
                maxResults=50, singleEvents=True,
                orderBy="startTime"
            ).execute()
            items = result.get("items", [])
            print(f"  > {cal_name} ({cal_id[:35]}): {len(items)} eventos")
            for ev in items:
                ev_id = ev.get("id", "")
                if ev_id not in seen_ids:
                    seen_ids.add(ev_id)
                    ev["_cal_name"] = cal_name
                    all_events.append(ev)
        except Exception as e:
            print(f"  Error leyendo calendario '{cal_name}' ({cal_id[:35]}): {e}")

    all_events.sort(key=lambda ev: ev["start"].get("dateTime", ev["start"].get("date", "")))

    formatted = []
    for event in all_events:
        summary     = event.get("summary", "Sin titulo")
        start       = event["start"].get("dateTime", event["start"].get("date", ""))
        end         = event["end"].get("dateTime",   event["end"].get("date",   ""))
        location    = event.get("location", "")
        description = event.get("description", "")
        cal_name    = event.get("_cal_name", "")

        if "T" in start:
            dt         = datetime.datetime.fromisoformat(start)
            hora_inicio = dt.strftime("%I:%M %p")
            dt_end     = datetime.datetime.fromisoformat(end)
            hora_fin   = dt_end.strftime("%I:%M %p")
            hora_str   = f"{hora_inicio} - {hora_fin}"
        else:
            hora_str = "Todo el dia"

        entry = f"- {hora_str}: {summary}"
        if location:
            entry += f" (Lugar: {location})"
        if cal_name and cal_name.lower() not in ("primary", "checho", summary.lower()):
            entry += f" [{cal_name}]"
        if description:
            entry += f"\n  Nota: {description[:100].replace(chr(10), ' ')}"
        formatted.append(entry)

    if not formatted:
        return "No hay eventos programados para manana."
    return "\n".join(formatted)


def get_urgent_emails(creds):
    service = build("gmail", "v1", credentials=creds)
    now_utc   = datetime.datetime.now(datetime.timezone.utc)
    since_utc = now_utc - datetime.timedelta(hours=24)
    query = f"is:unread after:{int(since_utc.timestamp())} -from:noreply -from:no-reply -from:notifications"

    try:
        result   = service.users().messages().list(userId="me", q=query, maxResults=10).execute()
        messages = result.get("messages", [])
    except Exception:
        return "No se pudo acceder al correo."

    if not messages:
        return "No hay emails urgentes en las ultimas 24 horas."

    emails_text = []
    for msg in messages[:5]:
        try:
            detail  = service.users().messages().get(
                userId="me", id=msg["id"], format="metadata",
                metadataHeaders=["From","Subject","Date"]).execute()
            headers = {h["name"]: h["value"] for h in detail.get("payload", {}).get("headers", [])}
            subject = headers.get("Subject", "Sin asunto")[:80]
            sender  = headers.get("From",    "Desconocido")[:50]
            emails_text.append(f"- De: {sender}\n  Asunto: {subject}")
        except Exception:
            continue

    return "\n".join(emails_text) if emails_text else "No hay emails urgentes."


def generate_report(calendar_text, email_text):
    hoy    = datetime.datetime.now(BOGOTA_OFFSET)
    manana  = hoy + datetime.timedelta(days=1)
    if manana.weekday() == 5:    # sabado -> lunes
        manana += datetime.timedelta(days=2)
    elif manana.weekday() == 6:  # domingo -> lunes
        manana += datetime.timedelta(days=1)
    fecha  = manana.strftime("%A %d de %B de %Y")
    hora   = hoy.strftime("%I:%M %p")

    prompt = (
        'Eres Alfred, mayordomo ejecutivo de Sr. Checho, Director General de Amin.\n'
        'Genera un informe vespertino conciso y profesional en espanol '
        'en formato bonito para Telegram (usa emojis).\n'
        f'Fecha del reporte: hoy en la noche\nAgenda para: {fecha}\nHora de envio: {hora}\n'
        f'EVENTOS DE MANANA EN GOOGLE CALENDAR:\n{calendar_text}\n'
        f'EMAILS URGENTES (ultimas 24h):\n{email_text}\n'
        'Instrucciones: saludo indicando que este es el briefing de la noche con la agenda del dia siguiente, '
        'emojis de reloj y luna, seccion emails urgentes, '
        'resumen ejecutivo de la agenda de manana, formato Telegram (*negrita*, _cursiva_), '
        'tono de mayordomo britanico, maximo 2000 chars.'
    )

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    max_attempts = 4
    for attempt in range(max_attempts):
        try:
            message = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}])
            return message.content[0].text
        except anthropic.APIStatusError as e:
            if e.status_code == 529:
                if attempt < max_attempts - 1:
                    wait = 30 * (attempt + 1)
                    print(f"API sobrecargada (529). Esperando {wait}s antes de reintento {attempt+2}/{max_attempts}...")
                    time.sleep(wait)
                else:
                    return "Alfred no pudo generar el reporte (API sobrecargada). Intente de nuevo mas tarde."
            else:
                raise
    return "Error generando reporte."


def send_telegram(text):
    url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    resp = requests.post(url, data=data, timeout=30)
    resp.raise_for_status()


def main():
    print("Alfred - Briefing Nocturno (Agenda del Dia Siguiente)")
    print("=" * 50)
    print("[1/4] Autenticando con Google...")
    creds = get_google_credentials()
    print("[2/4] Consultando Google Calendar (manana)...")
    calendar_text = get_calendar_events(creds)
    print("[3/4] Buscando emails urgentes...")
    email_text = get_urgent_emails(creds)
    print("[4/4] Generando reporte con Claude...")
    report = generate_report(calendar_text, email_text)
    print(f"Reporte generado ({len(report)} chars)")
    print("Enviando por Telegram...")
    send_telegram(report)
    print("Mensaje enviado por Telegram!")
    print("Alfred ha completado el Briefing Nocturno!")


if __name__ == "__main__":
    main()
