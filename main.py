import os
import json
import asyncio
import smtplib
import requests
import io
import uuid
import threading
import pdfplumber
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from datetime import datetime
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, EmailStr
from dotenv import load_dotenv
from google import genai as google_genai
from google.oauth2 import service_account
from googleapiclient.discovery import build
from fpdf import FPDF

load_dotenv()

app = FastAPI(title="Fedor Sawoloka - API Backend v4.0")

# ============================================================
# JOB QUEUE — Sistema de procesamiento asíncrono
# ============================================================
# Almacena los trabajos en memoria. En Render (plan Starter)
# el proceso vive indefinidamente, así que esto es seguro.
# Los trabajos se limpian automáticamente después de 1 hora.

jobs: Dict[str, Dict[str, Any]] = {}
jobs_lock = threading.Lock()

def crear_job(job_id: str):
    """Crea un nuevo trabajo en estado 'processing'."""
    with jobs_lock:
        jobs[job_id] = {
            'status': 'processing',
            'created_at': datetime.now().isoformat(),
            'pdf_bytes': None,
            'filename': None,
            'error': None
        }

def completar_job(job_id: str, pdf_bytes: bytes, filename: str):
    """Marca un trabajo como completado con el PDF generado."""
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id]['status'] = 'done'
            jobs[job_id]['pdf_bytes'] = pdf_bytes
            jobs[job_id]['filename'] = filename

def fallar_job(job_id: str, error: str):
    """Marca un trabajo como fallido."""
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id]['status'] = 'error'
            jobs[job_id]['error'] = error

def limpiar_jobs_viejos():
    """Elimina trabajos con más de 1 hora de antigüedad."""
    from datetime import timedelta
    ahora = datetime.now()
    con_jobs_lock = threading.Lock()
    with jobs_lock:
        ids_a_eliminar = []
        for jid, job in jobs.items():
            try:
                creado = datetime.fromisoformat(job['created_at'])
                if (ahora - creado) > timedelta(hours=1):
                    ids_a_eliminar.append(jid)
            except:
                pass
        for jid in ids_a_eliminar:
            del jobs[jid]

# CORS: permitir peticiones desde yosoyelruso.com y localhost
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://yosoyelruso.com",
        "http://yosoyelruso.com",
        "http://localhost",
        "http://localhost:8000",
        "http://127.0.0.1:5500",
        "*"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Configuración ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MAILCHIMP_API_KEY = os.getenv("MAILCHIMP_API_KEY")
MAILCHIMP_LIST_ID = os.getenv("MAILCHIMP_LIST_ID")
MAILCHIMP_SERVER_PREFIX = os.getenv("MAILCHIMP_SERVER_PREFIX", "us7")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "google_credentials.json")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
GMAIL_USER = os.getenv("GMAIL_USER", "fedor.sawoloka@gmail.com")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")

# ID de la hoja de cálculo para la lista de acceso del Programa Anti-Inercia
# Usa la misma hoja principal; la lista de acceso está en la pestaña "Acceso_Programa"
PROGRAMA_SHEET_ID = os.getenv("PROGRAMA_SHEET_ID", GOOGLE_SHEET_ID)

# ============================================================
# MODELOS DE DATOS — Configura tu IA (existente)
# ============================================================

class FormData(BaseModel):
    email: str
    mailchimp_consent: bool = False
    nombre_cargo: str
    filosofia_trabajo: str
    responsabilidades: str
    diferenciador: str
    audiencia: str
    proyecto_actual: str
    cuello_botella: str
    uso_ia: List[str]
    nivel_ayuda: List[str]
    nivel_autonomia: List[str]
    tipo_resultado: List[str]
    importancia_accion: List[str]
    estilo_comunicacion: List[str]
    palabras_evitar: str
    formato_preferido: List[str]
    enlaces_referencia: Optional[str] = ""

class GenerateResponse(BaseModel):
    success: bool
    document: Optional[str] = None
    error: Optional[str] = None
    fallback: bool = False
    email_sent: bool = False

class PdfRequest(BaseModel):
    document: str
    nombre: Optional[str] = "Profesional"

# ============================================================
# MODELOS DE DATOS — Programa Anti-Inercia (nuevo)
# ============================================================

class AccessCheckRequest(BaseModel):
    email: str

class AccessCheckResponse(BaseModel):
    allowed: bool
    message: str

# ============================================================
# FUNCIONES AUXILIARES — Configura tu IA (existente, sin cambios)
# ============================================================

def classify_profile(data: FormData) -> dict:
    nombre_lower = data.nombre_cargo.lower()
    profile_type = "Otro"
    if any(w in nombre_lower for w in ["ceo", "director", "presidente", "vp", "chief"]):
        profile_type = "Ejecutivo"
    elif any(w in nombre_lower for w in ["gerente", "manager", "jefe", "head"]):
        profile_type = "Gerente"
    elif any(w in nombre_lower for w in ["dueño", "propietario", "fundador", "owner"]):
        profile_type = "Dueño de negocio"
    elif any(w in nombre_lower for w in ["emprendedor", "entrepreneur", "startup"]):
        profile_type = "Emprendedor"
    elif any(w in nombre_lower for w in ["consultor", "consultant", "asesor", "advisor"]):
        profile_type = "Consultor"
    elif any(w in nombre_lower for w in ["marketing", "marketer", "growth", "publicidad"]):
        profile_type = "Marketer"
    elif any(w in nombre_lower for w in ["creador", "creator", "content", "influencer"]):
        profile_type = "Creador"
    elif any(w in nombre_lower for w in ["freelance", "independiente", "autónomo"]):
        profile_type = "Freelancer"

    uso_str = " ".join(data.uso_ia).lower()
    cuello_lower = data.cuello_botella.lower()
    combined = uso_str + " " + cuello_lower
    need = "Claridad"
    if any(w in combined for w in ["productividad", "tiempo", "eficiencia", "automatizar"]):
        need = "Productividad"
    elif any(w in combined for w in ["contenido", "escribir", "publicar", "redes", "post"]):
        need = "Contenido"
    elif any(w in combined for w in ["organizar", "organización", "orden", "caos"]):
        need = "Organización"
    elif any(w in combined for w in ["estrategia", "plan", "dirección", "rumbo"]):
        need = "Estrategia"
    elif any(w in combined for w in ["vender", "ventas", "clientes", "conversión"]):
        need = "Ventas"
    elif any(w in combined for w in ["delegar", "equipo", "team", "colaborar"]):
        need = "Delegación"
    elif any(w in combined for w in ["sistema", "proceso", "flujo", "workflow"]):
        need = "Sistemas"
    elif any(w in combined for w in ["posicionamiento", "marca", "branding", "reputación"]):
        need = "Posicionamiento"
    elif any(w in combined for w in ["decisión", "decidir", "priorizar", "elegir"]):
        need = "Toma de decisiones"

    autonomia_str = " ".join(data.nivel_autonomia).lower()
    resultado_str = " ".join(data.tipo_resultado).lower()
    maturity = "Explorador"
    if "copiloto" in autonomia_str or "sistemas completos" in resultado_str:
        maturity = "Listo para ejecutar"
    elif "planes" in resultado_str or "estructura" in autonomia_str:
        maturity = "En transición"

    score = 0
    decision_roles = ["ceo", "director", "gerente", "dueño", "fundador", "propietario", "vp", "chief"]
    if any(w in nombre_lower for w in decision_roles):
        score += 2
    if len(data.proyecto_actual) > 30:
        score += 2
    if len(data.cuello_botella) > 50:
        score += 2
    if data.enlaces_referencia and ("http" in data.enlaces_referencia or "www" in data.enlaces_referencia):
        score += 1
    if len(data.filosofia_trabajo) > 50 and len(data.diferenciador) > 50:
        score += 1
    if "copiloto" in autonomia_str or "crítico" in " ".join(data.importancia_accion).lower():
        score += 2

    if score <= 3:
        commercial_potential = "Frío"
    elif score <= 6:
        commercial_potential = "Interesante"
    elif score <= 8:
        commercial_potential = "Calificado"
    else:
        commercial_potential = "Premium"

    return {
        "profile_type": profile_type,
        "need": need,
        "maturity": maturity,
        "commercial_potential": commercial_potential,
        "score": score
    }


def generate_document_gemini(data: FormData) -> str:
    client = google_genai.Client(api_key=GEMINI_API_KEY)
    uso_str = ", ".join(data.uso_ia) if data.uso_ia else "No especificado"
    nivel_ayuda_str = ", ".join(data.nivel_ayuda) if data.nivel_ayuda else "No especificado"
    nivel_autonomia_str = ", ".join(data.nivel_autonomia) if data.nivel_autonomia else "No especificado"
    tipo_resultado_str = ", ".join(data.tipo_resultado) if data.tipo_resultado else "No especificado"
    importancia_accion_str = ", ".join(data.importancia_accion) if data.importancia_accion else "No especificado"
    estilo_str = ", ".join(data.estilo_comunicacion) if data.estilo_comunicacion else "No especificado"
    formato_str = ", ".join(data.formato_preferido) if data.formato_preferido else "No especificado"

    prompt = f"""Eres un experto en inteligencia artificial y productividad profesional. Con base en las siguientes respuestas de un profesional, genera un Documento Maestro de Contexto claro, estructurado y en primera persona, listo para ser pegado en cualquier chat de IA (ChatGPT, Gemini, Claude, etc.).

INSTRUCCIONES CRÍTICAS:
- El documento debe estar en PRIMERA PERSONA (yo soy, yo trabajo, yo quiero...)
- NO debe ser narrativo ni inspiracional — debe ser directamente operativo
- La IA que lo lea debe entender exactamente: quién es el usuario, cómo trabaja, qué quiere lograr con la IA, y cómo debe comportarse con él
- Incluye una sección de "Instrucciones implícitas para la IA" al final, donde traduces toda la información anterior en directrices concretas de comportamiento
- El documento debe cubrir TODAS las secciones indicadas en la estructura
- Al final, en línea aparte, incluye exactamente: ---\\nDocumento generado con base en la metodología Gold Standard de Fedor Sawoloka.

ESTRUCTURA OBLIGATORIA DEL DOCUMENTO:
1. Identidad profesional
2. Contexto de trabajo
3. Objetivo de uso de IA
4. Nivel de autonomía requerido
5. Estilo de comunicación
6. Formato de respuesta preferido
7. Nivel de ejecución esperado
8. Instrucciones implícitas para la IA

RESPUESTAS DEL USUARIO:

SECCIÓN 1 - IDENTIDAD PROFESIONAL:
- Nombre y cargo: {data.nombre_cargo}
- Filosofía de trabajo: {data.filosofia_trabajo}
- Responsabilidades principales: {data.responsabilidades}
- Diferenciador profesional: {data.diferenciador}

SECCIÓN 2 - CONTEXTO DE TRABAJO:
- Audiencia / cliente / equipo: {data.audiencia}
- Proyecto o área de enfoque actual: {data.proyecto_actual}
- Mayor problema o cuello de botella: {data.cuello_botella}

SECCIÓN 3 - COMPORTAMIENTO DE LA IA:
- Para qué quiere usar la IA: {uso_str}
- Nivel de ayuda esperado: {nivel_ayuda_str}
- Nivel de autonomía requerido: {nivel_autonomia_str}
- Tipo de resultado esperado: {tipo_resultado_str}
- Importancia de la acción (no solo pensar): {importancia_accion_str}

SECCIÓN 4 - ESTILO DE COMUNICACIÓN:
- Estilo de comunicación preferido: {estilo_str}
- Palabras o estilos a evitar: {data.palabras_evitar}
- Formato de información preferido: {formato_str}

SECCIÓN 5 - CONTEXTO ADICIONAL:
- Referencias / enlaces: {data.enlaces_referencia or 'No especificado'}
"""
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    return response.text


def generate_document_fallback(data: FormData) -> str:
    uso_str = ", ".join(data.uso_ia) if data.uso_ia else "No especificado"
    nivel_ayuda_str = ", ".join(data.nivel_ayuda) if data.nivel_ayuda else "No especificado"
    nivel_autonomia_str = ", ".join(data.nivel_autonomia) if data.nivel_autonomia else "No especificado"
    tipo_resultado_str = ", ".join(data.tipo_resultado) if data.tipo_resultado else "No especificado"
    importancia_accion_str = ", ".join(data.importancia_accion) if data.importancia_accion else "No especificado"
    estilo_str = ", ".join(data.estilo_comunicacion) if data.estilo_comunicacion else "No especificado"
    formato_str = ", ".join(data.formato_preferido) if data.formato_preferido else "No especificado"

    doc = f"""# Documento Maestro de Contexto

## 1. Identidad Profesional
{data.nombre_cargo}

Mi filosofía de trabajo: {data.filosofia_trabajo}

Mis responsabilidades principales:
{data.responsabilidades}

Lo que me diferencia: {data.diferenciador}

## 2. Contexto de Trabajo
Trabajo principalmente con: {data.audiencia}

Proyecto o área de enfoque actual: {data.proyecto_actual}

Mi principal cuello de botella: {data.cuello_botella}

## 3. Objetivo de Uso de IA
Quiero usar la IA principalmente para: {uso_str}

## 4. Nivel de Autonomía Requerido
Nivel de ayuda que espero: {nivel_ayuda_str}
Nivel de autonomía que quiero: {nivel_autonomia_str}

## 5. Estilo de Comunicación
Estilo preferido: {estilo_str}
Palabras o estilos a evitar: {data.palabras_evitar}

## 6. Formato de Respuesta Preferido
{formato_str}

## 7. Nivel de Ejecución Esperado
Tipo de resultado que espero: {tipo_resultado_str}
Importancia de la acción (no solo pensar): {importancia_accion_str}

## 8. Instrucciones Implícitas para la IA
- Respóndeme siempre en primera persona y de forma directa
- Adapta tu tono a: {estilo_str}
- Evita: {data.palabras_evitar}
- Entrega resultados en formato: {formato_str}
- Mi nivel de autonomía esperado es: {nivel_autonomia_str}
- Prioriza la acción sobre la teoría: {importancia_accion_str}

## Referencias
{data.enlaces_referencia or 'No especificado'}

---
Documento generado con base en la metodología Gold Standard de Fedor Sawoloka."""
    return doc


def save_to_google_sheets(data: FormData, tags: dict):
    try:
        if GOOGLE_CREDENTIALS_JSON:
            import json as json_module
            creds_dict = json_module.loads(GOOGLE_CREDENTIALS_JSON)
            creds = service_account.Credentials.from_service_account_info(
                creds_dict,
                scopes=["https://www.googleapis.com/auth/spreadsheets"]
            )
        else:
            credentials_path = GOOGLE_CREDENTIALS_FILE
            if not os.path.isabs(credentials_path):
                credentials_path = os.path.join(os.path.dirname(__file__), credentials_path)
            creds = service_account.Credentials.from_service_account_file(
                credentials_path,
                scopes=["https://www.googleapis.com/auth/spreadsheets"]
            )

        service = build("sheets", "v4", credentials=creds)
        sheet = service.spreadsheets()

        uso_str = ", ".join(data.uso_ia) if data.uso_ia else ""
        nivel_ayuda_str = ", ".join(data.nivel_ayuda) if data.nivel_ayuda else ""
        nivel_autonomia_str = ", ".join(data.nivel_autonomia) if data.nivel_autonomia else ""
        tipo_resultado_str = ", ".join(data.tipo_resultado) if data.tipo_resultado else ""
        importancia_accion_str = ", ".join(data.importancia_accion) if data.importancia_accion else ""
        estilo_str = ", ".join(data.estilo_comunicacion) if data.estilo_comunicacion else ""
        formato_str = ", ".join(data.formato_preferido) if data.formato_preferido else ""

        row = [
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            data.email,
            data.nombre_cargo,
            data.filosofia_trabajo,
            data.responsabilidades,
            data.diferenciador,
            data.audiencia,
            data.proyecto_actual,
            data.cuello_botella,
            uso_str,
            nivel_ayuda_str,
            nivel_autonomia_str,
            tipo_resultado_str,
            importancia_accion_str,
            estilo_str,
            data.palabras_evitar,
            formato_str,
            data.enlaces_referencia or "",
            "Sí" if data.mailchimp_consent else "No",
            tags.get("profile_type", ""),
            tags.get("need", ""),
            tags.get("maturity", ""),
            tags.get("commercial_potential", ""),
            str(tags.get("score", 0))
        ]

        body = {"values": [row]}
        sheet.values().append(
            spreadsheetId=GOOGLE_SHEET_ID,
            range="A:X",
            valueInputOption="RAW",
            body=body
        ).execute()
        return True
    except Exception as e:
        print(f"Error guardando en Google Sheets: {e}")
        return False


def subscribe_to_mailchimp(data: FormData, tags: dict):
    if not data.mailchimp_consent:
        return False
    try:
        nombre_parts = data.nombre_cargo.split(" ")
        first_name = nombre_parts[0] if nombre_parts else ""
        mailchimp_tags = ["configura-tu-ia"]
        profile = tags.get("profile_type", "").lower().replace(" ", "-")
        if profile and profile != "otro":
            mailchimp_tags.append(f"perfil-{profile}")
        need = tags.get("need", "").lower().replace(" ", "-")
        if need:
            mailchimp_tags.append(f"necesidad-{need}")
        maturity = tags.get("maturity", "").lower().replace(" ", "-")
        if maturity:
            mailchimp_tags.append(f"madurez-{maturity}")
        potential = tags.get("commercial_potential", "").lower()
        if potential:
            mailchimp_tags.append(f"potencial-{potential}")

        url = f"https://{MAILCHIMP_SERVER_PREFIX}.api.mailchimp.com/3.0/lists/{MAILCHIMP_LIST_ID}/members"
        payload = {
            "email_address": data.email,
            "status": "subscribed",
            "merge_fields": {"FNAME": first_name},
            "tags": mailchimp_tags
        }
        response = requests.post(url, auth=("anystring", MAILCHIMP_API_KEY), json=payload)
        if response.status_code == 400 and "already a list member" in response.text:
            import hashlib
            email_hash = hashlib.md5(data.email.lower().encode()).hexdigest()
            update_url = f"https://{MAILCHIMP_SERVER_PREFIX}.api.mailchimp.com/3.0/lists/{MAILCHIMP_LIST_ID}/members/{email_hash}"
            requests.patch(update_url, auth=("anystring", MAILCHIMP_API_KEY),
                           json={"merge_fields": {"FNAME": first_name}, "tags": mailchimp_tags})
            tags_url = f"https://{MAILCHIMP_SERVER_PREFIX}.api.mailchimp.com/3.0/lists/{MAILCHIMP_LIST_ID}/members/{email_hash}/tags"
            tags_payload = {"tags": [{"name": t, "status": "active"} for t in mailchimp_tags]}
            requests.post(tags_url, auth=("anystring", MAILCHIMP_API_KEY), json=tags_payload)
        return True
    except Exception as e:
        print(f"Error en Mailchimp: {e}")
        return False


