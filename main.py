import os
import json
import asyncio
import smtplib
import requests
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from typing import Optional, List
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from dotenv import load_dotenv
from openai import OpenAI
from google.oauth2 import service_account
from googleapiclient.discovery import build

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
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MAILCHIMP_API_KEY = os.getenv("MAILCHIMP_API_KEY")
MAILCHIMP_LIST_ID = os.getenv("MAILCHIMP_LIST_ID")
MAILCHIMP_SERVER_PREFIX = os.getenv("MAILCHIMP_SERVER_PREFIX", "us7")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "google_credentials.json")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")  # JSON string como variable de entorno
GMAIL_USER = os.getenv("GMAIL_USER", "fedor.sawoloka@gmail.com")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")

# --- Modelos de datos ---
class FormData(BaseModel):
    email: str
    mailchimp_consent: bool = False
    # Sección 1
    nombre_cargo: str
    filosofia_trabajo: str
    responsabilidades: str
    diferenciador: str
    # Sección 2
    audiencia: str
    objetivo_ia: str
    cuello_botella: str
    # Sección 3
    estilo_comunicacion: List[str]
    palabras_evitar: str
    formato_preferido: List[str]
    # Sección 4
    proyecto_actual: Optional[str] = ""
    enlaces_referencia: Optional[str] = ""

class GenerateResponse(BaseModel):
    success: bool
    document: Optional[str] = None
    error: Optional[str] = None
    fallback: bool = False
    email_sent: bool = False

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
    
    # B. Necesidad principal
    objetivo_lower = data.objetivo_ia.lower()
    cuello_lower = data.cuello_botella.lower()
    combined = objetivo_lower + " " + cuello_lower
    
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
    
    # C. Nivel de madurez
    maturity = "Explorador"
    if data.proyecto_actual and len(data.proyecto_actual) > 30:
        maturity = "Listo para ejecutar"
    elif len(data.objetivo_ia) > 80 and len(data.cuello_botella) > 80:
        maturity = "En transición"
    
    # D. Potencial comercial (score)
    score = 0
    decision_roles = ["ceo", "director", "gerente", "dueño", "fundador", "propietario", "vp", "chief"]
    if any(w in nombre_lower for w in decision_roles):
        score += 2
    if len(data.objetivo_ia) > 50:
        score += 2
    if len(data.cuello_botella) > 50:
        score += 2
    if data.proyecto_actual and len(data.proyecto_actual) > 20:
        score += 2
    if data.enlaces_referencia and ("http" in data.enlaces_referencia or "www" in data.enlaces_referencia):
        score += 1
    if len(data.filosofia_trabajo) > 50 and len(data.diferenciador) > 50:
        score += 1
    
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


def generate_document_openai(data: FormData) -> str:
    """Genera el Documento Maestro de Contexto usando OpenAI."""
    client = OpenAI(api_key=OPENAI_API_KEY)
    
    estilo_str = ", ".join(data.estilo_comunicacion) if data.estilo_comunicacion else "No especificado"
    formato_str = ", ".join(data.formato_preferido) if data.formato_preferido else "No especificado"
    
    user_content = f"""
Respuestas del usuario:

SECCIÓN 1 - IDENTIDAD Y ROL PROFESIONAL:
- Nombre y cargo: {data.nombre_cargo}
- Filosofía de trabajo: {data.filosofia_trabajo}
- Responsabilidades principales: {data.responsabilidades}
- Diferenciador profesional: {data.diferenciador}

SECCIÓN 2 - AUDIENCIA Y OBJETIVOS:
- Audiencia / cliente ideal: {data.audiencia}
- Objetivo principal con IA: {data.objetivo_ia}
- Cuello de botella principal: {data.cuello_botella}

SECCIÓN 3 - TONO, ESTILO Y FORMATO:
- Estilo de comunicación preferido: {estilo_str}
- Palabras o estilos a evitar: {data.palabras_evitar}
- Formato de información preferido: {formato_str}

SECCIÓN 4 - CONTEXTO ADICIONAL:
- Proyecto actual: {data.proyecto_actual or 'No especificado'}
- Referencias / enlaces: {data.enlaces_referencia or 'No especificado'}
"""
    
    system_prompt = """Eres un experto en inteligencia artificial y productividad profesional. Con base en las siguientes respuestas de un profesional, genera un Documento Maestro de Contexto claro, estructurado y en primera persona, listo para ser pegado en cualquier chat de IA. El documento debe tener: nombre y rol, filosofía de trabajo, audiencia y objetivos, estilo de comunicación preferido, y contexto de proyectos actuales. Que sea directo, sin adornos, y que la IA que lo lea entienda exactamente con quién está hablando y cómo debe responder.

Al final del documento, en una línea aparte y en formato discreto, incluye exactamente este texto:
---
Documento generado con base en la metodología Gold Standard de Fedor Sawoloka."""
    
    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ],
        max_tokens=1500,
        temperature=0.7
    )
    
    return response.choices[0].message.content


