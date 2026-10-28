import os
import json
import csv
import io
import time
import uuid
import sqlite3
import threading
import requests
from flask import Flask, request, jsonify, render_template_string, Response, send_file

app = Flask(__name__)
DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "osm_jobs.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            name TEXT,
            city TEXT,
            categories TEXT,
            created_at TEXT,
            status TEXT,
            total_items INTEGER DEFAULT 0,
            error_message TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS places (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT,
            osm_id TEXT,
            osm_type TEXT,
            name TEXT,
            category TEXT,
            type TEXT,
            latitude REAL,
            longitude REAL,
            address TEXT,
            street TEXT,
            house_number TEXT,
            postcode TEXT,
            city TEXT,
            phone TEXT,
            email TEXT,
            website TEXT,
            opening_hours TEXT,
            cuisine TEXT,
            brand TEXT,
            wheelchair TEXT,
            raw_data TEXT,
            FOREIGN KEY (job_id) REFERENCES jobs(id)
        )
    """)
    conn.commit()
    conn.close()

init_db()

HEADERS = {
    "User-Agent": "OSM-POI-Extractor/2.0 (contact: admin@local-maps.dev)",
    "Content-Type": "application/x-www-form-urlencoded"
}

OVERPASS_SERVERS = [
    "https://overpass.openstreetmap.fr/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter"
]

def query_nominatim_boundary(city_name):
    clean_name = city_name.split(",")[0].strip()
    url = f"https://nominatim.openstreetmap.org/search?q={requests.utils.quote(clean_name + ', Italia')}&format=json&limit=5"
    headers = {"User-Agent": "OSM-LeadCollector/2.0 (admin@local-maps.dev)"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.ok and len(resp.json()) > 0:
            items = resp.json()
            # Prefer items that have postal code or are smaller city bboxes
            best_item = items[0]
            for it in items:
                # If it's a city/town rather than whole province
                if "postcode" in it.get("display_name", "") or it.get("type") in ["city", "town", "administrative"]:
                    best_item = it
                    break

            bbox = best_item.get("boundingbox") # [lat_min, lat_max, lon_min, lon_max]
            if bbox:
                return {
                    "lat_min": float(bbox[0]),
                    "lat_max": float(bbox[1]),
                    "lon_min": float(bbox[2]),
                    "lon_max": float(bbox[3]),
                    "display_name": best_item.get("display_name"),
                    "clean_name": clean_name
                }
    except Exception:
        pass
    return None

def fetch_overpass_places(city_name, bbox, categories, limit=1000000000):
    clean_name = city_name.split(",")[0].strip()
    cats_regex = "|".join([c.strip().lower() for c in categories if c.strip()])
    if not cats_regex:
        cats_regex = "restaurant|cafe|bar"

    # Limit clause for Overpass: if 1 billion or 0/negative, no limit clause is applied
    limit_clause = f" {limit}" if limit and limit < 1000000000 else ""

    if bbox:
        lat_min = bbox['lat_min']
        lon_min = bbox['lon_min']
        lat_max = bbox['lat_max']
        lon_max = bbox['lon_max']
        query = f"""[out:json][timeout:60];
        (
          node["amenity"~"{cats_regex}"]({lat_min},{lon_min},{lat_max},{lon_max});
          way["amenity"~"{cats_regex}"]({lat_min},{lon_min},{lat_max},{lon_max});
          relation["amenity"~"{cats_regex}"]({lat_min},{lon_min},{lat_max},{lon_max});
          node["tourism"~"{cats_regex}"]({lat_min},{lon_min},{lat_max},{lon_max});
          way["tourism"~"{cats_regex}"]({lat_min},{lon_min},{lat_max},{lon_max});
          node["shop"~"{cats_regex}"]({lat_min},{lon_min},{lat_max},{lon_max});
          way["shop"~"{cats_regex}"]({lat_min},{lon_min},{lat_max},{lon_max});
        );
        out center tags{limit_clause};
        """
    else:
        query = f"""[out:json][timeout:60];
        (
          node["amenity"~"{cats_regex}"]["addr:city"~"{clean_name}",i];
          way["amenity"~"{cats_regex}"]["addr:city"~"{clean_name}",i];
          relation["amenity"~"{cats_regex}"]["addr:city"~"{clean_name}",i];
          node["tourism"~"{cats_regex}"]["addr:city"~"{clean_name}",i];
          way["tourism"~"{cats_regex}"]["addr:city"~"{clean_name}",i];
          node["shop"~"{cats_regex}"]["addr:city"~"{clean_name}",i];
          way["shop"~"{cats_regex}"]["addr:city"~"{clean_name}",i];
        );
        out center tags{limit_clause};
        """

    for server in OVERPASS_SERVERS:
        try:
            resp = requests.post(server, data={"data": query}, headers=HEADERS, timeout=45)
            if resp.ok:
                data = resp.json()
                elements = data.get("elements", [])
                if elements:
                    return elements
        except Exception:
            continue
    return None

def fetch_photon_fallback(city_name, categories, limit=200):
    places = []
    seen = set()
    clean_name = city_name.split(",")[0].strip()

    # Get coordinates of city first via photon
    center_lat, center_lon = 45.4384, 10.9916 # default
    try:
        geo_r = requests.get(f"https://photon.komoot.io/api/?q={requests.utils.quote(clean_name)}&limit=1", headers={"User-Agent": "OSM-POI-Extractor/2.0"}, timeout=10)
        if geo_r.ok and geo_r.json().get("features"):
            coords = geo_r.json()["features"][0]["geometry"]["coordinates"]
            center_lon, center_lat = coords[0], coords[1]
    except Exception:
        pass

    for cat in categories:
        cat = cat.strip()
        if not cat:
            continue
        try:
            url = f"https://photon.komoot.io/api/?q={requests.utils.quote(cat)}&lat={center_lat}&lon={center_lon}&limit={min(limit, 100)}"
            resp = requests.get(url, headers={"User-Agent": "OSM-POI-Extractor/2.0"}, timeout=12)
            if resp.ok:
                features = resp.json().get("features", [])
                for f in features:
                    props = f.get("properties", {})
                    oid = f"{props.get('osm_type')}_{props.get('osm_id')}"
                    if oid not in seen:
                        seen.add(oid)
                        coords = f.get("geometry", {}).get("coordinates", [0, 0])
                        places.append({
                            "osm_id": props.get("osm_id"),
                            "osm_type": props.get("osm_type"),
                            "name": props.get("name") or cat,
                            "category": props.get("osm_value") or props.get("osm_key") or cat,
                            "type": props.get("type", ""),
                            "lat": coords[1],
                            "lon": coords[0],
                            "street": props.get("street", ""),
                            "housenumber": props.get("housenumber", ""),
                            "postcode": props.get("postcode", ""),
                            "city": props.get("city", clean_name),
                            "country": props.get("country", ""),
                            "raw": props
                        })
            time.sleep(0.3)
        except Exception:
            pass
    return places

def run_osm_scrape(job_id, city, categories_str, limit=1000000000):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    try:
        categories = [c.strip() for c in categories_str.split(",") if c.strip()]
        bbox = query_nominatim_boundary(city)

        elements = fetch_overpass_places(city, bbox, categories, limit=limit)

        records = []
        if elements:
            for el in elements:
                tags = el.get("tags") or {}
                lat = el.get("lat") or (el.get("center", {}).get("lat") if el.get("center") else None)
                lon = el.get("lon") or (el.get("center", {}).get("lon") if el.get("center") else None)
                name = tags.get("name")
                if not name:
                    continue

                category = tags.get("amenity") or tags.get("tourism") or tags.get("shop") or "poi"
                street = tags.get("addr:street", "")
                housenumber = tags.get("addr:housenumber", "")
                postcode = tags.get("addr:postcode", "")
                c_city = tags.get("addr:city", city)
                addr_parts = [p for p in [street, housenumber, postcode, c_city] if p]
                address = ", ".join(addr_parts) if addr_parts else ""

                phone = tags.get("phone") or tags.get("contact:phone") or ""
                email = tags.get("email") or tags.get("contact:email") or ""
                website = tags.get("website") or tags.get("contact:website") or ""
                hours = tags.get("opening_hours", "")
                cuisine = tags.get("cuisine", "")
                brand = tags.get("brand", "")
                wheelchair = tags.get("wheelchair", "")

                records.append((
                    job_id,
                    str(el.get("id")),
                    el.get("type", "node"),
                    name,
                    category,
                    category,
                    lat,
                    lon,
                    address,
                    street,
                    housenumber,
                    postcode,
                    c_city,
                    phone,
                    email,
                    website,
                    hours,
                    cuisine,
                    brand,
                    wheelchair,
                    json.dumps(tags)
                ))
        else:
            # Fallback via Photon / Komoot
            photon_items = fetch_photon_fallback(city, categories, limit=limit)
            for item in photon_items:
                name = item.get("name")
                category = item.get("category")
                street = item.get("street", "")
                housenumber = item.get("housenumber", "")
                postcode = item.get("postcode", "")
                c_city = item.get("city", city)
                addr_parts = [p for p in [street, housenumber, postcode, c_city] if p]
                address = ", ".join(addr_parts) if addr_parts else ""

                records.append((
                    job_id,
                    str(item.get("osm_id")),
                    str(item.get("osm_type", "node")),
                    name,
                    category,
                    item.get("type", ""),
                    float(item.get("lat", 0)),
                    float(item.get("lon", 0)),
                    address,
                    street,
                    housenumber,
                    postcode,
                    c_city,
                    "", # phone
                    "", # email
                    "", # website
                    "", # hours
                    "", # cuisine
                    "", # brand
                    "", # wheelchair
                    json.dumps(item.get("raw", {}))
                ))

        cur.executemany("""
            INSERT INTO places (
                job_id, osm_id, osm_type, name, category, type, latitude, longitude,
                address, street, house_number, postcode, city, phone, email, website,
                opening_hours, cuisine, brand, wheelchair, raw_data
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, records)

        cur.execute("UPDATE jobs SET status = 'completed', total_items = ? WHERE id = ?", (len(records), job_id))
        conn.commit()

        # Export JSON and CSV
        export_job_files(job_id)

    except Exception as e:
        cur.execute("UPDATE jobs SET status = 'failed', error_message = ? WHERE id = ?", (str(e), job_id))
        conn.commit()
    finally:
        conn.close()

