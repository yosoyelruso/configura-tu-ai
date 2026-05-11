import os
import json
import asyncio
import smtplib
import requests
import io
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from typing import Optional, List
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, EmailStr
from dotenv import load_dotenv
from google import genai as google_genai
from google.oauth2 import service_account
from googleapiclient.discovery import build
from fpdf import FPDF

load_dotenv()

app = FastAPI(title="Configura tu IA - API Backend")

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

# --- Modelos de datos ---
class FormData(BaseModel):
    email: str
    mailchimp_consent: bool = False
    # Sección 1 - Identidad Profesional
    nombre_cargo: str
    filosofia_trabajo: str
    responsabilidades: str
    diferenciador: str
    # Sección 2 - Contexto de Trabajo
    audiencia: str
    proyecto_actual: str
    cuello_botella: str
    # Sección 3 - Comportamiento de la IA
    uso_ia: List[str]
    nivel_ayuda: List[str]
    nivel_autonomia: List[str]
    tipo_resultado: List[str]
    importancia_accion: List[str]
    # Sección 4 - Estilo de Comunicación
    estilo_comunicacion: List[str]
    palabras_evitar: str
    formato_preferido: List[str]
    # Sección 5 - Contexto Adicional
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

# --- Funciones auxiliares ---

def classify_profile(data: FormData) -> dict:
    """Genera etiquetas inteligentes basadas en las respuestas del formulario."""
    nombre_lower = data.nombre_cargo.lower()

    # A. Tipo de perfil
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

    # B. Necesidad principal (basada en uso_ia + cuello_botella)
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

    # C. Nivel de madurez (basado en autonomía y tipo de resultado)
    autonomia_str = " ".join(data.nivel_autonomia).lower()
    resultado_str = " ".join(data.tipo_resultado).lower()
    maturity = "Explorador"
    if "copiloto" in autonomia_str or "sistemas completos" in resultado_str:
        maturity = "Listo para ejecutar"
    elif "planes" in resultado_str or "estructura" in autonomia_str:
        maturity = "En transición"

    # D. Potencial comercial (score)
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
    # Bonus por nivel de ambición
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
    """Genera el Documento Maestro de Contexto usando Google Gemini."""
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
    """Genera un documento básico sin Gemini como fallback."""
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
    """Guarda las respuestas en Google Sheets."""
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
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),   # Timestamp
            data.email,                                       # Email
            data.nombre_cargo,                                # Nombre y cargo
            data.filosofia_trabajo,                           # Filosofía de trabajo
            data.responsabilidades,                           # Responsabilidades
            data.diferenciador,                               # Diferenciador
            data.audiencia,                                   # Audiencia
            data.proyecto_actual,                             # Proyecto actual (ahora obligatorio)
            data.cuello_botella,                              # Cuello de botella
            uso_str,                                          # Uso de IA
            nivel_ayuda_str,                                  # Nivel de ayuda
            nivel_autonomia_str,                              # Nivel de autonomía
            tipo_resultado_str,                               # Tipo de resultado
            importancia_accion_str,                           # Importancia de acción
            estilo_str,                                       # Estilo comunicación
            data.palabras_evitar,                             # Palabras a evitar
            formato_str,                                      # Formato preferido
            data.enlaces_referencia or "",                    # Enlaces
            "Sí" if data.mailchimp_consent else "No",         # Consentimiento
            tags.get("profile_type", ""),                     # Tipo de perfil
            tags.get("need", ""),                             # Necesidad principal
            tags.get("maturity", ""),                         # Nivel de madurez
            tags.get("commercial_potential", ""),             # Potencial comercial
            str(tags.get("score", 0))                         # Score
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
    """Suscribe al usuario en Mailchimp con etiquetas inteligentes."""
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
            "merge_fields": {
                "FNAME": first_name,
            },
            "tags": mailchimp_tags
        }

        response = requests.post(
            url,
            auth=("anystring", MAILCHIMP_API_KEY),
            json=payload
        )

        if response.status_code == 400 and "already a list member" in response.text:
            import hashlib
            email_hash = hashlib.md5(data.email.lower().encode()).hexdigest()
            update_url = f"https://{MAILCHIMP_SERVER_PREFIX}.api.mailchimp.com/3.0/lists/{MAILCHIMP_LIST_ID}/members/{email_hash}"
            requests.patch(
                update_url,
                auth=("anystring", MAILCHIMP_API_KEY),
                json={"merge_fields": {"FNAME": first_name}, "tags": mailchimp_tags}
            )

            tags_url = f"https://{MAILCHIMP_SERVER_PREFIX}.api.mailchimp.com/3.0/lists/{MAILCHIMP_LIST_ID}/members/{email_hash}/tags"
            tags_payload = {"tags": [{"name": t, "status": "active"} for t in mailchimp_tags]}
            requests.post(
                tags_url,
                auth=("anystring", MAILCHIMP_API_KEY),
                json=tags_payload
            )

        return True
    except Exception as e:
        print(f"Error en Mailchimp: {e}")
        return False


def send_document_by_email(recipient_email: str, document: str, nombre_cargo: str):
    """Envía el Documento Maestro de Contexto por email al usuario."""
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


# --- Endpoints ---

@app.get("/")
def root():
    return {"status": "ok", "service": "Configura tu IA - Backend v2.0"}

@app.get("/health")
def health():
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

@app.post("/generate", response_model=GenerateResponse)
async def generate(data: FormData):
    """Endpoint principal: genera el documento y guarda los datos."""

    # 1. Clasificar perfil
    tags = classify_profile(data)

    # 2. Generar documento con Gemini (fallback si falla)
    document = None
    fallback_used = False

    try:
        document = generate_document_gemini(data)
    except Exception as e:
        print(f"Gemini falló: {e}")
        fallback_used = True
        document = generate_document_fallback(data)

    # 3. Guardar en Google Sheets
    try:
        save_to_google_sheets(data, tags)
    except Exception as e:
        print(f"Google Sheets falló: {e}")

    # 4. Suscribir en Mailchimp (solo si dio consentimiento)
    try:
        subscribe_to_mailchimp(data, tags)
    except Exception as e:
        print(f"Mailchimp falló: {e}")

    # 5. Enviar por email (opcional, no bloquea)
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
    """Genera un PDF del documento maestro y lo devuelve como archivo descargable."""
    try:
        pdf = FPDF(orientation='P', unit='mm', format='A4')
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.add_page()

        # Encabezado con fondo azul
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
                # Limpiar asteriscos de markdown bold
                clean = line.replace('**', '').replace('*', '')
                pdf.set_font('Helvetica', '', 10)
                pdf.set_text_color(60, 60, 60)
                pdf.multi_cell(0, 5, clean, ln=True)

        pdf_bytes = pdf.output()
        pdf_buffer = io.BytesIO(bytes(pdf_bytes))
        nombre_archivo = 'documento-maestro-contexto.pdf'

        return StreamingResponse(
            pdf_buffer,
            media_type='application/pdf',
            headers={
                'Content-Disposition': f'attachment; filename="{nombre_archivo}"',
                'Content-Type': 'application/pdf'
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'Error generando PDF: {str(e)}')
