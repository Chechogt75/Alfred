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
        token=None, refresh_token=GOOGLE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID, client_secret=GOOGLE_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/calendar.readonly",
                "https://www.googleapis.com/auth/gmail.readonly"])
    creds.refresh(Request())
    return creds
 
 
def get_calendar_events(creds):
    service = build("calendar", "v3", credentials=creds)
    now = datetime.datetime.now(BOGOTA_OFFSET)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day   = now.replace(hour=23, minute=59, second=59, microsecond=0)
    events_result = service.events().list(
        calendarId=CALENDAR_ID, timeMin=start_of_day.isoformat(),
        timeMax=end_of_day.isoformat(), maxResults=20,
        singleEvents=True, orderBy="startTime").execute()
    events = events_result.get("items", [])
    formatted = []
    for event in events:
        start = event["start"].get("dateTime", event["start"].get("date"))
        end   = event["end"].get("dateTime", event["end"].get("date"))
        summary     = event.get("summary", "Sin titulo")
        location    = event.get("location", "")
        description = event.get("description", "")
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
        if description:
            desc_short = description[:100].replace("\n", " ")
            entry += f"\n  Nota: {desc_short}"
        formatted.append(entry)
    if not formatted:
        return "No hay eventos programados para hoy."
    return "\n".join(formatted)
 
 
def get_urgent_emails(creds):
    service = build("gmail", "v1", credentials=creds)
    query = ("subject:(urgente OR vencimiento OR pago OR extracto OR factura "
             "OR forward OR divisa OR alerta OR recordatorio OR importante) "
             "newer_than:1d")
    results = service.users().messages().list(userId="me", q=query, maxResults=5).execute()
    messages = results.get("messages", [])
    formatted = []
    for msg_ref in messages:
        msg = service.users().messages().get(
            userId="me", id=msg_ref["id"], format="metadata",
            metadataHeaders=["Subject", "From", "Date"]).execute()
        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        subject = headers.get("Subject", "Sin asunto")
        sender  = headers.get("From", "Desconocido")
        if "<" in sender:
            sender = sender.split("<")[0].strip().strip('"')
        snippet = msg.get("snippet", "")[:120]
        formatted.append(f"- De: {sender}\n  Asunto: {subject}\n  Vista previa: {snippet}")
    if not formatted:
        return "No hay emails urgentes en las ultimas 24 horas."
    return "\n".join(formatted)
 
 
def format_with_claude(calendar_text, email_text):
    now   = datetime.datetime.now(BOGOTA_OFFSET)
    fecha = now.strftime("%A %d de %B de %Y")
    hora  = now.strftime("%I:%M %p")
    dias  = {"Monday": "Lunes", "Tuesday": "Martes", "Wednesday": "Miercoles",
             "Thursday": "Jueves", "Friday": "Viernes", "Saturday": "Sabado", "Sunday": "Domingo"}
    meses = {"January": "Enero", "February": "Febrero", "March": "Marzo",
             "April": "Abril", "May": "Mayo", "June": "Junio", "July": "Julio",
             "August": "Agosto", "September": "Septiembre", "October": "Octubre",
             "November": "Noviembre", "December": "Diciembre"}
    for en, es in {**dias, **meses}.items():
        fecha = fecha.replace(en, es)
 
    prompt = f"""Eres Alfred, el asistente personal de Checho. Genera un reporte matutino llamado "Agenda del Dia" con formato bonito para Telegram (usa emojis apropiados).
Fecha: {fecha}
Hora del reporte: {hora}
EVENTOS DE HOY EN GOOGLE CALENDAR:
{calendar_text}
EMAILS URGENTES (ultimas 24h):
{email_text}
Instrucciones: saludo amigable, emojis de reloj, seccion emails urgentes, resumen ejecutivo, formato Telegram (*negrita*, _cursiva_), tono de mayordomo britanico, maximo 2000 chars."""
 
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
 
    # Reintentos ante error 529 (API saturada) — espera progresiva: 30s, 60s, 90s
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
                    wait = 30 * (attempt + 1)   # 30s → 60s → 90s
                    print(f"  API saturada (529). Reintento {attempt+1}/{max_attempts-1} en {wait}s...")
                    time.sleep(wait)
                else:
                    raise Exception(
                        f"API de Anthropic no disponible despues de {max_attempts} intentos (error 529)."
                    ) from e
            else:
                raise   # otros errores los propagamos inmediatamente
 
 
def send_telegram(text):
    url     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    response = requests.post(url, json=payload, timeout=30)
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise Exception(f"Telegram error: {result}")
    print("Mensaje enviado por Telegram!")
    return result
 
 
def main():
    print("Alfred - Agenda del Dia")
    print("=" * 50)
    print("[1/4] Autenticando con Google...")
    creds = get_google_credentials()
    print("[2/4] Consultando Google Calendar...")
    calendar_text = get_calendar_events(creds)
    print("[3/4] Buscando emails urgentes...")
    email_text = get_urgent_emails(creds)
    print("[4/4] Generando reporte con Claude...")
    report = format_with_claude(calendar_text, email_text)
    print(f"Reporte generado ({len(report)} chars)")
    print("Enviando por Telegram...")
    send_telegram(report)
    print("Alfred ha completado la Agenda del Dia!")
 
 
if __name__ == "__main__":
    main()