def generate_document_fallback(data: FormData) -> str:
    """Genera un documento básico sin OpenAI como fallback."""
    estilo_str = ", ".join(data.estilo_comunicacion) if data.estilo_comunicacion else "No especificado"
    formato_str = ", ".join(data.formato_preferido) if data.formato_preferido else "No especificado"
    
    doc = f"""# Documento Maestro de Contexto

## Quién soy
{data.nombre_cargo}

## Mi filosofía de trabajo
{data.filosofia_trabajo}

## Mis responsabilidades principales
{data.responsabilidades}

## Lo que me diferencia
{data.diferenciador}

## Para quién trabajo
{data.audiencia}

## Qué quiero lograr con la IA
{data.objetivo_ia}

## Mi principal cuello de botella
{data.cuello_botella}

## Cómo quiero que la IA se comunique conmigo
Estilo preferido: {estilo_str}
Formatos preferidos: {formato_str}
Palabras o estilos a evitar: {data.palabras_evitar}

## Contexto de proyectos actuales
{data.proyecto_actual or 'No especificado'}

## Referencias
{data.enlaces_referencia or 'No especificado'}

---
Documento generado con base en la metodología Gold Standard de Fedor Sawoloka."""
    
    return doc


def save_to_google_sheets(data: FormData, tags: dict):
    """Guarda las respuestas en Google Sheets."""
    try:
        # Usar variable de entorno JSON si está disponible (para producción en Render)
        if GOOGLE_CREDENTIALS_JSON:
            import json as json_module
            creds_dict = json_module.loads(GOOGLE_CREDENTIALS_JSON)
            creds = service_account.Credentials.from_service_account_info(
                creds_dict,
                scopes=["https://www.googleapis.com/auth/spreadsheets"]
            )
        else:
            # Fallback: usar archivo local (para desarrollo)
            credentials_path = GOOGLE_CREDENTIALS_FILE
            if not os.path.isabs(credentials_path):
                credentials_path = os.path.join(os.path.dirname(__file__), credentials_path)
            creds = service_account.Credentials.from_service_account_file(
                credentials_path,
                scopes=["https://www.googleapis.com/auth/spreadsheets"]
            )
        
        service = build("sheets", "v4", credentials=creds)
        sheet = service.spreadsheets()
        
        estilo_str = ", ".join(data.estilo_comunicacion) if data.estilo_comunicacion else ""
        formato_str = ", ".join(data.formato_preferido) if data.formato_preferido else ""
        
        row = [
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),  # Timestamp
            data.email,                                      # Email
            data.nombre_cargo,                               # Nombre y cargo
            data.filosofia_trabajo,                          # Filosofía de trabajo
            data.responsabilidades,                          # Responsabilidades
            data.diferenciador,                              # Diferenciador
            data.audiencia,                                  # Audiencia
            data.objetivo_ia,                                # Objetivo con IA
            data.cuello_botella,                             # Cuello de botella
            estilo_str,                                      # Estilo comunicación
            data.palabras_evitar,                            # Palabras a evitar
            formato_str,                                     # Formato preferido
            data.proyecto_actual or "",                      # Proyecto actual
            data.enlaces_referencia or "",                   # Enlaces
            "Sí" if data.mailchimp_consent else "No",        # Consentimiento
            tags.get("profile_type", ""),                    # Tipo de perfil
            tags.get("need", ""),                            # Necesidad principal
            tags.get("maturity", ""),                        # Nivel de madurez
            tags.get("commercial_potential", ""),            # Potencial comercial
            str(tags.get("score", 0))                        # Score
        ]
        
        body = {"values": [row]}
        sheet.values().append(
            spreadsheetId=GOOGLE_SHEET_ID,
            range="A:T",
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
        # Extraer nombre del campo nombre_cargo
        nombre_parts = data.nombre_cargo.split(" ")
        first_name = nombre_parts[0] if nombre_parts else ""
        
        # Construir etiquetas
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
        
        # Si ya existe, actualizar
        if response.status_code == 400 and "already a list member" in response.text:
            import hashlib
            email_hash = hashlib.md5(data.email.lower().encode()).hexdigest()
            update_url = f"https://{MAILCHIMP_SERVER_PREFIX}.api.mailchimp.com/3.0/lists/{MAILCHIMP_LIST_ID}/members/{email_hash}"
            requests.patch(
                update_url,
                auth=("anystring", MAILCHIMP_API_KEY),
                json={"merge_fields": {"FNAME": first_name}, "tags": mailchimp_tags}
            )
            
            # Actualizar etiquetas
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
        # Extraer nombre del campo nombre_cargo
        nombre = nombre_cargo.split(",")[0].strip() if "," in nombre_cargo else nombre_cargo.split()[0]
        
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "Tu Documento Maestro de Contexto para IA está listo"
        msg["From"] = f"Fedor Sawoloka <{GMAIL_USER}>"
        msg["To"] = recipient_email
        
        # Convertir el documento markdown a HTML básico
        doc_html = document.replace("\n\n", "</p><p>").replace("\n", "<br>")
        doc_html = doc_html.replace("## ", "<h2>").replace("# ", "<h1>")
        # Cerrar etiquetas h1/h2 correctamente
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
                <p><strong>Instrucciones:</strong> Copia el texto del documento y pégalo al inicio de cualquier conversación con ChatGPT, Claude, Gemini u otra IA. A partir de ese momento, la IA te responderá como si te conociera de siempre.</p>
                <hr style="border: 1px solid #dee2e6; margin: 20px 0;">
                {doc_html_clean}
                <hr style="border: 1px solid #dee2e6; margin: 20px 0;">
                <p style="font-size: 12px; color: #6c757d;">Este documento fue generado en <a href="https://yosoyelruso.com/configura-tu-ia/" style="color: #FF8C42;">yosoyelruso.com/configura-tu-ia</a></p>
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
    return {"status": "ok", "service": "Configura tu IA - Backend"}

@app.get("/health")
def health():
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

@app.post("/generate", response_model=GenerateResponse)
async def generate(data: FormData):
    """Endpoint principal: genera el documento y guarda los datos."""
    
    # 1. Clasificar perfil (etiquetado inteligente)
    tags = classify_profile(data)
    
    # 2. Ejecutar las 3 acciones en paralelo
    document = None
    fallback_used = False
    
    # Intentar generar con OpenAI
    try:
        document = generate_document_openai(data)
    except Exception as e:
        print(f"OpenAI falló: {e}")
        fallback_used = True
        document = generate_document_fallback(data)
    
    # Guardar en Google Sheets (no bloquea si falla)
    try:
        save_to_google_sheets(data, tags)
    except Exception as e:
        print(f"Google Sheets falló: {e}")
    
    # Suscribir en Mailchimp (no bloquea si falla)
    try:
        subscribe_to_mailchimp(data, tags)
    except Exception as e:
        print(f"Mailchimp falló: {e}")
    
    # Enviar documento por email
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