def send_document_by_email(recipient_email: str, document: str, nombre_cargo: str):
    if not GMAIL_APP_PASSWORD:
        print("GMAIL_APP_PASSWORD no configurado, omitiendo envío de email")
        return False
    try:
        nombre = nombre_cargo.split(",")[0].strip() if "," in nombre_cargo else nombre_cargo.split()[0]
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "Tu Documento Maestro de Contexto para IA está listo"
        msg["From"] = f"Fedor Sawoloka <{GMAIL_USER}>"
        msg["To"] = recipient_email

        lines = []
        for line in document.split("\n"):
            if line.startswith("## "):
                lines.append(f"<h2>{line[3:]}</h2>")
            elif line.startswith("# "):
                lines.append(f"<h1>{line[2:]}</h1>")
            elif line.startswith("---"):
                lines.append("<hr>")
            elif line.strip() == "":
                lines.append("<br>")
            else:
                lines.append(f"<p>{line}</p>")
        doc_html_clean = "\n".join(lines)

        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; max-width: 700px; margin: 0 auto; padding: 20px; color: #2C3E50;">
            <div style="background: #2C3E50; padding: 20px; border-radius: 8px 8px 0 0;">
                <h1 style="color: white; margin: 0; font-size: 20px;">Tu Documento Maestro de Contexto</h1>
                <p style="color: #FF8C42; margin: 5px 0 0 0;">Generado con la metodología Gold Standard de Fedor Sawoloka</p>
            </div>
            <div style="background: #f5f6fa; padding: 25px; border-radius: 0 0 8px 8px; border: 1px solid #dee2e6;">
                <p>Hola, aquí está tu documento listo para usar en cualquier IA.</p>
                <p><strong>Instrucciones:</strong> Copia el texto del documento y pégalo al inicio de cualquier conversación con ChatGPT, Claude, Gemini u otra IA.</p>
                <hr style="border: 1px solid #dee2e6; margin: 20px 0;">
                {doc_html_clean}
                <hr style="border: 1px solid #dee2e6; margin: 20px 0;">
                <p style="font-size: 12px; color: #6c757d;">Generado en <a href="https://yosoyelruso.com/configura-tu-ia/" style="color: #FF8C42;">yosoyelruso.com/configura-tu-ia</a></p>
            </div>
        </body>
        </html>
        """
        text_body = f"Tu Documento Maestro de Contexto\n\n{document}\n\n---\nGenerado en yosoyelruso.com/configura-tu-ia"
        msg.attach(MIMEText(text_body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_USER, recipient_email, msg.as_string())
        print(f"Email enviado exitosamente a {recipient_email}")
        return True
    except Exception as e:
        print(f"Error enviando email: {e}")
        return False


# ============================================================
# FUNCIONES AUXILIARES — Programa Anti-Inercia (nuevo)
# ============================================================

def get_google_sheets_service():
    """Devuelve un servicio autenticado de Google Sheets."""
    if GOOGLE_CREDENTIALS_JSON:
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = service_account.Credentials.from_service_account_info(
            creds_dict,
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
    else:
        credentials_path = GOOGLE_CREDENTIALS_FILE
        if not os.path.isabs(credentials_path):
            credentials_path = os.path.join(os.path.dirname(__file__), credentials_path)
        creds = service_account.Credentials.from_service_account_file(
            credentials_path,
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
    return build("sheets", "v4", credentials=creds)


def check_email_access(email: str) -> bool:
    """
    Verifica si el email tiene acceso al Programa Anti-Inercia.
    Lee la pestaña 'Acceso_Programa' del Google Sheet.
    Columna A: email | Columna B: activo (SI/NO)
    """
    try:
        service = get_google_sheets_service()
        sheet = service.spreadsheets()
        result = sheet.values().get(
            spreadsheetId=PROGRAMA_SHEET_ID,
            range="Acceso_Programa!A:B"
        ).execute()
        values = result.get("values", [])
        email_lower = email.strip().lower()
        for row in values:
            if len(row) >= 1 and row[0].strip().lower() == email_lower:
                # Si hay columna B, verificar que sea "SI" o "SÍ" o "si" o "sí"
                if len(row) >= 2:
                    estado = row[1].strip().upper()
                    return estado in ["SI", "SÍ", "YES", "ACTIVO", "1", "TRUE"]
                else:
                    # Si no hay columna B, la presencia del email ya da acceso
                    return True
        return False
    except Exception as e:
        print(f"Error verificando acceso: {e}")
        return False


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Extrae texto limpio de un PDF en bytes usando pdfplumber."""
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            text_parts = []
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    text_parts.append(text)
            return "\n".join(text_parts)
    except Exception as e:
        print(f"Error extrayendo texto del PDF: {e}")
        return ""


def limpiar_para_pdf(texto: str) -> str:
    """
    Convierte caracteres Unicode especiales a equivalentes ASCII/Latin-1
    compatibles con la fuente Helvetica de fpdf2.
    """
    reemplazos = {
        # Comillas tipográficas
        '\u2018': "'", '\u2019': "'", '\u201a': "'", '\u201b': "'",
        '\u201c': '"', '\u201d': '"', '\u201e': '"', '\u201f': '"',
        # Guiones especiales
        '\u2013': '-', '\u2014': '-', '\u2015': '-',
        # Puntos suspensivos
        '\u2026': '...',
        # Espacios especiales
        '\u00a0': ' ', '\u202f': ' ', '\u2009': ' ',
        # Flechas y símbolos comunes
        '\u2192': '->', '\u2190': '<-', '\u2022': '-', '\u25cf': '-',
        '\u2713': 'OK', '\u2714': 'OK', '\u2715': 'X', '\u2716': 'X',
        '\u2610': '[ ]', '\u2611': '[X]',
        # Fracciones y superscripts
        '\u00bd': '1/2', '\u00bc': '1/4', '\u00be': '3/4',
        # Otros comunes
        '\u00b7': '-', '\u2217': '*', '\u00d7': 'x', '\u00f7': '/',
    }
    for char, reemplazo in reemplazos.items():
        texto = texto.replace(char, reemplazo)
    # Eliminar cualquier caracter fuera del rango Latin-1 que quede
    resultado = []
    for c in texto:
        try:
            c.encode('latin-1')
            resultado.append(c)
        except (UnicodeEncodeError, UnicodeDecodeError):
            resultado.append('?')
    return ''.join(resultado)


def generate_pdf_branded(content: str, titulo: str, subtitulo: str, nombre_archivo: str) -> bytes:
    """
    Genera un PDF con branding de la marca Anti-Inercia.
    Paleta: Azul marino #2C3E50, Naranja #FF8C42, Blanco, Gris claro.
    """
    # Limpiar TODOS los textos antes de cualquier operacion PDF
    content = limpiar_para_pdf(content)
    titulo = limpiar_para_pdf(titulo)
    subtitulo = limpiar_para_pdf(subtitulo)

    pdf = FPDF(orientation='P', unit='mm', format='A4')
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    # Encabezado con fondo azul marino
    pdf.set_fill_color(44, 62, 80)
    pdf.rect(0, 0, 210, 28, 'F')
    pdf.set_text_color(255, 255, 255)
    pdf.set_font('Helvetica', 'B', 14)
    pdf.set_xy(15, 7)
    pdf.cell(0, 8, titulo, ln=False)
    pdf.set_font('Helvetica', '', 9)
    pdf.set_text_color(255, 140, 66)
    pdf.set_xy(15, 18)
    pdf.cell(0, 6, subtitulo, ln=False)

    pdf.set_y(35)
    pdf.set_text_color(44, 62, 80)

    lines = content.split('\n')
    for line in lines:
        line_stripped = line.strip()
        if line_stripped.startswith('## '):
            pdf.ln(5)
            pdf.set_font('Helvetica', 'B', 12)
            pdf.set_text_color(44, 62, 80)
            pdf.multi_cell(0, 6, line_stripped[3:], ln=True)
            pdf.set_draw_color(255, 140, 66)
            pdf.set_line_width(0.5)
            pdf.line(15, pdf.get_y(), 195, pdf.get_y())
            pdf.ln(3)
        elif line_stripped.startswith('# '):
            pdf.ln(4)
            pdf.set_font('Helvetica', 'B', 14)
            pdf.set_text_color(44, 62, 80)
            pdf.multi_cell(0, 7, line_stripped[2:], ln=True)
            pdf.ln(2)
        elif line_stripped.startswith('### '):
            pdf.ln(3)
            pdf.set_font('Helvetica', 'B', 11)
            pdf.set_text_color(255, 140, 66)
            pdf.multi_cell(0, 6, line_stripped[4:], ln=True)
            pdf.ln(1)
        elif line_stripped.startswith('---'):
            pdf.ln(2)
            pdf.set_draw_color(200, 200, 200)
            pdf.set_line_width(0.3)
            pdf.line(15, pdf.get_y(), 195, pdf.get_y())
            pdf.ln(3)
        elif line_stripped == '':
            pdf.ln(3)
        else:
            clean = line_stripped.replace('**', '').replace('*', '')
            # Detectar listas con guión o bullet
            if clean.startswith('- ') or clean.startswith('• '):
                pdf.set_font('Helvetica', '', 10)
                pdf.set_text_color(60, 60, 60)
                pdf.set_x(20)
                pdf.multi_cell(175, 5, clean, ln=True)
            else:
                pdf.set_font('Helvetica', '', 10)
                pdf.set_text_color(60, 60, 60)
                pdf.multi_cell(0, 5, clean, ln=True)

    # Pie de página
    pdf.set_y(-15)
    pdf.set_font('Helvetica', 'I', 8)
    pdf.set_text_color(150, 150, 150)
    pdf.cell(0, 10, f'Programa Anti-Inercia de Marca Personal  |  yosoyelruso.com  |  {datetime.now().strftime("%d/%m/%Y")}', align='C')

    return bytes(pdf.output())


def save_programa_lead(email: str, modulo: int, datos_adicionales: dict = None):
    """Registra la actividad del usuario en el programa en Google Sheets."""
    try:
        service = get_google_sheets_service()
        sheet = service.spreadsheets()
        row = [
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            email,
            f"Módulo {modulo}",
            json.dumps(datos_adicionales or {}, ensure_ascii=False)
        ]
        body = {"values": [row]}
        sheet.values().append(
            spreadsheetId=PROGRAMA_SHEET_ID,
            range="Actividad_Programa!A:D",
            valueInputOption="RAW",
            body=body
        ).execute()
        return True
    except Exception as e:
        print(f"Error guardando actividad del programa: {e}")
        return False


def generate_modulo0_gemini(respuestas: dict) -> str:
    """Genera el Mapa de Fricciones (Módulo 0) usando Gemini."""
    client = google_genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""Eres el motor de análisis del Programa Anti-Inercia de Marca Personal, creado por Fedor Sawoloka.

Este programa se basa en la Metodología Gold Standard Anti-Inercia, cuyos principios fundamentales son:
1. DIAGNÓSTICO PRIMERO, ESTRATEGIA DESPUÉS: Nunca proponer soluciones sin un diagnóstico profundo. La calidad de la estrategia depende de la calidad del análisis.
2. VALOR REAL VS. VALOR PERCIBIDO: El problema central de la mayoría de profesionales es que tienen más valor del que comunican. Tu trabajo es identificar esa brecha.
3. ROI COMO MÉTRICA PRINCIPAL: El éxito se mide en impacto de negocio (clientes, ingresos, posicionamiento), no en métricas de vanidad.
4. SISTEMA SOBRE TÁCTICAS: No se entregan listas de tareas, sino diagnósticos que permiten construir sistemas sostenibles.
5. ANÁLISIS SINCERO SOBRE PLANTILLA: Cada análisis es producto del caso específico, no de aplicar una fórmula genérica.

Tu tarea es generar el "Mapa de Fricciones" — el documento de diagnóstico del Módulo 0.

INSTRUCCIONES DE TONO Y ESTILO:
- Este es el PRIMER módulo del programa. La persona acaba de comenzar. Tu tono debe ser HONESTO Y DIRECTO, pero nunca brutal ni desmotivador.
- El objetivo es que el participante salga pensando: "Esto me abrió los ojos" — no "Esto me destruyó".
- Confronta la realidad con respeto. Nombra los problemas con claridad, pero siempre desde un lugar constructivo.
- NO uses lenguaje condescendiente ni paternalista. Habla de igual a igual, como un estratega que ve lo que el participante aún no puede ver.
- Identifica patrones reales. No repitas las respuestas del usuario — interprétalas.
- Si el diferenciador del usuario usa palabras vacías como "calidad", "experiencia" o "pasión", señálalo con tacto: no como un error, sino como una oportunidad de mejora.
- Formato: usa ## para secciones principales y ### para subsecciones.
- NO uses emojis ni símbolos decorativos.

VOCABULARIO DE LA METODOLOGÍA (úsalo de forma natural, no forzada):
- "Inercia": patrón de comportamiento que se repite y frena el avance
- "Valor real vs. valor percibido": brecha entre lo que el profesional vale y lo que el mercado percibe
- "Arquetipo": el posicionamiento único que el profesional debe ocupar (se desarrollará en módulos posteriores)
- "Paradoja": el problema central que resume la situación del participante (ej. "El experto que nadie conoce")
- "Anti-Inercia": la filosofía de romper patrones que frenan el crecimiento

ESTRUCTURA OBLIGATORIA DEL MAPA DE FRICCIONES:

## Diagnóstico de Punto Cero
Síntesis clara de dónde está parado el participante hoy. Identifica la "Paradoja" central — el problema raíz que resume su situación en una frase con nombre propio (ej. "El Experto Invisible", "El Profesional en Construcción"). Máximo 3 párrafos. Honesto, no cruel.

## Radiografía del Negocio
Análisis de la situación actual: fuente de clientes, flujo, formalización de oferta. Qué está funcionando (si algo lo está) y qué no. Termina con una implicación estratégica clara.

## Estado de la Presencia Digital
Qué tan visible y coherente es su presencia hoy. Identifica la brecha entre lo que existe y lo que necesita. Termina con una implicación estratégica clara.

## Fricciones Identificadas
Las 3 inercias principales que están frenando su avance. Para cada una:
### [Nombre de la inercia]
- Cómo se manifiesta en su día a día
- Qué la está causando (raíz real, no síntoma)

## Nivel de Disposición para Ejecutar
Análisis honesto del nivel de compromiso declarado vs. la evidencia en sus respuestas. Si hay contradicción entre lo que dice y lo que muestra, señálala con respeto. El objetivo es que el participante tome conciencia, no que se sienta juzgado.

## Lo que viene: Tus Próximas Prioridades
No estrategia de contenido todavía. Las 3 cosas que este participante debe resolver o clarificar antes de avanzar al Módulo 1. Formuladas como oportunidades, no como reproches.

---
Documento generado por el Programa Anti-Inercia de Marca Personal | Metodologia de Fedor Sawoloka | yosoyelruso.com

RESPUESTAS DEL PARTICIPANTE:

BLOQUE 1 — NEGOCIO HOY:
- ¿A qué te dedicas?: {respuestas.get('dedicacion', 'No respondido')}
- Tiempo ejerciendo de forma independiente: {respuestas.get('tiempo_independiente', 'No respondido')}
- Principal fuente de clientes: {respuestas.get('fuente_clientes', 'No respondido')}
- Clientes activos: {respuestas.get('clientes_activos', 'No respondido')}
- Flujo de clientes actual: {respuestas.get('flujo_clientes', 'No respondido')}

BLOQUE 2 — OFERTA:
- ¿Tiene servicios y precios definidos?: {respuestas.get('servicios_definidos', 'No respondido')}
- Facilidad para explicar lo que hace: {respuestas.get('facilidad_explicar', 'No respondido')}
- Diferenciador (palabras del usuario): {respuestas.get('diferenciador', 'No respondido')}
- Resultado concreto que obtiene un cliente: {respuestas.get('resultado_cliente', 'No respondido')}

BLOQUE 3 — PRESENCIA DIGITAL:
- Plataformas activas: {respuestas.get('plataformas', 'No respondido')}
- Frecuencia de publicación: {respuestas.get('frecuencia_publicacion', 'No respondido')}
- Qué aparece al buscar su nombre en Google: {respuestas.get('google_resultado', 'No respondido')}
- Qué encuentra un cliente potencial en su perfil: {respuestas.get('perfil_actual', 'No respondido')}

BLOQUE 4 — FRICCIONES REALES:
- Razón principal por la que no está donde quiere: {respuestas.get('razon_principal', 'No respondido')}
- Ampliación de la razón (si aplica): {respuestas.get('razon_ampliacion', 'No respondido')}
- ¿Ha intentado trabajar su marca antes?: {respuestas.get('intento_previo', 'No respondido')}
- ¿Qué falló en intentos anteriores?: {respuestas.get('que_fallo', 'No respondido')}
- Nivel de disposición para ejecutar (1-10): {respuestas.get('disposicion', 'No respondido')}
- Qué necesitaría para que el programa valiera la pena: {respuestas.get('exito_definido', 'No respondido')}

