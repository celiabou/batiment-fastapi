import json
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from db import init_db, insert_lead
from pricing import estimate_from_text

app = FastAPI()
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

services = json.load(open("content/services.json"))
cities = json.load(open("content/cities.json"))


@app.on_event("startup")
def startup():
    init_db()


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("home.html", {"request": request})


@app.get("/services", response_class=HTMLResponse)
def list_services(request: Request):
    return templates.TemplateResponse(
        "list_services.html", {"request": request, "services": services}
    )


@app.get("/services/{slug}", response_class=HTMLResponse)
def service_page(request: Request, slug: str):
    service = next((s for s in services if s["slug"] == slug), None)
    return templates.TemplateResponse(
        "service.html", {"request": request, "service": service}
    )


@app.get("/zones", response_class=HTMLResponse)
def list_cities(request: Request):
    return templates.TemplateResponse("list_cities.html", {"request": request, "cities": cities})


@app.get("/zones/{city}", response_class=HTMLResponse)
def city_page(request: Request, city: str):
    return templates.TemplateResponse("city.html", {"request": request, "city": city})


@app.get("/contact", response_class=HTMLResponse)
def contact(request: Request):
    return templates.TemplateResponse("contact.html", {"request": request})


@app.post("/api/lead")
def lead(
    name: str = Form(...),
    phone: str = Form(...),
    email: str = Form(...),
    message: str = Form(...),
):
    insert_lead(name, phone, email, message)
    return RedirectResponse("/contact", status_code=303)


@app.post("/api/chat")
async def chat(request: Request):
    data = await request.json()
    messages = data.get("messages", [])
    last = messages[-1]["content"] if messages else ""

    est = estimate_from_text(last)

    if est.get("confidence", 0) > 0.5:
        reply = (
            f"Estimation: {est['low']}€ – {est['high']}€. Donnez votre téléphone pour un devis précis."
        )
    else:
        reply = "Quel type de travaux, quelle surface et quelle ville ?"

    return JSONResponse({"reply": reply})
