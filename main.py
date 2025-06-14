from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import os
import pandas as pd
import uuid

from dwg_logic import save_copy_with_changes, convert_dwg_to_dxf
from room_dimension import (
    extract_room_boundaries,
    extract_block_text_info,
    clean_text_value,
    check_text_within_room,
)
from db import add_history, get_all_history, clear_all_history, init_db

app = FastAPI()

UPLOAD_FOLDER = "uploads"
CONVERTED_FOLDER = "converted"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(CONVERTED_FOLDER, exist_ok=True)

app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/converted", StaticFiles(directory=CONVERTED_FOLDER), name="converted")

templates = Jinja2Templates(directory="templates")


@app.get("/", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/history", response_class=HTMLResponse)
async def view_history(request: Request):
    records = get_all_history()
    return templates.TemplateResponse("history.html", {
        "request": request,
        "records": records
    })


@app.post("/login", response_class=HTMLResponse)
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if username == "admin" and password == "admin":
        return RedirectResponse(url="/home", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "error": "Login failed"})


@app.get("/home", response_class=HTMLResponse)
async def home_page(request: Request):
    history_data = get_all_history()
    return templates.TemplateResponse("home.html", {
        "request": request,
        "history": history_data
    })


@app.get("/clear-history")
async def clear_history():
    clear_all_history()
    return RedirectResponse("/home", status_code=302)


@app.get("/self-check", response_class=HTMLResponse)
async def self_check(request: Request):
    return templates.TemplateResponse("result.html", {
        "request": request,
        "check_type": "Self Check",
        "filename": None,
        "rooms": None,
        "submitted_rooms": None,
        "matches": None,
        "download_link": None
    })


@app.get("/reference-check", response_class=HTMLResponse)
async def reference_check(request: Request):
    return templates.TemplateResponse("result.html", {
        "request": request,
        "check_type": "Reference Check",
        "filename": None,
        "rooms": None,
        "submitted_rooms": None,
        "matches": None,
        "download_link": None
    })


def feet_inches_to_mm(feet: int, inches: int) -> float:
    return round((feet * 12 + inches) * 25.4, 2)


def match_user_rooms_to_dxf(submitted_rooms, extracted_rooms):
    matched = []
    unmatched = []

    for user_room in submitted_rooms:
        is_matched = False
        for dxf_room in extracted_rooms:
            room_name_matches = any(
                user_room["name"] in t["cleaned"].lower()
                for t in dxf_room["texts"]
            )
            area_match = abs(user_room["width_mm"] * user_room["height_mm"] - dxf_room["Area"]) <= 100000

            if room_name_matches and area_match:
                matched.append({
                    "user_room": user_room,
                    "matched_room": dxf_room
                })
                is_matched = True
                break

        if not is_matched:
            unmatched.append(user_room)

    return matched, unmatched


def export_matches_to_excel(matches, unmatched, filepath="converted/full_report.xlsx"):
    data = []

    for match in matches:
        user = match["user_room"]
        dxf = match["matched_room"]
        data.append({
            "User Room Name": user["name"],
            "User Width (mm)": user["width_mm"],
            "User Height (mm)": user["height_mm"],
            "DXF Block": dxf.get("Block", ""),
            "DXF Length": dxf.get("Length", ""),
            "DXF Breadth": dxf.get("Breadth", ""),
            "DXF Area (mm²)": dxf.get("Area", ""),
            "Matched Room Name": next((t["cleaned"] for t in dxf.get("texts", []) if user["name"] in t["cleaned"].lower()), ""),
            "Match Found": "Yes"
        })

    for user in unmatched:
        data.append({
            "User Room Name": user["name"],
            "User Width (mm)": user["width_mm"],
            "User Height (mm)": user["height_mm"],
            "DXF Block": "",
            "DXF Length": "",
            "DXF Breadth": "",
            "DXF Area (mm²)": "",
            "Matched Room Name": "",
            "Match Found": "No"
        })

    df = pd.DataFrame(data)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    df.to_excel(filepath, index=False)
    return filepath