BLOQUE 5 — CONTEXTO:
- Horas semanales disponibles: {respuestas.get('horas_semanales', 'No respondido')}
- Nivel de comodidad con herramientas digitales: {respuestas.get('nivel_digital', 'No respondido')}
- Presupuesto disponible: {respuestas.get('presupuesto', 'No respondido')}
"""

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    return response.text


# ============================================================
# ENDPOINT DE DIAGNÓSTICO (temporal — solo para debugging)
# ============================================================

@app.get("/programa/diagnostico")
def diagnostico_programa():
    """Endpoint temporal para verificar configuración del programa."""
    resultado = {}
    try:
        service = get_google_sheets_service()
        # Obtener info del spreadsheet (pestañas disponibles)
        spreadsheet = service.spreadsheets().get(
            spreadsheetId=PROGRAMA_SHEET_ID
        ).execute()
        pestanas = [s['properties']['title'] for s in spreadsheet.get('sheets', [])]
        resultado['sheet_id'] = PROGRAMA_SHEET_ID
        resultado['pestanas_disponibles'] = pestanas
        resultado['acceso_sheet'] = 'OK'

        # Intentar leer Acceso_Programa
        try:
            values_result = service.spreadsheets().values().get(
                spreadsheetId=PROGRAMA_SHEET_ID,
                range="Acceso_Programa!A:B"
            ).execute()
            valores = values_result.get('values', [])
            resultado['acceso_programa_filas'] = len(valores)
            resultado['acceso_programa_contenido'] = valores[:5]  # primeras 5 filas
        except Exception as e2:
            resultado['error_lectura_acceso_programa'] = str(e2)

        # Obtener email de la cuenta de servicio
        if GOOGLE_CREDENTIALS_JSON:
            creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
            resultado['service_account_email'] = creds_dict.get('client_email', 'no encontrado')

    except Exception as e:
        resultado['error'] = str(e)

    return resultado


# ============================================================
# ENDPOINTS — Configura tu IA (existentes, sin cambios)
# ============================================================

@app.get("/")
def root():
    return {"status": "ok", "service": "Fedor Sawoloka - Backend v3.0"}

@app.get("/health")
def health():
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

@app.post("/generate", response_model=GenerateResponse)
async def generate(data: FormData):
    tags = classify_profile(data)
    document = None
    fallback_used = False
    try:
        document = generate_document_gemini(data)
    except Exception as e:
        print(f"Gemini falló: {e}")
        fallback_used = True
        document = generate_document_fallback(data)
    try:
        save_to_google_sheets(data, tags)
    except Exception as e:
        print(f"Google Sheets falló: {e}")
    try:
        subscribe_to_mailchimp(data, tags)
    except Exception as e:
        print(f"Mailchimp falló: {e}")
    email_sent = False
    try:
        email_sent = send_document_by_email(data.email, document, data.nombre_cargo)
    except Exception as e:
        print(f"Email falló: {e}")
    return GenerateResponse(
        success=True,
        document=document,
        fallback=fallback_used,
        email_sent=email_sent
    )

@app.post("/download-pdf")
def download_pdf(req: PdfRequest):
    try:
        pdf = FPDF(orientation='P', unit='mm', format='A4')
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.add_page()
        pdf.set_fill_color(44, 62, 80)
        pdf.rect(0, 0, 210, 22, 'F')
        pdf.set_text_color(255, 255, 255)
        pdf.set_font('Helvetica', 'B', 13)
        pdf.set_xy(15, 8)
        pdf.cell(0, 8, 'Documento Maestro de Contexto', ln=False)
        pdf.set_font('Helvetica', '', 9)
        pdf.set_text_color(255, 140, 66)
        pdf.set_xy(15, 16)
        pdf.cell(0, 6, 'Metodologia Gold Standard  |  yosoyelruso.com', ln=False)
        pdf.set_y(28)
        pdf.set_text_color(44, 62, 80)
        lines = req.document.split('\n')
        for line in lines:
            line = line.strip()
            if line.startswith('## '):
                pdf.ln(4)
                pdf.set_font('Helvetica', 'B', 12)
                pdf.set_text_color(44, 62, 80)
                pdf.multi_cell(0, 6, line[3:], ln=True)
                pdf.set_draw_color(255, 140, 66)
                pdf.set_line_width(0.5)
                pdf.line(15, pdf.get_y(), 195, pdf.get_y())
                pdf.ln(3)
            elif line.startswith('# '):
                pdf.ln(4)
                pdf.set_font('Helvetica', 'B', 14)
                pdf.set_text_color(44, 62, 80)
                pdf.multi_cell(0, 7, line[2:], ln=True)
                pdf.ln(2)
            elif line.startswith('---'):
                pdf.ln(2)
                pdf.set_draw_color(200, 200, 200)
                pdf.set_line_width(0.3)
                pdf.line(15, pdf.get_y(), 195, pdf.get_y())
                pdf.ln(3)
            elif line == '':
                pdf.ln(3)
            else:
                clean = line.replace('**', '').replace('*', '')
                pdf.set_font('Helvetica', '', 10)
                pdf.set_text_color(60, 60, 60)
                pdf.multi_cell(0, 5, clean, ln=True)
        pdf_bytes = pdf.output()
        pdf_buffer = io.BytesIO(bytes(pdf_bytes))
        return StreamingResponse(
            pdf_buffer,
            media_type='application/pdf',
            headers={
                'Content-Disposition': 'attachment; filename="documento-maestro-contexto.pdf"',
                'Content-Type': 'application/pdf'
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'Error generando PDF: {str(e)}')


# ============================================================
# ENDPOINTS — Programa Anti-Inercia (nuevos)
# ============================================================

@app.post("/programa/check-access", response_model=AccessCheckResponse)
async def check_access(req: AccessCheckRequest):
    """
    Verifica si un email tiene acceso al Programa Anti-Inercia.
    Lee la pestaña 'Acceso_Programa' del Google Sheet.
    """
    allowed = check_email_access(req.email)
    if allowed:
        return AccessCheckResponse(
            allowed=True,
            message="Acceso verificado. Bienvenido al Programa Anti-Inercia."
        )
    else:
        return AccessCheckResponse(
            allowed=False,
            message="Este correo no tiene acceso al programa. Si ya realizaste tu pago, escríbele a Fedor directamente."
        )


@app.post("/programa/modulo0/generar")
async def generar_modulo0(
    email: str = Form(...),
    dedicacion: str = Form(...),
    tiempo_independiente: str = Form(...),
    fuente_clientes: str = Form(...),
    clientes_activos: str = Form(...),
    flujo_clientes: str = Form(...),
    servicios_definidos: str = Form(...),
    facilidad_explicar: str = Form(...),
    diferenciador: str = Form(...),
    resultado_cliente: str = Form(...),
    plataformas: str = Form(...),
    frecuencia_publicacion: str = Form(...),
    google_resultado: str = Form(...),
    perfil_actual: str = Form(...),
    razon_principal: str = Form(...),
    razon_ampliacion: str = Form(default=""),
    intento_previo: str = Form(...),
    que_fallo: str = Form(default=""),
    disposicion: str = Form(...),
    exito_definido: str = Form(...),
    horas_semanales: str = Form(...),
    nivel_digital: str = Form(...),
    presupuesto: str = Form(...)
):
    """
    Procesa el Módulo 0 del Programa Anti-Inercia:
    1. Verifica acceso del email
    2. Genera el Mapa de Fricciones con Gemini
    3. Registra actividad en Google Sheets
    4. Devuelve el PDF para descarga
    """

    # 1. Verificar acceso
    if not check_email_access(email):
        raise HTTPException(
            status_code=403,
            detail="Este correo no tiene acceso al programa."
        )

    # 2. Construir diccionario de respuestas
    respuestas = {
        "dedicacion": dedicacion,
        "tiempo_independiente": tiempo_independiente,
        "fuente_clientes": fuente_clientes,
        "clientes_activos": clientes_activos,
        "flujo_clientes": flujo_clientes,
        "servicios_definidos": servicios_definidos,
        "facilidad_explicar": facilidad_explicar,
        "diferenciador": diferenciador,
        "resultado_cliente": resultado_cliente,
        "plataformas": plataformas,
        "frecuencia_publicacion": frecuencia_publicacion,
        "google_resultado": google_resultado,
        "perfil_actual": perfil_actual,
        "razon_principal": razon_principal,
        "razon_ampliacion": razon_ampliacion,
        "intento_previo": intento_previo,
        "que_fallo": que_fallo,
        "disposicion": disposicion,
        "exito_definido": exito_definido,
        "horas_semanales": horas_semanales,
        "nivel_digital": nivel_digital,
        "presupuesto": presupuesto
    }

    # 3. Generar documento con Gemini
    try:
        documento = generate_modulo0_gemini(respuestas)
    except Exception as e:
        print(f"Gemini falló en Módulo 0: {e}")
        raise HTTPException(status_code=500, detail="Error generando el documento. Intenta de nuevo en unos minutos.")

    # 4. Registrar actividad
    try:
        save_programa_lead(email, 0, {"disposicion": disposicion, "horas": horas_semanales})
    except Exception as e:
        print(f"Error registrando actividad M0: {e}")

    # 5. Generar y devolver PDF
    try:
        pdf_bytes = generate_pdf_branded(
            content=documento,
            titulo="Mapa de Fricciones — Módulo 0",
            subtitulo="Programa Anti-Inercia de Marca Personal  |  yosoyelruso.com",
            nombre_archivo="mapa-de-fricciones-modulo-0.pdf"
        )
        pdf_buffer = io.BytesIO(pdf_bytes)
        return StreamingResponse(
            pdf_buffer,
            media_type='application/pdf',
            headers={
                'Content-Disposition': 'attachment; filename="mapa-de-fricciones-modulo-0.pdf"',
                'Content-Type': 'application/pdf'
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'Error generando PDF: {str(e)}')


# ============================================================
# FUNCIONES GENERADORAS — Módulos 1, 2 y 3
# ============================================================

def generate_modulo1_gemini(respuestas: dict, contexto_m0: str) -> str:
    """Genera el Documento de Auditoría y Diagnóstico (Módulo 1) usando Gemini."""
    client = google_genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""Eres el motor de análisis del Programa Anti-Inercia de Marca Personal, creado por Fedor Sawoloka.

Este módulo genera el "Documento de Auditoría y Diagnóstico" — el Entregable 1 de la Metodología Gold Standard Anti-Inercia.

METODOLOGÍA GOLD STANDARD — PRINCIPIOS QUE RIGEN ESTE DOCUMENTO:
1. DIAGNÓSTICO BASADO EN EVIDENCIA: Cada hallazgo debe estar respaldado por datos específicos del participante, no en generalizaciones.
2. VALOR REAL VS. VALOR PERCIBIDO: Identifica la brecha entre lo que el profesional realmente vale y lo que el mercado percibe de él.
3. ANÁLISIS SINCERO SOBRE PLANTILLA: Este documento es producto del caso específico, no de aplicar una fórmula genérica.
4. SISTEMA SOBRE TÁCTICAS: El objetivo es identificar el sistema que falta, no dar una lista de tareas.
5. ROI COMO MÉTRICA: El éxito se mide en impacto de negocio, no en métricas de vanidad.

INSTRUCCIONES DE TONO Y ESTILO:
- Tono: directo, analítico, profesional. Como un estratega que ve el panorama completo.
- NO es un documento motivacional, pero tampoco es brutal. Es honesto y constructivo.
- Usa el vocabulario de la metodología: "inercia", "valor percibido", "arquetipo", "paradoja", "Anti-Inercia".
- Cada sección debe terminar con una implicación estratégica clara.
- Formato: ## para secciones principales, ### para subsecciones.
- NO uses emojis ni símbolos decorativos.

ESTRUCTURA OBLIGATORIA — ENTREGABLE 1 (AUDITORÍA Y DIAGNÓSTICO):

## Resumen Ejecutivo
La "Paradoja" o problema central del participante (ej. "El Experto que el Mercado No Puede Ver"). La brecha entre su valor real y su valor percibido, en 2-3 párrafos directos.

## Oportunidad de Mercado
Contextualiza el servicio del participante dentro de una tendencia real. Por qué lo que ofrece es una necesidad, no un lujo. Basado en su sector y audiencia declarados.

## Auditoría de Presencia Digital
Análisis detallado de cada canal declarado por el participante. Para cada uno: qué existe, qué comunica, qué falta y qué implicación tiene. Incluye el análisis del perfil principal con la bio que el participante describió.

## Análisis de Competencia y Referentes
Basado en los 3 perfiles analizados por el participante. Para cada uno: posicionamiento, estrategia de contenido, fortalezas, debilidades y lección aplicable. Cierra con la síntesis: el hueco de mercado que el participante puede ocupar.

## Diagnóstico FODA
Matriz con fortalezas, oportunidades, debilidades y amenazas. Cada punto con evidencia concreta de las respuestas del participante. Sin adjetivos vacíos.

## Identificación de Inercias Críticas
Las 3 inercias más importantes identificadas en este módulo (pueden complementar o profundizar las del Módulo 0). Para cada una:
### [Nombre de la inercia]
- Cómo se manifiesta con evidencia específica
- Raíz real (no el síntoma)
- Impacto en el negocio si no se rompe

## El Arquetipo Ganador
El posicionamiento único que este participante debe ocupar. Nombre del arquetipo, sus atributos y la nueva Propuesta de Valor Única (PUV). Basado en el diferenciador real identificado en sus respuestas y en el análisis de competencia.

## Diagnóstico Final
El problema raíz resumido en una sola frase contundente. La síntesis de todo el análisis anterior.

## El Siguiente Paso
Puente hacia el Módulo 2 (Síntesis Estratégica y ADN). Qué construirán juntos en el próximo módulo y por qué este diagnóstico lo hace posible.

---
Documento generado por el Programa Anti-Inercia de Marca Personal | Metodologia de Fedor Sawoloka | yosoyelruso.com

CONTEXTO DEL MÓDULO 0 (Mapa de Fricciones previo del participante):
{contexto_m0[:3000] if contexto_m0 else 'No disponible'}

RESPUESTAS DEL MÓDULO 1:

PARTE 1 — AUDITORÍA DE PRESENCIA ACTUAL:
- Foto de perfil: {respuestas.get('foto_perfil', 'No respondido')}
- Bio actual (texto exacto): {respuestas.get('bio_actual', 'No respondido')}
- ¿Un desconocido entendería en 10 segundos qué hace?: {respuestas.get('claridad_bio', 'No respondido')}
- ¿Tiene llamado a la acción en el perfil?: {respuestas.get('cta_perfil', 'No respondido')}
- Patrón en las últimas 9 publicaciones: {respuestas.get('patron_publicaciones', 'No respondido')}
- Nivel de interacción promedio: {respuestas.get('nivel_interaccion', 'No respondido')}

CANALES SECUNDARIOS:
{respuestas.get('canales_secundarios', 'No respondido')}

AUDITORÍA WEB:
- ¿Tiene sitio web?: {respuestas.get('tiene_web', 'No respondido')}
- Qué encuentra alguien en los primeros 5 segundos: {respuestas.get('web_primeros_segundos', 'No respondido')}
- ¿Tiene formulario de contacto funcionando?: {respuestas.get('web_formulario', 'No respondido')}
- ¿Tiene sistema de captación de correos?: {respuestas.get('web_captacion', 'No respondido')}

PARTE 2 — ANÁLISIS DE COMPETENCIA:
- Perfil 1 — Nombre: {respuestas.get('comp1_nombre', 'No respondido')}
- Perfil 1 — Por qué lo eligió: {respuestas.get('comp1_razon', 'No respondido')}
- Perfil 1 — Posicionamiento: {respuestas.get('comp1_posicionamiento', 'No respondido')}
- Perfil 1 — Tipo de contenido: {respuestas.get('comp1_contenido', 'No respondido')}
- Perfil 1 — Nivel de interacción: {respuestas.get('comp1_interaccion', 'No respondido')}
- Perfil 1 — Mayor fortaleza: {respuestas.get('comp1_fortaleza', 'No respondido')}
- Perfil 1 — Mayor debilidad: {respuestas.get('comp1_debilidad', 'No respondido')}
- Perfil 1 — Qué ofrece que el participante no: {respuestas.get('comp1_ventaja', 'No respondido')}
- Perfil 1 — Qué puede ofrecer el participante que él no: {respuestas.get('comp1_oportunidad', 'No respondido')}

- Perfil 2 — Nombre: {respuestas.get('comp2_nombre', 'No respondido')}
- Perfil 2 — Por qué lo eligió: {respuestas.get('comp2_razon', 'No respondido')}
- Perfil 2 — Posicionamiento: {respuestas.get('comp2_posicionamiento', 'No respondido')}
- Perfil 2 — Tipo de contenido: {respuestas.get('comp2_contenido', 'No respondido')}
- Perfil 2 — Nivel de interacción: {respuestas.get('comp2_interaccion', 'No respondido')}
- Perfil 2 — Mayor fortaleza: {respuestas.get('comp2_fortaleza', 'No respondido')}
- Perfil 2 — Mayor debilidad: {respuestas.get('comp2_debilidad', 'No respondido')}
- Perfil 2 — Qué ofrece que el participante no: {respuestas.get('comp2_ventaja', 'No respondido')}
- Perfil 2 — Qué puede ofrecer el participante que él no: {respuestas.get('comp2_oportunidad', 'No respondido')}

- Perfil 3 — Nombre: {respuestas.get('comp3_nombre', 'No respondido')}
- Perfil 3 — Por qué lo eligió: {respuestas.get('comp3_razon', 'No respondido')}
- Perfil 3 — Posicionamiento: {respuestas.get('comp3_posicionamiento', 'No respondido')}
- Perfil 3 — Tipo de contenido: {respuestas.get('comp3_contenido', 'No respondido')}
- Perfil 3 — Nivel de interacción: {respuestas.get('comp3_interaccion', 'No respondido')}
- Perfil 3 — Mayor fortaleza: {respuestas.get('comp3_fortaleza', 'No respondido')}
- Perfil 3 — Mayor debilidad: {respuestas.get('comp3_debilidad', 'No respondido')}
- Perfil 3 — Qué ofrece que el participante no: {respuestas.get('comp3_ventaja', 'No respondido')}
- Perfil 3 — Qué puede ofrecer el participante que él no: {respuestas.get('comp3_oportunidad', 'No respondido')}

- Patrón común en los 3 perfiles: {respuestas.get('patron_competencia', 'No respondido')}
- Hueco de mercado identificado: {respuestas.get('hueco_mercado', 'No respondido')}
- Aprendizaje del ejercicio: {respuestas.get('aprendizaje_competencia', 'No respondido')}

PARTE 3 — CLIENTE IDEAL:
- Quién es la persona específica que más se beneficia: {respuestas.get('cliente_ideal', 'No respondido')}
- Problema principal antes de llegar al participante: {respuestas.get('problema_cliente', 'No respondido')}
- Qué ha intentado antes y por qué no funcionó: {respuestas.get('intentos_cliente', 'No respondido')}
- Palabras que usa para describir su problema: {respuestas.get('palabras_cliente', 'No respondido')}
- Resultado específico que busca: {respuestas.get('resultado_buscado', 'No respondido')}
- Dónde está hoy (plataformas, qué consume, a quién sigue): {respuestas.get('donde_esta_cliente', 'No respondido')}
- Por qué no ha resuelto su problema todavía: {respuestas.get('razon_sin_resolver', 'No respondido')}

PARTE 4 — FODA PERSONAL:
- Fortalezas (mínimo 3 con evidencia): {respuestas.get('fortalezas', 'No respondido')}
- Debilidades (mínimo 3 honestas): {respuestas.get('debilidades', 'No respondido')}
- Oportunidades: {respuestas.get('oportunidades', 'No respondido')}
- Amenazas: {respuestas.get('amenazas', 'No respondido')}

PARTE 5 — INERCIAS CRÍTICAS:
- Inercia 1 — Patrón que se repite: {respuestas.get('inercia1_patron', 'No respondido')}
- Inercia 1 — Cómo se manifiesta: {respuestas.get('inercia1_manifestacion', 'No respondido')}
- Inercia 1 — Tiempo con esta inercia: {respuestas.get('inercia1_tiempo', 'No respondido')}
- Inercia 1 — Qué ha intentado para romperla: {respuestas.get('inercia1_intentos', 'No respondido')}

- Inercia 2 — Patrón que se repite: {respuestas.get('inercia2_patron', 'No respondido')}
- Inercia 2 — Cómo se manifiesta: {respuestas.get('inercia2_manifestacion', 'No respondido')}
- Inercia 2 — Tiempo con esta inercia: {respuestas.get('inercia2_tiempo', 'No respondido')}
- Inercia 2 — Qué ha intentado para romperla: {respuestas.get('inercia2_intentos', 'No respondido')}

- Inercia 3 — Patrón que se repite: {respuestas.get('inercia3_patron', 'No respondido')}
- Inercia 3 — Cómo se manifiesta: {respuestas.get('inercia3_manifestacion', 'No respondido')}
- Inercia 3 — Tiempo con esta inercia: {respuestas.get('inercia3_tiempo', 'No respondido')}
- Inercia 3 — Qué ha intentado para romperla: {respuestas.get('inercia3_intentos', 'No respondido')}
"""

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    return response.text