def export_job_files(job_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT * FROM places WHERE job_id = ?", (job_id,))
    cols = [col[0] for col in cur.description]
    rows = cur.fetchall()
    conn.close()

    data = []
    for r in rows:
        row_dict = dict(zip(cols, r))
        if "raw_data" in row_dict:
            try:
                row_dict["raw_data"] = json.loads(row_dict["raw_data"])
            except Exception:
                pass
        data.append(row_dict)

    json_path = os.path.join(DATA_DIR, f"{job_id}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    csv_path = os.path.join(DATA_DIR, f"{job_id}.csv")
    csv_cols = [c for c in cols if c != "raw_data"]
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=csv_cols, extrasaction="ignore")
        writer.writeheader()
        for d in data:
            writer.writerow(d)

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route("/api/v1/jobs", methods=["GET"])
def list_jobs():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, name, city, categories, created_at, status, total_items, error_message FROM jobs ORDER BY created_at DESC")
    jobs = []
    for row in cur.fetchall():
        jobs.append({
            "id": row[0],
            "name": row[1],
            "city": row[2],
            "categories": row[3],
            "created_at": row[4],
            "status": row[5],
            "total_items": row[6],
            "error_message": row[7]
        })
    conn.close()
    return jsonify(jobs)

@app.route("/api/v1/jobs", methods=["POST"])
def create_job():
    data = request.json or {}
    city = data.get("city", "").strip()
    categories = data.get("categories", "restaurant,cafe,bar").strip()
    name = data.get("name", "").strip() or f"OSM - {city} ({categories})"
    limit = int(data.get("limit", 1000000000))

    if not city:
        return jsonify({"error": "City / Area is required"}), 400

    job_id = str(uuid.uuid4())[:8]
    created_at = time.strftime("%Y-%m-%d %H:%M:%S")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO jobs (id, name, city, categories, created_at, status, total_items)
        VALUES (?, ?, ?, ?, ?, 'running', 0)
    """, (job_id, name, city, categories, created_at))
    conn.commit()
    conn.close()

    t = threading.Thread(target=run_osm_scrape, args=(job_id, city, categories, limit), daemon=True)
    t.start()

    return jsonify({"id": job_id, "status": "running"})

@app.route("/api/v1/jobs/<job_id>", methods=["GET"])
def get_job(job_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    job_row = cur.fetchone()
    if not job_row:
        conn.close()
        return jsonify({"error": "Not found"}), 404

    cur.execute("SELECT * FROM places WHERE job_id = ?", (job_id,))
    cols = [col[0] for col in cur.description]
    places = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()

    return jsonify({
        "id": job_row[0],
        "name": job_row[1],
        "city": job_row[2],
        "categories": job_row[3],
        "created_at": job_row[4],
        "status": job_row[5],
        "total_items": job_row[6],
        "error_message": job_row[7],
        "places": places
    })

@app.route("/api/v1/jobs/<job_id>/download/<fmt>", methods=["GET"])
def download_job(job_id, fmt):
    filename = f"{job_id}.{fmt}"
    file_path = os.path.join(DATA_DIR, filename)
    if not os.path.exists(file_path):
        export_job_files(job_id)
    if os.path.exists(file_path):
        return send_file(file_path, as_attachment=True, download_name=f"osm_places_{filename}")
    return jsonify({"error": "File not found"}), 404

@app.route("/api/v1/jobs/<job_id>", methods=["DELETE"])
def delete_job(job_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM places WHERE job_id = ?", (job_id,))
    cur.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    conn.commit()
    conn.close()
    for ext in ["csv", "json"]:
        p = os.path.join(DATA_DIR, f"{job_id}.{ext}")
        if os.path.exists(p):
            os.remove(p)
    return jsonify({"success": True})

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="it">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OpenStreetMap Scraper</title>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css" />
    <link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css" />
    <style>
        :root {
            --bg-color: #0b0f19;
            --surface-color: #121826;
            --card-color: #182234;
            --border-color: #24324a;
            --primary: #10b981;
            --primary-hover: #059669;
            --text-main: #f3f4f6;
            --text-muted: #9ca3af;
            --accent-blue: #3b82f6;
            --accent-danger: #ef4444;
            --radius-md: 10px;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Plus Jakarta Sans', sans-serif; }
        body { background: var(--bg-color); color: var(--text-main); min-height: 100vh; padding: 24px; }
        .container { max-width: 1200px; margin: 0 auto; }
        
        .header { display: flex; align-items: center; justify-content: space-between; padding-bottom: 24px; border-bottom: 1px solid var(--border-color); margin-bottom: 24px; }
        .logo-area { display: flex; align-items: center; gap: 14px; }
        .logo-icon { width: 44px; height: 44px; background: rgba(16, 185, 129, 0.15); border-radius: 10px; display: flex; align-items: center; justify-content: center; font-size: 22px; color: var(--primary); border: 1px solid rgba(16, 185, 129, 0.3); }
        .title-block h1 { font-size: 22px; font-weight: 700; color: #fff; }
        .title-block p { font-size: 13px; color: var(--text-muted); }

        .grid-layout { display: grid; grid-template-columns: 360px 1fr; gap: 24px; }
        @media (max-width: 900px) { .grid-layout { grid-template-columns: 1fr; } }

        .card { background: var(--surface-color); border: 1px solid var(--border-color); border-radius: var(--radius-md); padding: 20px; }
        .card-header { font-size: 16px; font-weight: 600; margin-bottom: 16px; display: flex; align-items: center; gap: 8px; }

        .form-group { margin-bottom: 16px; }
        .form-group label { display: block; font-size: 13px; font-weight: 500; margin-bottom: 6px; color: var(--text-muted); }
        .form-control { width: 100%; background: var(--card-color); border: 1px solid var(--border-color); color: #fff; padding: 10px 14px; border-radius: 8px; font-size: 14px; }
        .form-control:focus { outline: none; border-color: var(--primary); }

        .btn { display: inline-flex; align-items: center; justify-content: center; gap: 8px; padding: 10px 16px; font-size: 14px; font-weight: 600; border-radius: 8px; border: none; cursor: pointer; transition: 0.2s; text-decoration: none; }
        .btn-primary { background: var(--primary); color: #fff; width: 100%; }
        .btn-primary:hover { background: var(--primary-hover); }
        .btn-secondary { background: var(--card-color); color: var(--text-main); border: 1px solid var(--border-color); }
        .btn-secondary:hover { background: var(--border-color); }
        .btn-sm { padding: 6px 10px; font-size: 12px; }
        .btn-danger { background: rgba(239, 68, 68, 0.15); color: var(--accent-danger); border: 1px solid rgba(239, 68, 68, 0.3); }
        .btn-danger:hover { background: var(--accent-danger); color: #fff; }

        .job-item { background: var(--card-color); border: 1px solid var(--border-color); border-radius: 8px; padding: 14px; margin-bottom: 12px; }
        .job-item-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 8px; }
        .job-title { font-weight: 600; font-size: 15px; color: #fff; }
        .job-meta { font-size: 12px; color: var(--text-muted); display: flex; gap: 12px; margin-bottom: 12px; }
        .job-actions { display: flex; gap: 8px; flex-wrap: wrap; }

        .badge { display: inline-block; padding: 2px 8px; border-radius: 6px; font-size: 11px; font-weight: 600; text-transform: uppercase; }
        .badge-running { background: rgba(59, 130, 246, 0.2); color: var(--accent-blue); }
        .badge-completed { background: rgba(16, 185, 129, 0.2); color: var(--primary); }
        .badge-failed { background: rgba(239, 68, 68, 0.2); color: var(--accent-danger); }

        /* Modal */
        .modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.7); backdrop-filter: blur(4px); display: none; align-items: center; justify-content: center; z-index: 1000; padding: 20px; }
        .modal-overlay.active { display: flex; }
        .modal-card { background: var(--surface-color); border: 1px solid var(--border-color); border-radius: 12px; width: 100%; max-width: 1000px; max-height: 90vh; display: flex; flex-direction: column; overflow: hidden; }
        .modal-header { padding: 16px 20px; border-bottom: 1px solid var(--border-color); display: flex; justify-content: space-between; align-items: center; }
        .modal-body { padding: 20px; overflow-y: auto; flex: 1; }
        #modalMap { height: 350px; border-radius: 8px; margin-bottom: 16px; border: 1px solid var(--border-color); }
        .table-wrap { overflow-x: auto; }
        table { width: 100%; border-collapse: collapse; font-size: 13px; text-align: left; }
        th, td { padding: 10px 12px; border-bottom: 1px solid var(--border-color); }
        th { color: var(--text-muted); font-weight: 600; background: var(--card-color); }
    </style>
</head>
<body>
    <div class="container">
        <header class="header">
            <div class="logo-area">
                <div class="logo-icon">🌍</div>
                <div class="title-block">
                    <h1>OpenStreetMap Scraper</h1>
                </div>
            </div>
        </header>

        <div class="grid-layout">
            <!-- Form Area -->
            <div class="card">
                <div class="card-header">📍 Nuova Ricerca OSM</div>
                <form id="scrapeForm" onsubmit="startJob(event)">
                    <div class="form-group">
                        <label>Città / Località</label>
                        <input type="text" id="cityInput" class="form-control" placeholder="Es. Verona, Milano, Roma" required>
                    </div>
                    <div class="form-group">
                        <label>Categorie OSM (separate da virgola)</label>
                        <input type="text" id="categoriesInput" class="form-control" value="restaurant,cafe,bar,hotel" placeholder="restaurant,cafe,bar,hotel,pub,pharmacy">
                        <small style="color:var(--text-muted); font-size:11px; margin-top:4px; display:block;">Tag OSM comuni: restaurant, cafe, bar, hotel, supermarket, bakery, pharmacy</small>
                    </div>
                    <div class="form-group">
                        <label>Limite Risultati</label>
                        <input type="number" id="limitInput" class="form-control" value="1000000000" min="1" max="1000000000">
                    </div>
                    <button type="submit" id="submitBtn" class="btn btn-primary">⚡ Avvia Estrazione OSM</button>
                </form>
            </div>

            <!-- Jobs List -->
            <div class="card">
                <div class="card-header" style="justify-content: space-between;">
                    <span>📋 Lavori OSM Salvati</span>
                    <button class="btn btn-secondary btn-sm" onclick="loadJobs()">🔄 Aggiorna</button>
                </div>
                <div id="jobsList">
                    <p style="color:var(--text-muted); font-size:14px;">Caricamento lavori...</p>
                </div>
            </div>
        </div>
    </div>

    <!-- Modal View -->
    <div class="modal-overlay" id="viewModal">
        <div class="modal-card">
            <div class="modal-header">
                <h3 id="modalTitle">Dettaglio Risultati</h3>
                <button class="btn btn-secondary btn-sm" onclick="closeModal()">✕ Chiudi</button>
            </div>
            <div class="modal-body">
                <div id="modalMap"></div>
                <div class="table-wrap">
                    <table>
                        <thead>
                            <tr>
                                <th>Nome</th>
                                <th>Categoria</th>
                                <th>Indirizzo</th>
                                <th>Telefono</th>
                                <th>Sito Web</th>
                            </tr>
                        </thead>
                        <tbody id="modalTableBody"></tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>

    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script src="https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js"></script>
    <script>
        let mapInstance = null;
        let clusterGroup = null;

        async function loadJobs() {
            try {
                const res = await fetch('/api/v1/jobs');
                const jobs = await res.json();
                const container = document.getElementById('jobsList');
                if (!jobs.length) {
                    container.innerHTML = '<p style="color:var(--text-muted); font-size:14px;">Nessun lavoro trovato. Esegui la prima estrazione!</p>';
                    return;
                }
                container.innerHTML = jobs.map(j => `
                    <div class="job-item">
                        <div class="job-item-header">
                            <span class="job-title">${j.name}</span>
                            <span class="badge badge-${j.status}">${j.status}</span>
                        </div>
                        <div class="job-meta">
                            <span>📅 ${j.created_at}</span>
                            <span>📍 ${j.city}</span>
                            <span>🏷️ ${j.categories}</span>
                            <span>📊 ${j.total_items} POI</span>
                        </div>
                        <div class="job-actions">
                            <button class="btn btn-secondary btn-sm" onclick="viewJob('${j.id}')">👁️ Visualizza & Mappa</button>
                            <a class="btn btn-secondary btn-sm" href="/api/v1/jobs/${j.id}/download/csv">📥 Scarica CSV</a>
                            <a class="btn btn-secondary btn-sm" href="/api/v1/jobs/${j.id}/download/json">📥 Scarica JSON</a>
                            <button class="btn btn-danger btn-sm" onclick="deleteJob('${j.id}')">🗑️ Elimina</button>
                        </div>
                    </div>
                `).join('');
            } catch(e) {
                console.error(e);
            }
        }

        async function startJob(e) {
            e.preventDefault();
            const btn = document.getElementById('submitBtn');
            btn.disabled = true;
            btn.innerText = "⏳ Avvio in corso...";

            const payload = {
                city: document.getElementById('cityInput').value,
                categories: document.getElementById('categoriesInput').value,
                limit: parseInt(document.getElementById('limitInput').value) || 1000000000
            };

            try {
                const res = await fetch('/api/v1/jobs', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(payload)
                });
                if (res.ok) {
                    await loadJobs();
                } else {
                    alert('Errore creazione job');
                }
            } catch(err) {
                alert('Errore di connessione: ' + err.message);
            } finally {
                btn.disabled = false;
                btn.innerText = "⚡ Avvia Estrazione OSM";
            }
        }

        async function viewJob(jobId) {
            const res = await fetch(`/api/v1/jobs/${jobId}`);
            if (!res.ok) return;
            const data = await res.json();
            
            document.getElementById('modalTitle').innerText = `${data.name} (${data.places.length} POI estratti)`;
            
            // Render first 200 places in table to prevent DOM freeze with 5000+ items
            const placesToRender = data.places.slice(0, 200);
            const tbody = document.getElementById('modalTableBody');
            tbody.innerHTML = placesToRender.map(p => `
                <tr>
                    <td style="font-weight:600;">${p.name || '-'}</td>
                    <td><span class="badge" style="background:#24324a;">${p.category || '-'}</span></td>
                    <td style="color:var(--text-muted);">${p.address || (p.street ? p.street + ' ' + (p.house_number || '') : '-')}</td>
                    <td>${p.phone ? `<a href="tel:${p.phone}" style="color:var(--primary); text-decoration:none;">${p.phone}</a>` : '-'}</td>
                    <td>${p.website ? `<a href="${p.website}" target="_blank" style="color:var(--accent-blue); text-decoration:none;">Sito Web ↗</a>` : '-'}</td>
                </tr>
            `).join('');

            if (data.places.length > 200) {
                tbody.innerHTML += `<tr><td colspan="5" style="text-align:center; color:var(--text-muted); padding:12px;">Mostrati primi 200 di ${data.places.length} POI nella tabella. Scarica CSV/JSON per tutti i dati completi.</td></tr>`;
            }

            document.getElementById('viewModal').classList.add('active');

            setTimeout(() => {
                if (!mapInstance) {
                    mapInstance = L.map('modalMap', {
                        preferCanvas: true
                    }).setView([45.4384, 10.9916], 13);
                    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
                        attribution: '&copy; OpenStreetMap'
                    }).addTo(mapInstance);
                }

                if (clusterGroup) {
                    clusterGroup.clearLayers();
                    mapInstance.removeLayer(clusterGroup);
                }

                clusterGroup = L.markerClusterGroup({
                    chunkedLoading: true,
                    maxClusterRadius: 50,
                    spiderfyOnMaxZoom: true,
                    showCoverageOnHover: false,
                    zoomToBoundsOnClick: true
                });

                const markersList = [];
                for (let i = 0; i < data.places.length; i++) {
                    const p = data.places[i];
                    if (p.latitude && p.longitude) {
                        const m = L.circleMarker([p.latitude, p.longitude], {
                            radius: 6,
                            fillColor: "#10b981",
                            color: "#fff",
                            weight: 1,
                            opacity: 1,
                            fillOpacity: 0.8
                        }).bindPopup(`<b>${p.name}</b><br><span style="color:#666">${p.category || ''}</span><br>${p.address || ''}<br>${p.phone ? '📞 ' + p.phone : ''}`);
                        markersList.push(m);
                    }
                }

                clusterGroup.addLayers(markersList);
                mapInstance.addLayer(clusterGroup);

                if (markersList.length > 0) {
                    mapInstance.fitBounds(clusterGroup.getBounds(), { padding: [30, 30] });
                }
                mapInstance.invalidateSize();
            }, 100);
        }

        function closeModal() {
            document.getElementById('viewModal').classList.remove('active');
        }

        async function deleteJob(id) {
            if (!confirm('Sei sicuro di voler eliminare questo lavoro?')) return;
            await fetch(`/api/v1/jobs/${id}`, { method: 'DELETE' });
            loadJobs();
        }

        loadJobs();
        setInterval(loadJobs, 5000);
    </script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