@app.post("/upload-{check_type}", response_class=HTMLResponse)
async def upload_dwg(
    request: Request,
    check_type: str,
    file: UploadFile = File(...),
    room_count: int = Form(...),
):
    form = await request.form()
    check_type_display = check_type.replace("-", " ").title()

    if not file.filename.lower().endswith(".dwg"):
        return templates.TemplateResponse("result.html", {
            "request": request,
            "check_type": check_type_display,
            "error": "Only .dwg files are supported.",
            "filename": None,
            "rooms": None,
            "submitted_rooms": None,
            "matches": None,
            "download_link": None
        })

    clean_name = file.filename.replace(" ", "_")
    dwg_filename = f"{uuid.uuid4().hex}_{clean_name}"
    dwg_path = os.path.join(UPLOAD_FOLDER, dwg_filename)
    with open(dwg_path, "wb") as f:
        f.write(await file.read())

    try:
        dxf_path = convert_dwg_to_dxf(dwg_path, CONVERTED_FOLDER)
    except Exception as e:
        return templates.TemplateResponse("result.html", {
            "request": request,
            "check_type": check_type_display,
            "error": f"Conversion failed: {str(e)}",
            "filename": None,
            "rooms": None,
            "submitted_rooms": None,
            "matches": None,
            "download_link": None
        })

    rooms = extract_room_boundaries(dxf_path)
    texts = extract_block_text_info(dxf_path)

    all_texts = []
    for blk_texts in texts.values():
        for text in blk_texts:
            text["original"] = text["Text"]
            text["cleaned"] = clean_text_value(text["Text"])
            all_texts.append(text)

    for room in rooms:
        room["texts"] = [t for t in all_texts if check_text_within_room(room, t)]

    submitted_rooms = []
    for i in range(1, room_count + 1):
        name = form.get(f"room_name_{i}", "").strip().lower()
        width_ft = int(form.get(f"width_feet_{i}", 0))
        width_in = int(form.get(f"width_inches_{i}", 0))
        height_ft = int(form.get(f"height_feet_{i}", 0))
        height_in = int(form.get(f"height_inches_{i}", 0))

        submitted_rooms.append({
            "name": name,
            "width_mm": feet_inches_to_mm(width_ft, width_in),
            "height_mm": feet_inches_to_mm(height_ft, height_in),
            "width_feet": width_ft,
            "width_inches": width_in,
            "height_feet": height_ft,
            "height_inches": height_in
        })

    matches, unmatched = match_user_rooms_to_dxf(submitted_rooms, rooms)

    excel_path = os.path.join(CONVERTED_FOLDER, "full_report.xlsx")
    export_matches_to_excel(matches, unmatched, excel_path)
    excel_download_link = f"/converted/full_report.xlsx"

    updated_filename = f"updated_{os.path.basename(dxf_path)}"
    updated_path = os.path.join(CONVERTED_FOLDER, updated_filename)
    save_copy_with_changes(dxf_path, updated_path, rooms, texts)

    download_link = f"/converted/{updated_filename}"
    
    add_history(
        check_type_display,
        file.filename,
        os.path.basename(updated_path),
        os.path.basename(excel_path)
    )

    return templates.TemplateResponse("result.html", {
        "request": request,
        "check_type": check_type_display,
        "filename": file.filename,
        "rooms": rooms,
        "submitted_rooms": submitted_rooms,
        "matches": matches,
        "download_link": download_link,
        "excel_link": excel_download_link
    })


@app.get("/download/{filename}")
async def download_file(filename: str):
    path = os.path.join(CONVERTED_FOLDER, filename)
    if os.path.exists(path):
        return FileResponse(path=path, filename=filename, media_type="application/octet-stream")
    return HTMLResponse(f"<h3>File not found: {filename}</h3>", status_code=404)


from db import init_db
init_db()