def generate_modulo2_gemini(respuestas: dict, contexto_previo: str) -> str:
    """Genera el Documento Maestro de Marca (Módulo 2) usando Gemini."""
    client = google_genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""Eres el motor de análisis del Programa Anti-Inercia de Marca Personal, creado por Fedor Sawoloka.

Este módulo genera el "Documento Maestro de Marca" — el núcleo estratégico de toda la identidad del participante.

ADVERTENCIA: Este es el módulo más importante del programa. Si el análisis aquí falla, todo lo que viene después falla. No apliques fórmulas genéricas. Trabaja exclusivamente con los datos del participante y el contexto de los módulos anteriores.

METODOLOGÍA GOLD STANDARD — PRINCIPIOS:
1. El Documento Maestro de Marca es el activo más valioso que puede tener un profesional independiente: claridad estratégica documentada.
2. Cada pilar debe estar libre de adjetivos vacíos. "Calidad", "experiencia" y "pasión" no son diferenciadores.
3. El Arquetipo Ganador debe ser tan específico que el cliente ideal, al leerlo, sienta que le están hablando directamente a él.
4. El Enemigo Común no es un competidor. Es una práctica, creencia o forma de hacer las cosas que le hace daño al cliente ideal.

INSTRUCCIONES DE TONO:
- Este documento es el espejo estratégico del participante. Debe sentir que "nadie más podría haber escrito esto sin conocerme a fondo".
- Directo, sin rodeos, sin relleno. Cada párrafo debe justificar su existencia.
- Formato: ## para secciones, ### para pilares.
- NO uses emojis ni símbolos decorativos.

ESTRUCTURA OBLIGATORIA — DOCUMENTO MAESTRO DE MARCA:

## El Problema Raíz
Una sola frase de entre 20 y 50 palabras que identifica el problema central del participante. No una lista. No una queja. Un diagnóstico. Seguido de 1 párrafo que explica por qué es el problema raíz y no un síntoma.

## Los 5 Pilares del ADN de Marca

### Pilar 1 — El Problema que Resuelves
No lo que hace. El problema concreto y específico que elimina. Redactado en las palabras que usa su cliente ideal, no en jerga técnica.

### Pilar 2 — Para Quién Exactamente
No un segmento demográfico. Una persona específica con contexto, situación y momento de urgencia. Tan específico que el cliente ideal se reconozca al leerlo.

### Pilar 3 — El Diferenciador Operativo
Qué hace de forma distinta a los demás en su mercado. Con evidencia o ejemplo concreto que lo respalde. Sin adjetivos vacíos.

### Pilar 4 — El Tono de Voz
La personalidad de su marca en palabras concretas. Cómo suena cuando explica lo que domina. Incluye frases que suenan como él y frases que NO suenan como él.

### Pilar 5 — El Enemigo Común
La práctica, creencia o forma de hacer las cosas en su industria contra la que lucha. Por qué le hace daño concreto a su cliente ideal. Formulado como postura, no como ataque a personas.

## El Arquetipo Ganador
La síntesis de los 5 pilares en una frase estructurada:
"Soy el/la [quién eres] que ayuda a [para quién exactamente] a [qué resultado concreto] a través de [diferenciador operativo], sin [el Enemigo Común que eliminas de la ecuación]."

Seguido de: por qué este arquetipo es el correcto para este participante, basado en el análisis de los módulos anteriores.

## Validación del Documento Maestro
Evaluación honesta de los 4 criterios de validación:
1. ¿Puede explicar su posicionamiento en 30 segundos sin usar palabras vacías?
2. ¿Su cliente ideal se reconocería al leer el Arquetipo Ganador?
3. ¿El Enemigo Común ataca una idea o práctica (no personas)?
4. ¿El diferenciador operativo es demostrable con evidencia?

Para cada criterio: Verde (cumple), Amarillo (necesita ajuste) o Rojo (requiere reescritura), con explicación específica.

## Lo que viene: Módulo 3
Qué construirán en la Propuesta de Estrategia y por qué este Documento Maestro es la base que lo hace posible.

---
Documento generado por el Programa Anti-Inercia de Marca Personal | Metodologia de Fedor Sawoloka | yosoyelruso.com

CONTEXTO DE MÓDULOS ANTERIORES:
{contexto_previo[:4000] if contexto_previo else 'No disponible'}

RESPUESTAS DEL MÓDULO 2:

PARTE 1 — EL PROBLEMA RAÍZ:
- Problema raíz (frase del participante): {respuestas.get('problema_raiz', 'No respondido')}
- Por qué es el problema raíz y no un síntoma: {respuestas.get('raiz_explicacion', 'No respondido')}

PARTE 2 — LOS 5 PILARES:
PILAR 1:
- Problema concreto que resuelve: {respuestas.get('p1_problema', 'No respondido')}
- En palabras del cliente: {respuestas.get('p1_palabras_cliente', 'No respondido')}

PILAR 2:
- Quién es exactamente: {respuestas.get('p2_quien', 'No respondido')}
- Momento de urgencia (detonador): {respuestas.get('p2_urgencia', 'No respondido')}

PILAR 3:
- Qué hace diferente: {respuestas.get('p3_diferente', 'No respondido')}
- Evidencia del diferencial: {respuestas.get('p3_evidencia', 'No respondido')}

PILAR 4:
- Estilo de comunicación natural: {respuestas.get('p4_estilo', 'No respondido')}
- Frases que suenan como él: {respuestas.get('p4_frases_si', 'No respondido')}
- Frases que NO suenan como él: {respuestas.get('p4_frases_no', 'No respondido')}
- Palabras que usa frecuentemente: {respuestas.get('p4_palabras_propias', 'No respondido')}
- Palabras del mercado que rechaza: {respuestas.get('p4_palabras_rechazo', 'No respondido')}

PILAR 5:
- Contra qué práctica o creencia lucha: {respuestas.get('p5_enemigo', 'No respondido')}
- Por qué le hace daño al cliente ideal: {respuestas.get('p5_dano', 'No respondido')}

PARTE 3 — ARQUETIPO GANADOR:
- Arquetipo formulado por el participante: {respuestas.get('arquetipo', 'No respondido')}
- ¿Le representa completamente?: {respuestas.get('arquetipo_validacion', 'No respondido')}
- Qué parte necesita ajuste (si aplica): {respuestas.get('arquetipo_ajuste', 'No respondido')}

PARTE 4 — VALIDACIÓN FINAL:
- ¿Puede explicar sin palabras vacías?: {respuestas.get('val_sin_vacias', 'No respondido')}
- ¿Cliente ideal se reconocería?: {respuestas.get('val_cliente_reconoce', 'No respondido')}
- ¿Enemigo Común ataca idea o persona?: {respuestas.get('val_enemigo', 'No respondido')}
- ¿Diferenciador es demostrable?: {respuestas.get('val_diferenciador', 'No respondido')}
"""

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    return response.text


def generate_modulo3_gemini(respuestas: dict, contexto_previo: str) -> str:
    """Genera la Propuesta de Estrategia (Módulo 3) usando Gemini."""
    client = google_genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""Eres el motor de análisis del Programa Anti-Inercia de Marca Personal, creado por Fedor Sawoloka.

Este módulo genera la "Propuesta de Estrategia" — el Entregable 2 de la Metodología Gold Standard Anti-Inercia.

PRINCIPIO RECTOR: Un diagnóstico sin estrategia es un documento bonito que no le sirve a nadie. Una estrategia sin diagnóstico es humo. Este documento convierte todo lo construido en los módulos anteriores en un plan con objetivos medibles, un sistema de conversión y un calendario de acción.

METODOLOGÍA GOLD STANDARD — PRINCIPIOS PARA ESTE ENTREGABLE:
1. EJECUTABILIDAD: El participante puede implementar esta estrategia sin necesidad de aclaraciones adicionales.
2. COHERENCIA: La estrategia resuelve directamente los problemas e inercias identificados en los módulos anteriores.
3. MÉTRICAS CLARAS: Cada objetivo tiene KPIs medibles y metas específicas.
4. SISTEMA SOBRE TÁCTICAS: No es una lista de tareas. Es un sistema sostenible.
5. ROI COMO MÉTRICA: Los objetivos están enfocados en impacto de negocio, no en vanity metrics.

INSTRUCCIONES DE TONO:
- Estratégico, ejecutable, sin relleno.
- Cada sección debe poder implementarse esta semana, no "en algún momento".
- Formato: ## para secciones, ### para subsecciones.
- NO uses emojis ni símbolos decorativos.

ESTRUCTURA OBLIGATORIA — PROPUESTA DE ESTRATEGIA:

## Introducción y Objetivos
Objetivo general y 3 objetivos específicos con KPIs y metas a 6 meses. Uno de visibilidad, uno de credibilidad, uno de conversión. Cada uno con número, plazo y criterio de éxito. Sin objetivos vagos.

## Los 3 Pilares Estratégicos de Contenido
Basados en el Documento Maestro de Marca del Módulo 2.
### Pilar A — Atracción
Tema central, 3 ejemplos de títulos concretos, formato recomendado. Función: que gente nueva descubra al participante.
### Pilar B — Autoridad
Tema central, 3 ejemplos de títulos concretos, tipo de evidencia a usar. Función: que quien ya lo conoce lo crea capaz.
### Pilar C — Conversión
Tema central, 3 ejemplos de títulos concretos, llamado a la acción principal. Función: que quien ya lo cree dé el siguiente paso.

## Sistema de Conversión
El camino de menor resistencia desde que alguien descubre al participante hasta que se convierte en cliente. Punto de entrada, siguiente paso, cierre. Configuración técnica recomendada según el nivel tecnológico declarado. La fricción principal identificada y cómo eliminarla.

## Plan de Implementación en Fases
### Fase 1 — Preparación (Semanas 1-2)
Acciones específicas para construir la base antes de aparecer en público.
### Fase 2 — Activación (Semanas 3-6)
Frecuencia de publicación, canal principal, cómo verificar que el embudo funciona.
### Fase 3 — Autoridad (Semanas 7-10)
Primera pieza de autoridad profunda, activación del pilar de conversión, primeras métricas a revisar.
### Fase 4 — Escalabilidad (Semanas 11-12)
Qué documentar, qué sistematizar, cómo diseñar el siguiente ciclo de 90 días.

## Las 3 Métricas que Debe Revisar
Explicación paso a paso de cómo acceder a cada métrica en su plataforma principal. Sin asumir conocimiento técnico previo. Calibradas al canal elegido y nivel tecnológico declarado.

## Protocolo de Ajuste — Los 3 Escenarios
Para consultar cuando algo no funcione:
- Escenario A: El alcance no crece — qué revisar y qué cambiar
- Escenario B: El alcance crece pero no llegan contactos — qué revisar y qué cambiar
- Escenario C: Llegan contactos pero no cierra — qué revisar y qué cambiar

## Propuesta de Colaboración
Cómo la Asesoría Estratégica Continua con Fedor Sawoloka puede acelerar la implementación de esta estrategia. Qué incluye, qué resuelve y cómo dar el siguiente paso.

---
Documento generado por el Programa Anti-Inercia de Marca Personal | Metodologia de Fedor Sawoloka | yosoyelruso.com

CONTEXTO DE MÓDULOS ANTERIORES:
{contexto_previo[:5000] if contexto_previo else 'No disponible'}

RESPUESTAS DEL MÓDULO 3:

PARTE 1 — OBJETIVOS Y KPIs:
- Objetivo de visibilidad: {respuestas.get('obj_visibilidad', 'No respondido')}
- Cómo medir el avance semana a semana: {respuestas.get('obj_visibilidad_medicion', 'No respondido')}
- Objetivo de credibilidad: {respuestas.get('obj_credibilidad', 'No respondido')}
- Evidencia concreta a producir: {respuestas.get('obj_credibilidad_evidencia', 'No respondido')}
- Objetivo de conversión: {respuestas.get('obj_conversion', 'No respondido')}
- Qué debe pasar en el embudo para alcanzarlo: {respuestas.get('obj_conversion_embudo', 'No respondido')}

PARTE 2 — PILARES DE CONTENIDO:
- Pilar Atracción — tema central: {respuestas.get('pilar_a_tema', 'No respondido')}
- Pilar Atracción — 3 ejemplos de títulos: {respuestas.get('pilar_a_titulos', 'No respondido')}
- Pilar Atracción — formato: {respuestas.get('pilar_a_formato', 'No respondido')}
- Pilar Autoridad — tema central: {respuestas.get('pilar_b_tema', 'No respondido')}
- Pilar Autoridad — 3 ejemplos de títulos: {respuestas.get('pilar_b_titulos', 'No respondido')}
- Pilar Autoridad — tipo de evidencia: {respuestas.get('pilar_b_evidencia', 'No respondido')}
- Pilar Conversión — tema central: {respuestas.get('pilar_c_tema', 'No respondido')}
- Pilar Conversión — 3 ejemplos de títulos: {respuestas.get('pilar_c_titulos', 'No respondido')}
- Pilar Conversión — llamado a la acción: {respuestas.get('pilar_c_cta', 'No respondido')}

PARTE 3 — SISTEMA DE CONVERSIÓN:
- Punto de entrada principal: {respuestas.get('embudo_entrada', 'No respondido')}
- Siguiente paso después de descubrirlo: {respuestas.get('embudo_siguiente', 'No respondido')}
- Paso final antes de convertirse en cliente: {respuestas.get('embudo_cierre', 'No respondido')}
- Descripción del embudo completo: {respuestas.get('embudo_descripcion', 'No respondido')}
- Dónde se pierde la mayoría de la gente: {respuestas.get('embudo_friccion', 'No respondido')}
- Cómo eliminar esa fricción: {respuestas.get('embudo_solucion', 'No respondido')}
- Nivel tecnológico actual: {respuestas.get('nivel_tecnologico', 'No respondido')}
- Herramientas ya configuradas: {respuestas.get('herramientas_actuales', 'No respondido')}
- Primera herramienta a configurar en 15 días: {respuestas.get('herramienta_proxima', 'No respondido')}

PARTE 4 — PLAN DE IMPLEMENTACIÓN:
- Mayor obstáculo para la Fase 1: {respuestas.get('fase1_obstaculo', 'No respondido')}
- Qué necesita para arrancar sin fricción: {respuestas.get('fase1_necesita', 'No respondido')}
- Frecuencia de publicación en Fase 2: {respuestas.get('fase2_frecuencia', 'No respondido')}
- Canal principal en Fase 2: {respuestas.get('fase2_canal', 'No respondido')}
- Pieza de autoridad en mente para Fase 3: {respuestas.get('fase3_pieza', 'No respondido')}

PARTE 5 — VALIDACIÓN ESTRATÉGICA:
- ¿Objetivos alcanzables con recursos actuales?: {respuestas.get('val_objetivos', 'No respondido')}
- ¿El embudo es operable hoy?: {respuestas.get('val_embudo', 'No respondido')}
- ¿La Fase 1 puede arrancar esta semana?: {respuestas.get('val_fase1', 'No respondido')}
- Qué bloquea el arranque (si aplica): {respuestas.get('val_bloqueo', 'No respondido')}
"""

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    return response.text


# ============================================================
# ENDPOINTS — Módulos 1, 2 y 3
# ============================================================

@app.post("/programa/modulo1/generar")
async def generar_modulo1(
    email: str = Form(...),
    # Parte 1 — Auditoría de perfil principal
    foto_perfil: str = Form(...),
    bio_actual: str = Form(...),
    claridad_bio: str = Form(...),
    cta_perfil: str = Form(...),
    patron_publicaciones: str = Form(...),
    nivel_interaccion: str = Form(...),
    # Canales secundarios (JSON string)
    canales_secundarios: str = Form(default=""),
    # Auditoría web
    tiene_web: str = Form(...),
    web_primeros_segundos: str = Form(default=""),
    web_formulario: str = Form(default=""),
    web_captacion: str = Form(default=""),
    # Parte 2 — Competencia (3 perfiles)
    comp1_nombre: str = Form(...),
    comp1_razon: str = Form(...),
    comp1_posicionamiento: str = Form(...),
    comp1_contenido: str = Form(...),
    comp1_interaccion: str = Form(...),
    comp1_fortaleza: str = Form(...),
    comp1_debilidad: str = Form(...),
    comp1_ventaja: str = Form(...),
    comp1_oportunidad: str = Form(...),
    comp2_nombre: str = Form(...),
    comp2_razon: str = Form(...),
    comp2_posicionamiento: str = Form(...),
    comp2_contenido: str = Form(...),
    comp2_interaccion: str = Form(...),
    comp2_fortaleza: str = Form(...),
    comp2_debilidad: str = Form(...),
    comp2_ventaja: str = Form(...),
    comp2_oportunidad: str = Form(...),
    comp3_nombre: str = Form(...),
    comp3_razon: str = Form(...),
    comp3_posicionamiento: str = Form(...),
    comp3_contenido: str = Form(...),
    comp3_interaccion: str = Form(...),
    comp3_fortaleza: str = Form(...),
    comp3_debilidad: str = Form(...),
    comp3_ventaja: str = Form(...),
    comp3_oportunidad: str = Form(...),
    patron_competencia: str = Form(...),
    hueco_mercado: str = Form(...),
    aprendizaje_competencia: str = Form(...),
    # Parte 3 — Cliente ideal
    cliente_ideal: str = Form(...),
    problema_cliente: str = Form(...),
    intentos_cliente: str = Form(...),
    palabras_cliente: str = Form(...),
    resultado_buscado: str = Form(...),
    donde_esta_cliente: str = Form(...),
    razon_sin_resolver: str = Form(...),
    # Parte 4 — FODA
    fortalezas: str = Form(...),
    debilidades: str = Form(...),
    oportunidades: str = Form(...),
    amenazas: str = Form(...),
    # Parte 5 — Inercias
    inercia1_patron: str = Form(...),
    inercia1_manifestacion: str = Form(...),
    inercia1_tiempo: str = Form(...),
    inercia1_intentos: str = Form(...),
    inercia2_patron: str = Form(...),
    inercia2_manifestacion: str = Form(...),
    inercia2_tiempo: str = Form(...),
    inercia2_intentos: str = Form(...),
    inercia3_patron: str = Form(...),
    inercia3_manifestacion: str = Form(...),
    inercia3_tiempo: str = Form(...),
    inercia3_intentos: str = Form(...),
    # PDF del módulo anterior
    pdf_m0: UploadFile = File(...)
):
    if not check_email_access(email):
        raise HTTPException(status_code=403, detail="Este correo no tiene acceso al programa.")

    contexto_m0 = extract_text_from_pdf(await pdf_m0.read())

    respuestas = {k: v for k, v in locals().items() if k not in ['email', 'pdf_m0', 'contexto_m0']}

    try:
        documento = generate_modulo1_gemini(respuestas, contexto_m0)
    except Exception as e:
        print(f"Gemini falló en Módulo 1: {e}")
        raise HTTPException(status_code=500, detail="Error generando el documento. Intenta de nuevo en unos minutos.")

    try:
        save_programa_lead(email, 1, {"perfiles_analizados": f"{comp1_nombre}, {comp2_nombre}, {comp3_nombre}"})
    except Exception as e:
        print(f"Error registrando actividad M1: {e}")

    try:
        pdf_bytes = generate_pdf_branded(
            content=documento,
            titulo="Documento de Auditoria y Diagnostico — Modulo 1",
            subtitulo="Programa Anti-Inercia de Marca Personal  |  yosoyelruso.com",
            nombre_archivo="auditoria-diagnostico-modulo-1.pdf"
        )
        pdf_buffer = io.BytesIO(pdf_bytes)
        return StreamingResponse(
            pdf_buffer,
            media_type='application/pdf',
            headers={
                'Content-Disposition': 'attachment; filename="auditoria-diagnostico-modulo-1.pdf"',
                'Content-Type': 'application/pdf'
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'Error generando PDF: {str(e)}')


@app.post("/programa/modulo2/generar")
async def generar_modulo2(
    email: str = Form(...),
    # Parte 1 — Problema raíz
    problema_raiz: str = Form(...),
    raiz_explicacion: str = Form(...),
    # Parte 2 — 5 Pilares
    p1_problema: str = Form(...),
    p1_palabras_cliente: str = Form(...),
    p2_quien: str = Form(...),
    p2_urgencia: str = Form(...),
    p3_diferente: str = Form(...),
    p3_evidencia: str = Form(...),
    p4_estilo: str = Form(...),
    p4_frases_si: str = Form(...),
    p4_frases_no: str = Form(...),
    p4_palabras_propias: str = Form(...),
    p4_palabras_rechazo: str = Form(...),
    p5_enemigo: str = Form(...),
    p5_dano: str = Form(...),
    # Parte 3 — Arquetipo
    arquetipo: str = Form(...),
    arquetipo_validacion: str = Form(...),
    arquetipo_ajuste: str = Form(default=""),
    # Parte 4 — Validación
    val_sin_vacias: str = Form(...),
    val_cliente_reconoce: str = Form(...),
    val_enemigo: str = Form(...),
    val_diferenciador: str = Form(...),
    # PDFs anteriores
    pdf_m0: UploadFile = File(...),
    pdf_m1: UploadFile = File(...)
):
    if not check_email_access(email):
        raise HTTPException(status_code=403, detail="Este correo no tiene acceso al programa.")

    texto_m0 = extract_text_from_pdf(await pdf_m0.read())
    texto_m1 = extract_text_from_pdf(await pdf_m1.read())
    contexto_previo = f"=== MÓDULO 0 ===\n{texto_m0}\n\n=== MÓDULO 1 ===\n{texto_m1}"

    respuestas = {k: v for k, v in locals().items() if k not in ['email', 'pdf_m0', 'pdf_m1', 'contexto_previo', 'texto_m0', 'texto_m1']}

    try:
        documento = generate_modulo2_gemini(respuestas, contexto_previo)
    except Exception as e:
        print(f"Gemini falló en Módulo 2: {e}")
        raise HTTPException(status_code=500, detail="Error generando el documento. Intenta de nuevo en unos minutos.")

    try:
        save_programa_lead(email, 2, {"arquetipo": arquetipo[:100]})
    except Exception as e:
        print(f"Error registrando actividad M2: {e}")

    try:
        pdf_bytes = generate_pdf_branded(
            content=documento,
            titulo="Documento Maestro de Marca — Modulo 2",
            subtitulo="Programa Anti-Inercia de Marca Personal  |  yosoyelruso.com",
            nombre_archivo="documento-maestro-marca-modulo-2.pdf"
        )
        pdf_buffer = io.BytesIO(pdf_bytes)
        return StreamingResponse(
            pdf_buffer,
            media_type='application/pdf',
            headers={
                'Content-Disposition': 'attachment; filename="documento-maestro-marca-modulo-2.pdf"',
                'Content-Type': 'application/pdf'
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'Error generando PDF: {str(e)}')


@app.post("/programa/modulo3/generar")
async def generar_modulo3(
    email: str = Form(...),
    # Parte 1 — Objetivos
    obj_visibilidad: str = Form(...),
    obj_visibilidad_medicion: str = Form(...),
    obj_credibilidad: str = Form(...),
    obj_credibilidad_evidencia: str = Form(...),
    obj_conversion: str = Form(...),
    obj_conversion_embudo: str = Form(...),
    # Parte 2 — Pilares de contenido
    pilar_a_tema: str = Form(...),
    pilar_a_titulos: str = Form(...),
    pilar_a_formato: str = Form(...),
    pilar_b_tema: str = Form(...),
    pilar_b_titulos: str = Form(...),
    pilar_b_evidencia: str = Form(...),
    pilar_c_tema: str = Form(...),
    pilar_c_titulos: str = Form(...),
    pilar_c_cta: str = Form(...),
    # Parte 3 — Sistema de conversión
    embudo_entrada: str = Form(...),
    embudo_siguiente: str = Form(...),
    embudo_cierre: str = Form(...),
    embudo_descripcion: str = Form(...),
    embudo_friccion: str = Form(...),
    embudo_solucion: str = Form(...),
    nivel_tecnologico: str = Form(...),
    herramientas_actuales: str = Form(...),
    herramienta_proxima: str = Form(...),
    # Parte 4 — Plan de implementación
    fase1_obstaculo: str = Form(...),
    fase1_necesita: str = Form(...),
    fase2_frecuencia: str = Form(...),
    fase2_canal: str = Form(...),
    fase3_pieza: str = Form(...),
    # Parte 5 — Validación
    val_objetivos: str = Form(...),
    val_embudo: str = Form(...),
    val_fase1: str = Form(...),
    val_bloqueo: str = Form(default=""),
    # PDFs anteriores
    pdf_m0: UploadFile = File(...),
    pdf_m1: UploadFile = File(...),
    pdf_m2: UploadFile = File(...)
):
    if not check_email_access(email):
        raise HTTPException(status_code=403, detail="Este correo no tiene acceso al programa.")

    texto_m0 = extract_text_from_pdf(await pdf_m0.read())
    texto_m1 = extract_text_from_pdf(await pdf_m1.read())
    texto_m2 = extract_text_from_pdf(await pdf_m2.read())
    contexto_previo = f"=== MÓDULO 0 ===\n{texto_m0}\n\n=== MÓDULO 1 ===\n{texto_m1}\n\n=== MÓDULO 2 ===\n{texto_m2}"

    respuestas = {k: v for k, v in locals().items() if k not in ['email', 'pdf_m0', 'pdf_m1', 'pdf_m2', 'contexto_previo', 'texto_m0', 'texto_m1', 'texto_m2']}

    try:
        documento = generate_modulo3_gemini(respuestas, contexto_previo)
    except Exception as e:
        print(f"Gemini falló en Módulo 3: {e}")
        raise HTTPException(status_code=500, detail="Error generando el documento. Intenta de nuevo en unos minutos.")

    try:
        save_programa_lead(email, 3, {"canal_principal": fase2_canal, "frecuencia": fase2_frecuencia})
    except Exception as e:
        print(f"Error registrando actividad M3: {e}")

    try:
        pdf_bytes = generate_pdf_branded(
            content=documento,
            titulo="Propuesta de Estrategia — Modulo 3",
            subtitulo="Programa Anti-Inercia de Marca Personal  |  yosoyelruso.com",
            nombre_archivo="propuesta-estrategia-modulo-3.pdf"
        )
        pdf_buffer = io.BytesIO(pdf_bytes)
        return StreamingResponse(
            pdf_buffer,
            media_type='application/pdf',
            headers={
                'Content-Disposition': 'attachment; filename="propuesta-estrategia-modulo-3.pdf"',
                'Content-Type': 'application/pdf'
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'Error generando PDF: {str(e)}')


# ============================================================
# ENDPOINTS ASÍNCRONOS — Sistema Job Queue para el Programa
# ============================================================
# Nuevo flujo:
# 1. POST /programa/moduloN/iniciar → recibe datos, lanza tarea en background, devuelve job_id
# 2. GET  /programa/job/{job_id}    → devuelve estado del trabajo
# 3. GET  /programa/job/{job_id}/pdf → descarga el PDF cuando está listo
# Esto elimina timeouts independientemente de cuánto tarde Gemini.

@app.get("/programa/job/{job_id}")
def consultar_job(job_id: str):
    """Consulta el estado de un trabajo de generación."""
    limpiar_jobs_viejos()
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Trabajo no encontrado o expirado.")
    return {
        "job_id": job_id,
        "status": job["status"],
        "filename": job.get("filename"),
        "error": job.get("error")
    }


@app.get("/programa/job/{job_id}/pdf")
def descargar_pdf_job(job_id: str):
    """Descarga el PDF de un trabajo completado."""
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Trabajo no encontrado o expirado.")
    if job["status"] != "done":
        raise HTTPException(status_code=400, detail=f"El trabajo aún no está listo. Estado: {job['status']}")
    pdf_bytes = job["pdf_bytes"]
    filename = job.get("filename", "documento.pdf")
    # Limpiar el job después de la descarga
    with jobs_lock:
        if job_id in jobs:
            del jobs[job_id]
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type='application/pdf',
        headers={
            'Content-Disposition': f'attachment; filename="{filename}"',
            'Content-Type': 'application/pdf'
        }
    )


# ── Módulo 0 asíncrono ──

def _procesar_modulo0(job_id: str, respuestas: dict, email: str, disposicion: str, horas_semanales: str):
    try:
        documento = generate_modulo0_gemini(respuestas)
        pdf_bytes = generate_pdf_branded(
            content=documento,
            titulo="Mapa de Fricciones — Modulo 0",
            subtitulo="Programa Anti-Inercia de Marca Personal  |  yosoyelruso.com",
            nombre_archivo="mapa-de-fricciones-modulo-0.pdf"
        )
        completar_job(job_id, pdf_bytes, "mapa-de-fricciones-modulo-0.pdf")
        try:
            save_programa_lead(email, 0, {"disposicion": disposicion, "horas": horas_semanales})
        except Exception as e:
            print(f"Error registrando actividad M0: {e}")
    except Exception as e:
        print(f"Error procesando M0 job {job_id}: {e}")
        fallar_job(job_id, str(e))


@app.post("/programa/modulo0/iniciar")
async def iniciar_modulo0(
    background_tasks: BackgroundTasks,
    email: str = Form(...),
    dedicacion: str = Form(...),
    tiempo_independiente: str = Form(...),
    fuente_clientes: str = Form(...),
    clientes_activos: str = Form(...),
    flujo_clientes: str = Form(...),
    servicios_definidos: str = Form(...),
    facilidad_explicar: str = Form(...),
    diferenciador: str = Form(...),
    resultado_cliente: str = Form(...),
    plataformas: str = Form(...),
    frecuencia_publicacion: str = Form(...),
    google_resultado: str = Form(...),
    perfil_actual: str = Form(...),
    razon_principal: str = Form(...),
    razon_ampliacion: str = Form(default=""),
    intento_previo: str = Form(...),
    que_fallo: str = Form(default=""),
    disposicion: str = Form(...),
    exito_definido: str = Form(...),
    horas_semanales: str = Form(...),
    nivel_digital: str = Form(...),
    presupuesto: str = Form(...)
):
    if not check_email_access(email):
        raise HTTPException(status_code=403, detail="Este correo no tiene acceso al programa.")

    job_id = str(uuid.uuid4())
    crear_job(job_id)

    respuestas = {
        "dedicacion": dedicacion, "tiempo_independiente": tiempo_independiente,
        "fuente_clientes": fuente_clientes, "clientes_activos": clientes_activos,
        "flujo_clientes": flujo_clientes, "servicios_definidos": servicios_definidos,
        "facilidad_explicar": facilidad_explicar, "diferenciador": diferenciador,
        "resultado_cliente": resultado_cliente, "plataformas": plataformas,
        "frecuencia_publicacion": frecuencia_publicacion, "google_resultado": google_resultado,
        "perfil_actual": perfil_actual, "razon_principal": razon_principal,
        "razon_ampliacion": razon_ampliacion, "intento_previo": intento_previo,
        "que_fallo": que_fallo, "disposicion": disposicion, "exito_definido": exito_definido,
        "horas_semanales": horas_semanales, "nivel_digital": nivel_digital, "presupuesto": presupuesto
    }

    background_tasks.add_task(_procesar_modulo0, job_id, respuestas, email, disposicion, horas_semanales)
    return {"job_id": job_id, "status": "processing"}


# ── Módulo 1 asíncrono ──

def _procesar_modulo1(job_id: str, respuestas: dict, contexto_m0: str, email: str, perfiles: str):
    try:
        documento = generate_modulo1_gemini(respuestas, contexto_m0)
        pdf_bytes = generate_pdf_branded(
            content=documento,
            titulo="Documento de Auditoria y Diagnostico — Modulo 1",
            subtitulo="Programa Anti-Inercia de Marca Personal  |  yosoyelruso.com",
            nombre_archivo="auditoria-diagnostico-modulo-1.pdf"
        )
        completar_job(job_id, pdf_bytes, "auditoria-diagnostico-modulo-1.pdf")
        try:
            save_programa_lead(email, 1, {"perfiles_analizados": perfiles})
        except Exception as e:
            print(f"Error registrando actividad M1: {e}")
    except Exception as e:
        print(f"Error procesando M1 job {job_id}: {e}")
        fallar_job(job_id, str(e))


@app.post("/programa/modulo1/iniciar")
async def iniciar_modulo1(
    background_tasks: BackgroundTasks,
    email: str = Form(...),
    foto_perfil: str = Form(...),
    bio_actual: str = Form(...),
    claridad_bio: str = Form(...),
    cta_perfil: str = Form(...),
    patron_publicaciones: str = Form(...),
    nivel_interaccion: str = Form(...),
    canales_secundarios: str = Form(default=""),
    tiene_web: str = Form(...),
    web_primeros_segundos: str = Form(default=""),
    web_formulario: str = Form(default=""),
    web_captacion: str = Form(default=""),
    comp1_nombre: str = Form(...), comp1_razon: str = Form(...),
    comp1_posicionamiento: str = Form(...), comp1_contenido: str = Form(...),
    comp1_interaccion: str = Form(...), comp1_fortaleza: str = Form(...),
    comp1_debilidad: str = Form(...), comp1_ventaja: str = Form(...),
    comp1_oportunidad: str = Form(...),
    comp2_nombre: str = Form(...), comp2_razon: str = Form(...),
    comp2_posicionamiento: str = Form(...), comp2_contenido: str = Form(...),
    comp2_interaccion: str = Form(...), comp2_fortaleza: str = Form(...),
    comp2_debilidad: str = Form(...), comp2_ventaja: str = Form(...),
    comp2_oportunidad: str = Form(...),
    comp3_nombre: str = Form(...), comp3_razon: str = Form(...),
    comp3_posicionamiento: str = Form(...), comp3_contenido: str = Form(...),
    comp3_interaccion: str = Form(...), comp3_fortaleza: str = Form(...),
    comp3_debilidad: str = Form(...), comp3_ventaja: str = Form(...),
    comp3_oportunidad: str = Form(...),
    patron_competencia: str = Form(...), hueco_mercado: str = Form(...),
    aprendizaje_competencia: str = Form(...),
    cliente_ideal: str = Form(...), problema_cliente: str = Form(...),
    intentos_cliente: str = Form(...), palabras_cliente: str = Form(...),
    resultado_buscado: str = Form(...), donde_esta_cliente: str = Form(...),
    razon_sin_resolver: str = Form(...),
    fortalezas: str = Form(...), debilidades: str = Form(...),
    oportunidades: str = Form(...), amenazas: str = Form(...),
    inercia1_patron: str = Form(...), inercia1_manifestacion: str = Form(...),
    inercia1_tiempo: str = Form(...), inercia1_intentos: str = Form(...),
    inercia2_patron: str = Form(...), inercia2_manifestacion: str = Form(...),
    inercia2_tiempo: str = Form(...), inercia2_intentos: str = Form(...),
    inercia3_patron: str = Form(...), inercia3_manifestacion: str = Form(...),
    inercia3_tiempo: str = Form(...), inercia3_intentos: str = Form(...),
    pdf_m0: UploadFile = File(...)
):
    if not check_email_access(email):
        raise HTTPException(status_code=403, detail="Este correo no tiene acceso al programa.")

    contexto_m0 = extract_text_from_pdf(await pdf_m0.read())
    job_id = str(uuid.uuid4())
    crear_job(job_id)

    respuestas = {k: v for k, v in locals().items()
                  if k not in ['email', 'pdf_m0', 'contexto_m0', 'background_tasks', 'job_id']}

    background_tasks.add_task(_procesar_modulo1, job_id, respuestas, contexto_m0, email,
                               f"{comp1_nombre}, {comp2_nombre}, {comp3_nombre}")
    return {"job_id": job_id, "status": "processing"}


# ── Módulo 2 asíncrono ──

def _procesar_modulo2(job_id: str, respuestas: dict, contexto_previo: str, email: str, arquetipo_corto: str):
    try:
        documento = generate_modulo2_gemini(respuestas, contexto_previo)
        pdf_bytes = generate_pdf_branded(
            content=documento,
            titulo="Documento Maestro de Marca — Modulo 2",
            subtitulo="Programa Anti-Inercia de Marca Personal  |  yosoyelruso.com",
            nombre_archivo="documento-maestro-marca-modulo-2.pdf"
        )
        completar_job(job_id, pdf_bytes, "documento-maestro-marca-modulo-2.pdf")
        try:
            save_programa_lead(email, 2, {"arquetipo": arquetipo_corto})
        except Exception as e:
            print(f"Error registrando actividad M2: {e}")
    except Exception as e:
        print(f"Error procesando M2 job {job_id}: {e}")
        fallar_job(job_id, str(e))


@app.post("/programa/modulo2/iniciar")
async def iniciar_modulo2(
    background_tasks: BackgroundTasks,
    email: str = Form(...),
    problema_raiz: str = Form(...), raiz_explicacion: str = Form(...),
    p1_problema: str = Form(...), p1_palabras_cliente: str = Form(...),
    p2_quien: str = Form(...), p2_urgencia: str = Form(...),
    p3_diferente: str = Form(...), p3_evidencia: str = Form(...),
    p4_estilo: str = Form(...), p4_frases_si: str = Form(...),
    p4_frases_no: str = Form(...), p4_palabras_propias: str = Form(...),
    p4_palabras_rechazo: str = Form(...),
    p5_enemigo: str = Form(...), p5_dano: str = Form(...),
    arquetipo: str = Form(...), arquetipo_validacion: str = Form(...),
    arquetipo_ajuste: str = Form(default=""),
    val_sin_vacias: str = Form(...), val_cliente_reconoce: str = Form(...),
    val_enemigo: str = Form(...), val_diferenciador: str = Form(...),
    pdf_m0: UploadFile = File(...),
    pdf_m1: UploadFile = File(...)
):
    if not check_email_access(email):
        raise HTTPException(status_code=403, detail="Este correo no tiene acceso al programa.")

    texto_m0 = extract_text_from_pdf(await pdf_m0.read())
    texto_m1 = extract_text_from_pdf(await pdf_m1.read())
    contexto_previo = f"=== MÓDULO 0 ===\n{texto_m0}\n\n=== MÓDULO 1 ===\n{texto_m1}"

    job_id = str(uuid.uuid4())
    crear_job(job_id)

    respuestas = {k: v for k, v in locals().items()
                  if k not in ['email', 'pdf_m0', 'pdf_m1', 'contexto_previo', 'texto_m0', 'texto_m1', 'background_tasks', 'job_id']}

    background_tasks.add_task(_procesar_modulo2, job_id, respuestas, contexto_previo, email, arquetipo[:100])
    return {"job_id": job_id, "status": "processing"}


# ── Módulo 3 asíncrono ──

def _procesar_modulo3(job_id: str, respuestas: dict, contexto_previo: str, email: str, canal: str, frecuencia: str):
    try:
        documento = generate_modulo3_gemini(respuestas, contexto_previo)
        pdf_bytes = generate_pdf_branded(
            content=documento,
            titulo="Propuesta de Estrategia — Modulo 3",
            subtitulo="Programa Anti-Inercia de Marca Personal  |  yosoyelruso.com",
            nombre_archivo="propuesta-estrategia-modulo-3.pdf"
        )
        completar_job(job_id, pdf_bytes, "propuesta-estrategia-modulo-3.pdf")
        try:
            save_programa_lead(email, 3, {"canal_principal": canal, "frecuencia": frecuencia})
        except Exception as e:
            print(f"Error registrando actividad M3: {e}")
    except Exception as e:
        print(f"Error procesando M3 job {job_id}: {e}")
        fallar_job(job_id, str(e))


@app.post("/programa/modulo3/iniciar")
async def iniciar_modulo3(
    background_tasks: BackgroundTasks,
    email: str = Form(...),
    obj_visibilidad: str = Form(...), obj_visibilidad_medicion: str = Form(...),
    obj_credibilidad: str = Form(...), obj_credibilidad_evidencia: str = Form(...),
    obj_conversion: str = Form(...), obj_conversion_embudo: str = Form(...),
    pilar_a_tema: str = Form(...), pilar_a_titulos: str = Form(...), pilar_a_formato: str = Form(...),
    pilar_b_tema: str = Form(...), pilar_b_titulos: str = Form(...), pilar_b_evidencia: str = Form(...),
    pilar_c_tema: str = Form(...), pilar_c_titulos: str = Form(...), pilar_c_cta: str = Form(...),
    embudo_entrada: str = Form(...), embudo_siguiente: str = Form(...), embudo_cierre: str = Form(...),
    embudo_descripcion: str = Form(...), embudo_friccion: str = Form(...), embudo_solucion: str = Form(...),
    nivel_tecnologico: str = Form(...), herramientas_actuales: str = Form(...),
    herramienta_proxima: str = Form(...),
    fase1_obstaculo: str = Form(...), fase1_necesita: str = Form(...),
    fase2_frecuencia: str = Form(...), fase2_canal: str = Form(...), fase3_pieza: str = Form(...),
    val_objetivos: str = Form(...), val_embudo: str = Form(...),
    val_fase1: str = Form(...), val_bloqueo: str = Form(default=""),
    pdf_m0: UploadFile = File(...),
    pdf_m1: UploadFile = File(...),
    pdf_m2: UploadFile = File(...)
):
    if not check_email_access(email):
        raise HTTPException(status_code=403, detail="Este correo no tiene acceso al programa.")

    texto_m0 = extract_text_from_pdf(await pdf_m0.read())
    texto_m1 = extract_text_from_pdf(await pdf_m1.read())
    texto_m2 = extract_text_from_pdf(await pdf_m2.read())
    contexto_previo = f"=== MÓDULO 0 ===\n{texto_m0}\n\n=== MÓDULO 1 ===\n{texto_m1}\n\n=== MÓDULO 2 ===\n{texto_m2}"

    job_id = str(uuid.uuid4())
    crear_job(job_id)

    respuestas = {k: v for k, v in locals().items()
                  if k not in ['email', 'pdf_m0', 'pdf_m1', 'pdf_m2', 'contexto_previo',
                                'texto_m0', 'texto_m1', 'texto_m2', 'background_tasks', 'job_id']}

    background_tasks.add_task(_procesar_modulo3, job_id, respuestas, contexto_previo, email, fase2_canal, fase2_frecuencia)
    return {"job_id": job_id, "status": "processing"}


# ============================================================
# FUNCIONES GENERADORAS — Módulos 4 y 5
# ============================================================

def generate_modulo4_gemini(respuestas: dict, contexto_previo: str) -> str:
    """Genera el Sistema Operativo de Contenido (Módulo 4) usando Gemini."""
    client = google_genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""Eres el motor de análisis del Programa Anti-Inercia de Marca Personal, creado por Fedor Sawoloka.

Este módulo genera el "Sistema Operativo de Contenido" — el sistema que hace que el contenido ocurra independientemente de cómo amaneciste.

PRINCIPIO RECTOR: La inspiración es para los artistas. Los profesionales que construyen marca con sistema saben exactamente qué publicar, en qué formato, en qué canal y con qué propósito cada semana del año. Este módulo no enseña a crear contenido. Instala el sistema.

METODOLOGÍA GOLD STANDARD:
1. Sistema sobre tácticas: No se entrega un calendario bonito. Se entrega un sistema sostenible.
2. Ejecutabilidad: Cada elemento debe poder implementarse esta semana, no "en algún momento".
3. Coherencia: El sistema de contenido debe resolver directamente las inercias identificadas en módulos anteriores.
4. Análisis sincero: El documento debe reflejar la realidad del participante, no un ideal inalcanzable.

INSTRUCCIONES DE TONO:
- Directo, práctico, sin relleno.
- Si las plantillas de arranque están incompletas, señalarlo con claridad pero sin juzgar.
- Formato: ## para secciones, ### para subsecciones.
- NO uses emojis ni símbolos decorativos.

ESTRUCTURA OBLIGATORIA — SISTEMA OPERATIVO DE CONTENIDO:

## Arquitectura de Contenido Semanal
Análisis de la estructura de publicación declarada por el participante. ¿Es sostenible? ¿La distribución de pilares tiene lógica estratégica? Recomendaciones específicas basadas en su contexto real.

## Banco de Ideas y Hooks
Evaluación de los 6 hooks generados. ¿Activan realmente el dolor del cliente ideal? ¿Cuáles son los más potentes y por qué? Análisis de las 12 ideas del banco con evaluación de potencial de tracción.

## Guía de Contenido Completa
Esta sección es el activo más valioso del documento. Reproduce TEXTUALMENTE y de forma COMPLETA cada una de las 9 ideas del banco de contenido tal como las escribió el participante. NO resumas, NO parafrasees, NO omitas ningún elemento. El participante invirtió tiempo y creatividad en desarrollarlas y este documento es su guía de ejecución.

Para cada idea usa exactamente esta estructura:

### [Pilar] — Idea [N]: [Título o tema de la idea]
**Hook:** [Texto exacto del hook]
**Desarrollo / Contenido:** [Texto exacto del desarrollo]
**Cierre / CTA:** [Texto exacto del cierre]
**Formato:** [Formato declarado]

Organiza las 9 ideas en 3 grupos:
- Pilar Atracción: Ideas 1 a 3 (o las que correspondan)
- Pilar Autoridad: Ideas siguientes
- Pilar Conversión: Ideas restantes

Si alguna idea tiene información incompleta (falta hook, desarrollo o cierre), indícalo con una nota breve al final de esa idea: "Nota: [elemento faltante] pendiente de completar."

## Protocolo de Reciclaje
Análisis de la idea elegida y los 5 formatos. ¿La secuencia propuesta tiene lógica? Recomendaciones para maximizar el alcance de cada formato.

## Las 3 Plantillas de Arranque
Evaluación de las 3 plantillas completadas por el participante. Para cada una:
- Qué funciona bien
- Qué mejorar antes de publicar
- Una sugerencia concreta de ajuste

Si alguna plantilla está incompleta: señalarlo directamente y explicar por qué es el único criterio de completitud de este módulo.

## Protocolo de Contingencia
Qué hacer cuando una semana no hay tiempo para producir contenido nuevo. Plan concreto basado en el banco de ideas ya construido.

## Validación del Sistema
Evaluación honesta: ¿Este sistema es sostenible para este participante específico con sus recursos declarados? ¿Qué ajustar antes de activar?

## Lo que viene: Módulo 5
Qué construirán en el Plan de Arranque y por qué este Sistema Operativo es la base que lo hace posible.

---
Documento generado por el Programa Anti-Inercia de Marca Personal | Metodologia de Fedor Sawoloka | yosoyelruso.com

CONTEXTO DE MÓDULOS ANTERIORES:
{contexto_previo[:5000] if contexto_previo else 'No disponible'}

RESPUESTAS DEL MÓDULO 4:

PARTE 1 — ARQUITECTURA SEMANAL:
- Días de publicación por semana: {respuestas.get('dias_publicacion', 'No respondido')}
- Distribución de pilares por día: {respuestas.get('distribucion_pilares', 'No respondido')}
- Razón de la distribución: {respuestas.get('razon_distribucion', 'No respondido')}
- ¿Tiene día fijo de producción?: {respuestas.get('dia_produccion_tipo', 'No respondido')}
- Día de producción (si aplica): {respuestas.get('dia_produccion', 'No respondido')}

PARTE 2 — BANCO DE IDEAS Y HOOKS:
- Dolor 1: {respuestas.get('dolor1', 'No respondido')}
- Dolor 2: {respuestas.get('dolor2', 'No respondido')}
- Dolor 3: {respuestas.get('dolor3', 'No respondido')}
- Hook D1-A: {respuestas.get('hook_d1a', 'No respondido')}
- Hook D1-B: {respuestas.get('hook_d1b', 'No respondido')}
- Hook D2-A: {respuestas.get('hook_d2a', 'No respondido')}
- Hook D2-B: {respuestas.get('hook_d2b', 'No respondido')}
- Hook D3-A: {respuestas.get('hook_d3a', 'No respondido')}
- Hook D3-B: {respuestas.get('hook_d3b', 'No respondido')}
- Hook que publicaría primero y por qué: {respuestas.get('hook_primero', 'No respondido')}
- Ideas Pilar Atracción (4 ideas): {respuestas.get('ideas_atraccion', 'No respondido')}
- Ideas Pilar Autoridad (4 ideas): {respuestas.get('ideas_autoridad', 'No respondido')}
- Ideas Pilar Conversión (4 ideas): {respuestas.get('ideas_conversion', 'No respondido')}

PARTE 3 — PROTOCOLO DE RECICLAJE:
- Idea más sólida elegida: {respuestas.get('idea_solida', 'No respondido')}
- Formato 1 (post texto largo): {respuestas.get('formato1', 'No respondido')}
- Formato 2 (carrusel/infografía): {respuestas.get('formato2', 'No respondido')}
- Formato 3 (video corto/reel): {respuestas.get('formato3', 'No respondido')}
- Formato 4 (historia/caso de estudio): {respuestas.get('formato4', 'No respondido')}
- Formato 5 (pregunta/encuesta): {respuestas.get('formato5', 'No respondido')}
- Orden de publicación y razón: {respuestas.get('orden_formatos', 'No respondido')}

PARTE 4 — PLANTILLAS DE ARRANQUE:
Plantilla 1 (Post de Posicionamiento):
- Hook: {respuestas.get('p1_hook', 'No respondido')}
- Desarrollo: {respuestas.get('p1_desarrollo', 'No respondido')}
- Punto de vista: {respuestas.get('p1_punto_vista', 'No respondido')}
- Cierre: {respuestas.get('p1_cierre', 'No respondido')}

Plantilla 2 (Historia de Origen):
- Momento antes: {respuestas.get('p2_antes', 'No respondido')}
- El quiebre: {respuestas.get('p2_quiebre', 'No respondido')}
- El después: {respuestas.get('p2_despues', 'No respondido')}
- La conexión: {respuestas.get('p2_conexion', 'No respondido')}

Plantilla 3 (Prueba Social Estratégica):
- Punto de partida del cliente: {respuestas.get('p3_inicio', 'No respondido')}
- El proceso: {respuestas.get('p3_proceso', 'No respondido')}
- El resultado específico: {respuestas.get('p3_resultado', 'No respondido')}
- La lección: {respuestas.get('p3_leccion', 'No respondido')}

PARTE 5 — VALIDACIÓN:
- ¿Sistema sostenible en semana difícil?: {respuestas.get('val_sostenible', 'No respondido')}
- ¿Las 3 plantillas están listas para publicar?: {respuestas.get('val_plantillas', 'No respondido')}
- Protocolo de contingencia: {respuestas.get('protocolo_contingencia', 'No respondido')}
- ¿Herramientas de producción definidas?: {respuestas.get('val_herramientas', 'No respondido')}
"""

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    return response.text


def generate_modulo5_gemini(respuestas: dict, contexto_previo: str) -> str:
    """Genera el Plan de Arranque Anti-Inercia (Módulo 5) usando Gemini."""
    client = google_genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""Eres el motor de análisis del Programa Anti-Inercia de Marca Personal, creado por Fedor Sawoloka.

Este módulo genera el "Plan de Arranque Anti-Inercia" — el documento final del programa. Es el entregable más importante y el que el participante usará como brújula durante sus primeros 30 días.

ADVERTENCIA CRÍTICA: Este documento debe ser el más personalizado de todos. Tiene acceso al contexto completo de los 5 módulos anteriores. No hay excusa para generar algo genérico. Cada sección debe estar calibrada a la realidad específica de este participante.

METODOLOGÍA GOLD STANDARD — PRINCIPIOS PARA EL DOCUMENTO FINAL:
1. Ejecutabilidad máxima: Cada tarea debe tener verbo, fecha y criterio de éxito. No objetivos. Tareas.
2. Coherencia total: El plan debe resolver directamente las inercias identificadas en el Módulo 0 y 1.
3. Calibración real: El plan debe ser operable con los recursos declarados (tiempo, presupuesto, nivel tecnológico), no con los ideales.
4. Sistema completo: Incluye protocolos de ajuste para cuando algo no funcione.

INSTRUCCIONES DE TONO:
- Este es el cierre del programa. El tono debe ser motivador pero realista. No inspiracional vacío.
- Directo, específico, accionable.
- Formato: ## para secciones, ### para subsecciones.
- NO uses emojis ni símbolos decorativos.

ESTRUCTURA OBLIGATORIA — PLAN DE ARRANQUE ANTI-INERCIA:

## Resumen Ejecutivo de Marca Personal
Síntesis de una página del Arquetipo Ganador, cliente ideal, diferenciador y Enemigo Común. El espejo de quién es como profesional con marca. Basado en el Documento Maestro del Módulo 2.

## Sus 3 Inercias Críticas y Cómo el Plan las Contrarresta
Conexión directa entre lo que lo ha frenado hasta hoy (identificado en Módulos 0 y 1) y las acciones concretas del plan que rompen cada patrón. Para cada inercia: la acción específica que la contrarresta.

## Plan de Acción Semana a Semana — Primeros 30 Días
4 semanas con tareas específicas, calibradas a:
- Frecuencia de publicación declarada en Módulo 5
- Canal elegido en Módulo 5
- Recursos disponibles (tiempo, presupuesto, nivel tecnológico)
- Las 3 plantillas de arranque del Módulo 4

Cada semana: tareas con verbo y criterio de éxito. No objetivos. Tareas.

### Semana 1 — Preparación
### Semana 2 — Activación
### Semana 3 — Autoridad
### Semana 4 — Evaluación y ajuste

## Las 3 Métricas que Debe Revisar
Explicación paso a paso de cómo acceder a cada métrica en su plataforma principal. Sin asumir conocimiento técnico previo. Calibradas al canal elegido y nivel tecnológico declarado.

## Protocolo de Ajuste — Los 3 Escenarios
Para consultar cuando algo no funcione:
### Escenario A: El alcance no crece
Qué revisar y qué cambiar.
### Escenario B: El alcance crece pero no llegan contactos
Qué revisar y qué cambiar.
### Escenario C: Llegan contactos pero no cierra
Qué revisar y qué cambiar.

## Su Primera Publicación Estructurada
La pieza declarada en el Módulo 5, construida con el hook, el desarrollo y el cierre. El sistema no la inventa. La construye con el contenido que el participante ya definió en sus módulos anteriores.

## Propuesta de Colaboración
Cómo la Asesoría Estratégica Continua con Fedor Sawoloka puede acelerar la implementación de este plan. Qué incluye, qué resuelve y cómo dar el siguiente paso: https://wa.link/mn6wcr

---
Documento generado por el Programa Anti-Inercia de Marca Personal | Metodologia de Fedor Sawoloka | yosoyelruso.com

CONTEXTO COMPLETO DE LOS 5 MÓDULOS ANTERIORES:
{contexto_previo[:6000] if contexto_previo else 'No disponible'}

RESPUESTAS DEL MÓDULO 5:

PARTE 1 — PUNTO DE PARTIDA REAL:
- ¿Cuándo puede comenzar a ejecutar?: {respuestas.get('cuando_arrancar', 'No respondido')}
- Qué necesita resolver antes (si aplica): {respuestas.get('que_resolver', 'No respondido')}
- Horas reales disponibles esta semana: {respuestas.get('horas_reales', 'No respondido')}
- Canal donde tiene más presencia hoy: {respuestas.get('canal_actual', 'No respondido')}
- ¿Tiene alguna pieza de contenido lista?: {respuestas.get('pieza_lista', 'No respondido')}

PARTE 2 — FRICCIONES PARA ARRANCAR:
- Situación más probable que lo frene: {respuestas.get('friccion_principal', 'No respondido')}
- Descripción de la fricción (si aplica): {respuestas.get('friccion_descripcion', 'No respondido')}
- ¿Con qué frecuencia abandona proyectos antes de 30 días?: {respuestas.get('patron_abandono', 'No respondido')}
- Qué necesitaría ver en las primeras 2 semanas para saber que va bien: {respuestas.get('senal_progreso', 'No respondido')}

PARTE 3 — RECURSOS REALES:
- ¿Tiene herramienta de diseño?: {respuestas.get('herramienta_diseno', 'No respondido')}
- ¿Puede grabar video?: {respuestas.get('puede_grabar', 'No respondido')}
- ¿Tiene espacio/momento fijo para producción?: {respuestas.get('momento_produccion', 'No respondido')}
- Día y hora de producción (si aplica): {respuestas.get('dia_hora_produccion', 'No respondido')}
- Presupuesto para los primeros 30 días: {respuestas.get('presupuesto_30dias', 'No respondido')}

PARTE 4 — COMPROMISOS DE ARRANQUE:
- Frecuencia de publicación comprometida: {respuestas.get('frecuencia_comprometida', 'No respondido')}
- Canal de concentración 100%: {respuestas.get('canal_100', 'No respondido')}
- Primera pieza de contenido (tema + formato): {respuestas.get('primera_pieza', 'No respondido')}
- Fecha de publicación de la primera pieza: {respuestas.get('fecha_primera_pieza', 'No respondido')}
- ¿Tiene persona de rendición de cuentas?: {respuestas.get('accountability', 'No respondido')}
- Quién es y qué le dirá: {respuestas.get('accountability_detalle', 'No respondido')}

PARTE 5 — DUDAS Y RESULTADO ESPERADO:
- Dudas pendientes (si las hay): {respuestas.get('dudas_pendientes', 'No respondido')}
- Resultado específico en 30 días que justificaría la inversión: {respuestas.get('resultado_30dias', 'No respondido')}
"""

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    return response.text


# ── Módulo 4 asíncrono ──

def _procesar_modulo4(job_id: str, respuestas: dict, contexto_previo: str, email: str):
    try:
        documento = generate_modulo4_gemini(respuestas, contexto_previo)
        pdf_bytes = generate_pdf_branded(
            content=documento,
            titulo="Sistema Operativo de Contenido — Modulo 4",
            subtitulo="Programa Anti-Inercia de Marca Personal  |  yosoyelruso.com",
            nombre_archivo="sistema-operativo-contenido-modulo-4.pdf"
        )
        completar_job(job_id, pdf_bytes, "sistema-operativo-contenido-modulo-4.pdf")
        try:
            save_programa_lead(email, 4, {})
        except Exception as e:
            print(f"Error registrando actividad M4: {e}")
    except Exception as e:
        print(f"Error procesando M4 job {job_id}: {e}")
        fallar_job(job_id, str(e))


@app.post("/programa/modulo4/iniciar")
async def iniciar_modulo4(
    background_tasks: BackgroundTasks,
    email: str = Form(...),
    dias_publicacion: str = Form(...),
    distribucion_pilares: str = Form(...),
    razon_distribucion: str = Form(...),
    dia_produccion_tipo: str = Form(...),
    dia_produccion: str = Form(default=""),
    dolor1: str = Form(...),
    dolor2: str = Form(...),
    dolor3: str = Form(...),
    hook_d1a: str = Form(...),
    hook_d1b: str = Form(...),
    hook_d2a: str = Form(...),
    hook_d2b: str = Form(...),
    hook_d3a: str = Form(...),
    hook_d3b: str = Form(...),
    hook_primero: str = Form(...),
    ideas_atraccion: str = Form(...),
    ideas_autoridad: str = Form(...),
    ideas_conversion: str = Form(...),
    idea_solida: str = Form(...),
    formato1: str = Form(...),
    formato2: str = Form(...),
    formato3: str = Form(...),
    formato4: str = Form(...),
    formato5: str = Form(...),
    orden_formatos: str = Form(...),
    p1_hook: str = Form(...),
    p1_desarrollo: str = Form(...),
    p1_punto_vista: str = Form(...),
    p1_cierre: str = Form(...),
    p2_antes: str = Form(...),
    p2_quiebre: str = Form(...),
    p2_despues: str = Form(...),
    p2_conexion: str = Form(...),
    p3_inicio: str = Form(...),
    p3_proceso: str = Form(...),
    p3_resultado: str = Form(...),
    p3_leccion: str = Form(...),
    val_sostenible: str = Form(...),
    val_plantillas: str = Form(...),
    protocolo_contingencia: str = Form(...),
    val_herramientas: str = Form(...),
    pdf_m0: UploadFile = File(...),
    pdf_m1: UploadFile = File(...),
    pdf_m2: UploadFile = File(...),
    pdf_m3: UploadFile = File(...)
):
    if not check_email_access(email):
        raise HTTPException(status_code=403, detail="Este correo no tiene acceso al programa.")

    texto_m0 = extract_text_from_pdf(await pdf_m0.read())
    texto_m1 = extract_text_from_pdf(await pdf_m1.read())
    texto_m2 = extract_text_from_pdf(await pdf_m2.read())
    texto_m3 = extract_text_from_pdf(await pdf_m3.read())
    contexto_previo = f"=== M0 ===\n{texto_m0}\n\n=== M1 ===\n{texto_m1}\n\n=== M2 ===\n{texto_m2}\n\n=== M3 ===\n{texto_m3}"

    job_id = str(uuid.uuid4())
    crear_job(job_id)

    respuestas = {k: v for k, v in locals().items()
                  if k not in ['email', 'pdf_m0', 'pdf_m1', 'pdf_m2', 'pdf_m3',
                                'contexto_previo', 'texto_m0', 'texto_m1', 'texto_m2', 'texto_m3',
                                'background_tasks', 'job_id']}

    background_tasks.add_task(_procesar_modulo4, job_id, respuestas, contexto_previo, email)
    return {"job_id": job_id, "status": "processing"}


# ── Módulo 5 asíncrono ──

def _procesar_modulo5(job_id: str, respuestas: dict, contexto_previo: str, email: str):
    try:
        documento = generate_modulo5_gemini(respuestas, contexto_previo)
        pdf_bytes = generate_pdf_branded(
            content=documento,
            titulo="Plan de Arranque Anti-Inercia — Modulo 5",
            subtitulo="Programa Anti-Inercia de Marca Personal  |  yosoyelruso.com",
            nombre_archivo="plan-arranque-anti-inercia-modulo-5.pdf"
        )
        completar_job(job_id, pdf_bytes, "plan-arranque-anti-inercia-modulo-5.pdf")
        try:
            save_programa_lead(email, 5, {"fecha_arranque": respuestas.get("cuando_arrancar", "")})
        except Exception as e:
            print(f"Error registrando actividad M5: {e}")
    except Exception as e:
        print(f"Error procesando M5 job {job_id}: {e}")
        fallar_job(job_id, str(e))


@app.post("/programa/modulo5/iniciar")
async def iniciar_modulo5(
    background_tasks: BackgroundTasks,
    email: str = Form(...),
    cuando_arrancar: str = Form(...),
    que_resolver: str = Form(default=""),
    horas_reales: str = Form(...),
    canal_actual: str = Form(...),
    pieza_lista: str = Form(...),
    friccion_principal: str = Form(...),
    friccion_descripcion: str = Form(default=""),
    patron_abandono: str = Form(...),
    senal_progreso: str = Form(...),
    herramienta_diseno: str = Form(...),
    puede_grabar: str = Form(...),
    momento_produccion: str = Form(...),
    dia_hora_produccion: str = Form(default=""),
    presupuesto_30dias: str = Form(...),
    frecuencia_comprometida: str = Form(...),
    canal_100: str = Form(...),
    primera_pieza: str = Form(...),
    fecha_primera_pieza: str = Form(...),
    accountability: str = Form(...),
    accountability_detalle: str = Form(default=""),
    dudas_pendientes: str = Form(default=""),
    resultado_30dias: str = Form(...),
    pdf_m0: UploadFile = File(...),
    pdf_m1: UploadFile = File(...),
    pdf_m2: UploadFile = File(...),
    pdf_m3: UploadFile = File(...),
    pdf_m4: UploadFile = File(...)
):
    if not check_email_access(email):
        raise HTTPException(status_code=403, detail="Este correo no tiene acceso al programa.")

    texto_m0 = extract_text_from_pdf(await pdf_m0.read())
    texto_m1 = extract_text_from_pdf(await pdf_m1.read())
    texto_m2 = extract_text_from_pdf(await pdf_m2.read())
    texto_m3 = extract_text_from_pdf(await pdf_m3.read())
    texto_m4 = extract_text_from_pdf(await pdf_m4.read())
    contexto_previo = f"=== M0 ===\n{texto_m0}\n\n=== M1 ===\n{texto_m1}\n\n=== M2 ===\n{texto_m2}\n\n=== M3 ===\n{texto_m3}\n\n=== M4 ===\n{texto_m4}"

    job_id = str(uuid.uuid4())
    crear_job(job_id)

    respuestas = {k: v for k, v in locals().items()
                  if k not in ['email', 'pdf_m0', 'pdf_m1', 'pdf_m2', 'pdf_m3', 'pdf_m4',
                                'contexto_previo', 'texto_m0', 'texto_m1', 'texto_m2', 'texto_m3', 'texto_m4',
                                'background_tasks', 'job_id']}

    background_tasks.add_task(_procesar_modulo5, job_id, respuestas, contexto_previo, email)
    return {"job_id": job_id, "status": "processing"}


# ============================================================
# LEAD MAGNET — MAPA DE FUGA COMERCIAL
# ============================================================
# Flujo: formulario público -> trabajo asíncrono -> Gemini -> PDF (máx. 2 páginas)
# -> Google Sheets + Mailchimp -> correo con PDF adjunto.

class MapaFugaRequest(BaseModel):
    nombre: str
    email: EmailStr
    whatsapp: str
    empresa: str
    sector: str
    consentimiento: bool = False
    q1_respuesta: str
    q2_cac: str
    q3_clientes: str
    q4_capacidad: str
    q5_previsibilidad: str
    q6_seguimiento: str
    q7_redes: str
    q8_propuesta_valor: str
    q9_marketing_ventas: str
    q10_rentabilidad: str
    utm_source: Optional[str] = ""
    utm_medium: Optional[str] = ""
    utm_campaign: Optional[str] = ""


MAPA_FUGA_PREGUNTAS = {
    "q1_respuesta": "Velocidad de respuesta a prospectos",
    "q2_cac": "Medición del costo de adquisición (CAC)",
    "q3_clientes": "Gestión de información de clientes y prospectos",
    "q4_capacidad": "Capacidad operativa ante un aumento de demanda",
    "q5_previsibilidad": "Previsibilidad de clientes nuevos",
    "q6_seguimiento": "Disciplina de seguimiento de cotizaciones",
    "q7_redes": "Comprensión del papel de redes y contenido",
    "q8_propuesta_valor": "Claridad de propuesta de valor",
    "q9_marketing_ventas": "Conexión entre marketing y ventas",
    "q10_rentabilidad": "Medición de rentabilidad por canal",
}

MAPA_FUGA_BLOQUES = {
    "Respuesta y seguimiento": ["q1_respuesta", "q6_seguimiento"],
    "Datos y rentabilidad": ["q2_cac", "q10_rentabilidad"],
    "Orden comercial": ["q3_clientes", "q4_capacidad", "q9_marketing_ventas"],
    "Previsibilidad de demanda": ["q5_previsibilidad"],
    "Oferta y enfoque": ["q7_redes", "q8_propuesta_valor"],
}

MAPA_FUGA_DESCRIPCIONES = {
    "Respuesta y seguimiento": "La demanda llega, pero se enfría o se abandona antes de convertirse en conversación comercial.",
    "Datos y rentabilidad": "La empresa invierte y toma decisiones sin trazabilidad suficiente sobre el retorno que genera cada canal.",
    "Orden comercial": "Los contactos, responsables y procesos no operan como un sistema; las oportunidades pueden perderse entre personas y herramientas.",
    "Previsibilidad de demanda": "La venta depende demasiado de recomendaciones, clientes anteriores o circunstancias que no se pueden controlar ni escalar.",
    "Oferta y enfoque": "La empresa puede estar comunicando actividad, pero no una diferencia clara ni una ruta que conecte visibilidad con ventas.",
}


def normalizar_respuesta_mapa(valor: str) -> str:
    """Acepta a/b/c, A/B/C o textos provenientes del formulario y devuelve a, b o c."""
    texto = (valor or "").strip().lower()
    if texto.startswith("a"):
        return "a"
    if texto.startswith("b"):
        return "b"
    if texto.startswith("c"):
        return "c"
    return "c"


def clasificar_mapa_fuga(data: MapaFugaRequest) -> dict:
    puntos_por_respuesta = {"a": 0, "b": 1, "c": 2}
    respuestas = {campo: normalizar_respuesta_mapa(getattr(data, campo)) for campo in MAPA_FUGA_PREGUNTAS}
    puntaje_total = sum(puntos_por_respuesta[r] for r in respuestas.values())

    puntajes_bloque = {
        bloque: sum(puntos_por_respuesta[respuestas[campo]] for campo in campos)
        for bloque, campos in MAPA_FUGA_BLOQUES.items()
    }
    orden_bloques = sorted(
        puntajes_bloque.items(),
        key=lambda item: (item[1], len(MAPA_FUGA_BLOQUES[item[0]])),
        reverse=True,
    )
    fuga_principal = orden_bloques[0][0]
    secundarias = [item[0] for item in orden_bloques[1:3] if item[1] > 0]

    if puntaje_total <= 3:
        nivel = "Sistema bajo control"
        prioridad = "Consolidar la disciplina de medición y prevenir que las fricciones puntuales se conviertan en inercias."
    elif puntaje_total <= 7:
        nivel = "Fricción en crecimiento"
        prioridad = "Ordenar el bloque que concentra más fricción antes de que la pérdida de oportunidades se vuelva habitual."
    elif puntaje_total <= 12:
        nivel = "Sistema comercial vulnerable"
        prioridad = "Priorizar el rediseño del proceso comercial; hay señales de fuga que ya limitan el aprovechamiento de la demanda."
    else:
        nivel = "Fuga comercial crítica"
        prioridad = "Detener la pérdida de oportunidades con una intervención estructurada antes de invertir más presupuesto o intentar escalar."

    return {
        "respuestas_normalizadas": respuestas,
        "puntaje_total": puntaje_total,
        "puntajes_bloque": puntajes_bloque,
        "fuga_principal": fuga_principal,
        "fugas_secundarias": secundarias,
        "nivel": nivel,
        "prioridad": prioridad,
    }


def generate_mapa_fuga_gemini(data: MapaFugaRequest, clasificacion: dict) -> str:
    """Genera un diagnóstico breve y personalizado, deliberadamente limitado para no sustituir una Auditoría 45D."""
    client = google_genai.Client(api_key=GEMINI_API_KEY)
    respuestas_legibles = "\n".join(
        f"- {pregunta}: opción {clasificacion['respuestas_normalizadas'][campo].upper()}"
        for campo, pregunta in MAPA_FUGA_PREGUNTAS.items()
    )
    secundarias = ", ".join(clasificacion["fugas_secundarias"]) if clasificacion["fugas_secundarias"] else "Sin fugas secundarias relevantes"

    prompt = f"""Eres un consultor estratégico que aplica la metodología Anti-Inercia de Fedor Sawoloka.

Genera el "Mapa de Fuga Comercial" de {data.nombre}, de la empresa {data.empresa} ({data.sector}). Este es un lead magnet gratuito: debe ser honesto, útil y específico, pero NO debe sustituir una Auditoría Estratégica 45D ni entregar un plan completo de implementación.

DATOS CALCULADOS DEL SISTEMA:
- Puntaje total: {clasificacion['puntaje_total']} de 20
- Nivel: {clasificacion['nivel']}
- Fuga principal: {clasificacion['fuga_principal']}
- Lectura de esa fuga: {MAPA_FUGA_DESCRIPCIONES[clasificacion['fuga_principal']]}
- Fugas secundarias: {secundarias}
- Prioridad calculada: {clasificacion['prioridad']}

RESPUESTAS DEL USUARIO:
{respuestas_legibles}

INSTRUCCIONES OBLIGATORIAS:
- Escribe en español latinoamericano, tono directo, profesional y respetuoso.
- Usa el nombre de la empresa cuando encaje naturalmente.
- No inventes cifras, ingresos, pérdidas monetarias ni datos que el usuario no proporcionó.
- No uses emojis ni frases de autoayuda.
- Máximo 360 palabras de contenido total para asegurar que el PDF no exceda dos páginas.
- El diagnóstico debe confrontar la realidad con respeto: abrir los ojos, no humillar.
- Ofrece solo UNA prioridad inicial de bajo riesgo; no conviertas el resultado en una lista de tácticas.
- Explica con claridad que el mapa identifica dónde se está frenando el sistema, mientras la Auditoría 45D investiga causas, responsables, indicadores y plan de corrección.
- Utiliza exactamente la siguiente estructura Markdown:

# Tu Mapa de Fuga Comercial
## Resultado principal
Indica el nivel y la fuga dominante en una frase clara.

## Lo que tus respuestas revelan
Incluye 2 o 3 evidencias específicas basadas en las respuestas A/B/C, explicadas en lenguaje de negocio.

## El riesgo de mantener esta inercia
Explica la consecuencia operativa o comercial probable sin inventar números.

## Tu primera prioridad
Una acción concreta de observación, orden o medición para los próximos 7 días.

## El siguiente paso
Incluye exactamente esta idea, adaptada con naturalidad: "Este mapa identifica dónde se está frenando tu sistema comercial. Corregirlo exige analizar causas, responsables, procesos e indicadores. Ese es el trabajo de la Auditoría Estratégica 45D." Cierra invitando a aplicar en https://yosoyelruso.com/auditoria-45d.html

---
Mapa de Fuga Comercial | Metodología Anti-Inercia de Fedor Sawoloka | yosoyelruso.com
"""
    response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
    return response.text


def generate_mapa_fuga_fallback(data: MapaFugaRequest, clasificacion: dict) -> str:
    """Respuesta controlada si Gemini no está disponible; conserva valor y no revela tácticas extensas."""
    evidencias = []
    for campo, respuesta in clasificacion["respuestas_normalizadas"].items():
        if respuesta in ["b", "c"]:
            evidencias.append(MAPA_FUGA_PREGUNTAS[campo])
        if len(evidencias) == 3:
            break
    evidencia_texto = ", ".join(evidencias) if evidencias else "algunas áreas puntuales de tu sistema comercial"
    return f"""# Tu Mapa de Fuga Comercial

## Resultado principal
Tu nivel actual es: {clasificacion['nivel']}. La fuga dominante se concentra en {clasificacion['fuga_principal']}.

## Lo que tus respuestas revelan
Tus respuestas muestran señales de fricción en {evidencia_texto}. {MAPA_FUGA_DESCRIPCIONES[clasificacion['fuga_principal']]}

## El riesgo de mantener esta inercia
Cuando esta fuga no se mide ni se ordena, la empresa puede seguir generando actividad sin convertirla de manera consistente en oportunidades comerciales aprovechables.

## Tu primera prioridad
Durante los próximos 7 días, registra de forma simple qué ocurre con cada prospecto que llega: origen, responsable, tiempo de respuesta y siguiente acción. No intentes cambiar todo todavía; primero haz visible el recorrido real.

## El siguiente paso
Este mapa identifica dónde se está frenando tu sistema comercial. Corregirlo exige analizar causas, responsables, procesos e indicadores. Ese es el trabajo de la Auditoría Estratégica 45D. Conoce el servicio en https://yosoyelruso.com/auditoria-45d.html

---
Mapa de Fuga Comercial | Metodología Anti-Inercia de Fedor Sawoloka | yosoyelruso.com
"""


def generate_mapa_fuga_pdf(documento: str, empresa: str) -> bytes:
    """Genera un PDF visualmente consistente y limitado a dos páginas para el lead magnet."""
    documento = limpiar_para_pdf(documento)
    empresa = limpiar_para_pdf(empresa)
    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_margins(16, 18, 16)
    pdf.add_page()

    pdf.set_fill_color(44, 62, 80)
    pdf.rect(0, 0, 210, 30, "F")
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 15)
    pdf.set_xy(16, 7)
    pdf.cell(0, 8, "Tu Mapa de Fuga Comercial", ln=True)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(255, 140, 66)
    pdf.set_x(16)
    pdf.cell(0, 6, empresa)
    pdf.set_y(38)

    for linea in documento.split("\n"):
        texto = linea.strip()
        if not texto or texto == "---":
            pdf.ln(2)
            continue
        if texto.startswith("# "):
            continue
        if texto.startswith("## "):
            pdf.ln(2)
            pdf.set_font("Helvetica", "B", 12)
            pdf.set_text_color(44, 62, 80)
            pdf.multi_cell(0, 6, texto[3:])
            pdf.set_draw_color(255, 140, 66)
            pdf.set_line_width(0.45)
            pdf.line(16, pdf.get_y(), 194, pdf.get_y())
            pdf.ln(2)
        else:
            pdf.set_font("Helvetica", "", 9.5)
            pdf.set_text_color(60, 60, 60)
            pdf.multi_cell(0, 4.8, texto.replace("**", "").replace("*", ""))
            pdf.ln(0.7)

    if pdf.page_no() > 2:
        raise ValueError("El diagnóstico excedió el límite de dos páginas. Reduce el contenido generado.")
    return bytes(pdf.output())


def get_or_create_mapa_fuga_sheet(service):
    """Garantiza una pestaña propia para el lead magnet sin tocar las pestañas existentes."""
    titulo = "Autodiagnostico_AntiInercia"
    spreadsheet = service.spreadsheets().get(spreadsheetId=GOOGLE_SHEET_ID).execute()
    hojas = {sheet["properties"]["title"] for sheet in spreadsheet.get("sheets", [])}
    if titulo not in hojas:
        service.spreadsheets().batchUpdate(
            spreadsheetId=GOOGLE_SHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": titulo}}}]},
        ).execute()
    headers = [
        "Fecha", "Nombre", "Email", "WhatsApp", "Empresa", "Sector",
        "Q1_Respuesta", "Q2_CAC", "Q3_Clientes", "Q4_Capacidad", "Q5_Previsibilidad",
        "Q6_Seguimiento", "Q7_Redes", "Q8_PropuestaValor", "Q9_MarketingVentas", "Q10_Rentabilidad",
        "Puntaje", "Nivel", "Fuga_Principal", "Fugas_Secundarias", "Puntajes_por_Bloque",
        "UTM_Source", "UTM_Medium", "UTM_Campaign", "Consentimiento", "Email_Enviado",
    ]
    valores = service.spreadsheets().values().get(
        spreadsheetId=GOOGLE_SHEET_ID, range=f"{titulo}!A1:Z1"
    ).execute().get("values", [])
    if not valores:
        service.spreadsheets().values().update(
            spreadsheetId=GOOGLE_SHEET_ID,
            range=f"{titulo}!A1:Z1",
            valueInputOption="RAW",
            body={"values": [headers]},
        ).execute()
    return titulo


def save_mapa_fuga_to_google_sheets(data: MapaFugaRequest, clasificacion: dict, email_enviado: bool) -> bool:
    try:
        service = get_google_sheets_service()
        titulo = get_or_create_mapa_fuga_sheet(service)
        row = [
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"), data.nombre, data.email, data.whatsapp,
            data.empresa, data.sector,
            data.q1_respuesta, data.q2_cac, data.q3_clientes, data.q4_capacidad, data.q5_previsibilidad,
            data.q6_seguimiento, data.q7_redes, data.q8_propuesta_valor, data.q9_marketing_ventas, data.q10_rentabilidad,
            clasificacion["puntaje_total"], clasificacion["nivel"], clasificacion["fuga_principal"],
            ", ".join(clasificacion["fugas_secundarias"]), json.dumps(clasificacion["puntajes_bloque"], ensure_ascii=False),
            data.utm_source or "", data.utm_medium or "", data.utm_campaign or "",
            "Sí" if data.consentimiento else "No", "Sí" if email_enviado else "No",
        ]
        service.spreadsheets().values().append(
            spreadsheetId=GOOGLE_SHEET_ID,
            range=f"{titulo}!A:Z",
            valueInputOption="RAW",
            body={"values": [row]},
        ).execute()
        return True
    except Exception as e:
        print(f"Error guardando Mapa de Fuga Comercial en Google Sheets: {e}")
        return False


def subscribe_mapa_fuga_mailchimp(data: MapaFugaRequest, clasificacion: dict) -> bool:
    if not data.consentimiento:
        return False
    try:
        slug_fuga = clasificacion["fuga_principal"].lower().replace(" ", "-")
        slug_nivel = clasificacion["nivel"].lower().replace(" ", "-")
        tags = ["mapa-fuga-comercial", f"fuga-{slug_fuga}", f"nivel-{slug_nivel}"]
        for secundaria in clasificacion["fugas_secundarias"]:
            tags.append(f"friccion-{secundaria.lower().replace(' ', '-')}")
        payload = {
            "email_address": data.email,
            "status": "subscribed",
            "merge_fields": {"FNAME": data.nombre.split()[0] if data.nombre else ""},
            "tags": tags,
        }
        url = f"https://{MAILCHIMP_SERVER_PREFIX}.api.mailchimp.com/3.0/lists/{MAILCHIMP_LIST_ID}/members"
        response = requests.post(url, auth=("anystring", MAILCHIMP_API_KEY), json=payload, timeout=20)
        if response.status_code == 400 and "already a list member" in response.text:
            import hashlib
            email_hash = hashlib.md5(data.email.lower().encode()).hexdigest()
            update_url = f"https://{MAILCHIMP_SERVER_PREFIX}.api.mailchimp.com/3.0/lists/{MAILCHIMP_LIST_ID}/members/{email_hash}"
            requests.patch(update_url, auth=("anystring", MAILCHIMP_API_KEY), json={"merge_fields": payload["merge_fields"]}, timeout=20)
            tags_url = f"{update_url}/tags"
            requests.post(tags_url, auth=("anystring", MAILCHIMP_API_KEY), json={"tags": [{"name": tag, "status": "active"} for tag in tags]}, timeout=20)
        return response.status_code in (200, 201, 400)
    except Exception as e:
        print(f"Error registrando Mapa de Fuga Comercial en Mailchimp: {e}")
        return False


def send_mapa_fuga_by_email(recipient_email: str, nombre: str, empresa: str, clasificacion: dict, pdf_bytes: bytes) -> bool:
    """Envía el PDF adjunto usando el mismo canal SMTP ya activo en Configura tu IA."""
    if not GMAIL_APP_PASSWORD:
        print("GMAIL_APP_PASSWORD no configurado, no se puede entregar el Mapa de Fuga Comercial")
        return False
    try:
        primer_nombre = nombre.strip().split()[0] if nombre and nombre.strip() else ""
        msg = MIMEMultipart("mixed")
        msg["Subject"] = "Tu Mapa de Fuga Comercial está listo"
        msg["From"] = f"Fedor Sawoloka <{GMAIL_USER}>"
        msg["To"] = recipient_email

        cuerpo = MIMEMultipart("alternative")
        texto = f"""Hola {primer_nombre},

Tu Mapa de Fuga Comercial ya está listo.

El diagnóstico detectó como foco principal: {clasificacion['fuga_principal']}. Adjuntamos tu PDF personalizado para que puedas revisarlo con calma.

Este mapa identifica dónde se está frenando tu sistema comercial. Corregirlo exige analizar causas, responsables, procesos e indicadores. Ese es el trabajo de la Auditoría Estratégica 45D.

Conoce la Auditoría 45D: https://yosoyelruso.com/auditoria-45d.html

Fedor Sawoloka
Estrategia Anti-Inercia
"""
        html = f"""
        <html><body style="font-family:Arial,sans-serif;max-width:640px;margin:0 auto;padding:20px;color:#2C3E50;">
          <div style="background:#2C3E50;padding:22px;border-radius:8px 8px 0 0;">
            <h1 style="margin:0;color:#fff;font-size:22px;">Tu Mapa de Fuga Comercial está listo</h1>
            <p style="margin:7px 0 0;color:#FF8C42;">Metodología Anti-Inercia de Fedor Sawoloka</p>
          </div>
          <div style="background:#f6f7f8;padding:24px;border:1px solid #e1e5e8;border-radius:0 0 8px 8px;">
            <p>Hola {primer_nombre},</p>
            <p>Ya analizamos tus respuestas para <strong>{empresa}</strong>. Tu principal foco de atención está en <strong>{clasificacion['fuga_principal']}</strong>.</p>
            <p>Adjunto encontrarás tu <strong>Mapa de Fuga Comercial</strong> personalizado. Léelo como un punto de partida: identifica dónde se está frenando tu sistema, pero no sustituye el análisis de causas, responsables, procesos e indicadores que requiere una intervención estratégica.</p>
            <p style="margin:24px 0;"><a href="https://yosoyelruso.com/auditoria-45d.html" style="background:#FF8C42;color:#fff;padding:12px 18px;text-decoration:none;border-radius:6px;font-weight:bold;">Conocer la Auditoría 45D</a></p>
            <p style="font-size:12px;color:#6c757d;">Generado en yosoyelruso.com con la metodología Anti-Inercia.</p>
          </div>
        </body></html>
        """
        cuerpo.attach(MIMEText(texto, "plain", "utf-8"))
        cuerpo.attach(MIMEText(html, "html", "utf-8"))
        msg.attach(cuerpo)
        adjunto = MIMEApplication(pdf_bytes, _subtype="pdf")
        adjunto.add_header("Content-Disposition", "attachment", filename="mapa-de-fuga-comercial.pdf")
        msg.attach(adjunto)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_USER, recipient_email, msg.as_string())
        print(f"Mapa de Fuga Comercial enviado exitosamente a {recipient_email}")
        return True
    except Exception as e:
        print(f"Error enviando Mapa de Fuga Comercial: {e}")
        return False


def _procesar_mapa_fuga(job_id: str, data: MapaFugaRequest):
    try:
        clasificacion = clasificar_mapa_fuga(data)
        try:
            documento = generate_mapa_fuga_gemini(data, clasificacion)
        except Exception as e:
            print(f"Gemini falló en Mapa de Fuga Comercial: {e}")
            documento = generate_mapa_fuga_fallback(data, clasificacion)
        pdf_bytes = generate_mapa_fuga_pdf(documento, data.empresa)
        email_enviado = send_mapa_fuga_by_email(data.email, data.nombre, data.empresa, clasificacion, pdf_bytes)
        try:
            save_mapa_fuga_to_google_sheets(data, clasificacion, email_enviado)
        except Exception as e:
            print(f"Error no crítico guardando Mapa de Fuga Comercial: {e}")
        try:
            subscribe_mapa_fuga_mailchimp(data, clasificacion)
        except Exception as e:
            print(f"Error no crítico registrando Mapa de Fuga Comercial en Mailchimp: {e}")
        if not email_enviado:
            raise RuntimeError("El diagnóstico fue generado, pero no pudo entregarse al correo indicado.")
        completar_job(job_id, pdf_bytes, "mapa-de-fuga-comercial.pdf")
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]["email_sent"] = True
                jobs[job_id]["classification"] = {
                    "nivel": clasificacion["nivel"],
                    "fuga_principal": clasificacion["fuga_principal"],
                }
    except Exception as e:
        print(f"Error procesando Mapa de Fuga Comercial {job_id}: {e}")
        fallar_job(job_id, str(e))


@app.post("/mapa-fuga-comercial/iniciar")
async def iniciar_mapa_fuga(data: MapaFugaRequest, background_tasks: BackgroundTasks):
    if not data.consentimiento:
        raise HTTPException(status_code=400, detail="Necesitamos tu autorización para enviarte el resultado y comunicaciones estratégicas.")
    if len(data.whatsapp.strip()) < 7:
        raise HTTPException(status_code=400, detail="Ingresa un número de WhatsApp válido con código de país.")
    job_id = str(uuid.uuid4())
    crear_job(job_id)
    background_tasks.add_task(_procesar_mapa_fuga, job_id, data)
    return {"job_id": job_id, "status": "processing", "message": "Estamos preparando tu Mapa de Fuga Comercial."}


@app.get("/mapa-fuga-comercial/job/{job_id}")
def consultar_mapa_fuga_job(job_id: str):
    limpiar_jobs_viejos()
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Trabajo no encontrado o expirado.")
    return {
        "job_id": job_id,
        "status": job["status"],
        "email_sent": job.get("email_sent", False),
        "classification": job.get("classification"),
        "error": job.get("error"),
    }


@app.get("/mapa-fuga-comercial/health")
def health_mapa_fuga():
    return {"status": "ready", "service": "Mapa de Fuga Comercial"}
