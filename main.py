import os
from typing import List, Optional, Dict, Any
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from geopy.geocoders import Nominatim
from geopy.distance import geodesic
import networkx as nx
from database import db, create_document, get_documents

app = FastAPI(title="EV Charging Optimizer API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------
# Models
# -----------------------------
class Station(BaseModel):
    id: Optional[str] = None
    name: str
    latitude: float
    longitude: float
    charger_type: str = Field(description="e.g., CCS, CHAdeMO, Type2")
    power_kw: float = 50
    price_per_kwh: Optional[float] = None
    availability: Optional[str] = Field(default="unknown")
    city: Optional[str] = None

class OptimizeRequest(BaseModel):
    origin: str
    max_distance_km: float = 50
    preferred_charger: Optional[str] = None

class OptimizeResponse(BaseModel):
    origin: Dict[str, float]
    best_station: Station
    distance_km: float
    eta_minutes: float
    route_polyline: List[List[float]]
    candidates: List[Dict[str, Any]]

class ChatMessage(BaseModel):
    message: str

# -----------------------------
# Utilities
# -----------------------------
geolocator = Nominatim(user_agent="ev_optimizer")


def geocode_address(query: str) -> Optional[Dict[str, float]]:
    try:
        loc = geolocator.geocode(query)
        if not loc:
            return None
        return {"lat": float(loc.latitude), "lon": float(loc.longitude)}
    except Exception:
        return None


def haversine_km(a: Dict[str, float], b: Dict[str, float]) -> float:
    return geodesic((a["lat"], a["lon"]), (b["lat"], b["lon"])).km


def ensure_seed_stations():
    if db is None:
        return
    existing = db["station"].count_documents({})
    if existing > 0:
        return
    # Minimal, real-world-like seed entries (lat/lon approximate public locations)
    seeds = [
        {
            "name": "IONITY Berlin",
            "latitude": 52.5208,
            "longitude": 13.4095,
            "charger_type": "CCS",
            "power_kw": 150,
            "price_per_kwh": 0.79,
            "availability": "available",
            "city": "Berlin",
        },
        {
            "name": "Tesla Supercharger Munich",
            "latitude": 48.1371,
            "longitude": 11.5754,
            "charger_type": "CCS",
            "power_kw": 250,
            "price_per_kwh": 0.68,
            "availability": "busy",
            "city": "Munich",
        },
        {
            "name": "Tata Power Mumbai",
            "latitude": 19.0760,
            "longitude": 72.8777,
            "charger_type": "Type2",
            "power_kw": 30,
            "price_per_kwh": 0.22,
            "availability": "available",
            "city": "Mumbai",
        },
        {
            "name": "Delhi EV Hub",
            "latitude": 28.6139,
            "longitude": 77.2090,
            "charger_type": "CHAdeMO",
            "power_kw": 50,
            "price_per_kwh": 0.19,
            "availability": "available",
            "city": "Delhi",
        },
        {
            "name": "London FastCharge",
            "latitude": 51.5074,
            "longitude": -0.1278,
            "charger_type": "CCS",
            "power_kw": 120,
            "price_per_kwh": 0.45,
            "availability": "available",
            "city": "London",
        },
    ]
    for s in seeds:
        create_document("station", s)


@app.on_event("startup")
def startup_event():
    try:
        ensure_seed_stations()
    except Exception:
        # Database may not be configured; continue without seeding
        pass


@app.get("/")
def read_root():
    return {"message": "EV Charging Optimizer Backend"}


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": [],
    }
    try:
        from database import db as _db
        if _db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
            response["database_name"] = _db.name
            response["connection_status"] = "Connected"
            try:
                response["collections"] = _db.list_collection_names()
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️ Connected but Error: {str(e)[:80]}"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:80]}"
    return response


@app.get("/api/geocode")
def api_geocode(q: str = Query(..., description="Address or place to geocode")):
    coords = geocode_address(q)
    if not coords:
        raise HTTPException(status_code=404, detail="Location not found")
    return coords


@app.get("/api/stations", response_model=List[Station])
def list_stations(city: Optional[str] = None, charger: Optional[str] = None):
    if db is None:
        # Fallback in-memory seed if no DB
        data = [
            Station(name="Sample Station", latitude=37.7749, longitude=-122.4194, charger_type="CCS", power_kw=100)
        ]
        return data
    q: Dict[str, Any] = {}
    if city:
        q["city"] = {"$regex": f"^{city}$", "$options": "i"}
    if charger:
        q["charger_type"] = {"$regex": f"^{charger}$", "$options": "i"}
    docs = get_documents("station", q, limit=200)
    results: List[Station] = []
    for d in docs:
        d["id"] = str(d.get("_id"))
        d.pop("_id", None)
        results.append(Station(**d))
    return results


@app.post("/api/optimize", response_model=OptimizeResponse)
def optimize_route(req: OptimizeRequest):
    origin_coords = geocode_address(req.origin)
    if not origin_coords:
        raise HTTPException(status_code=404, detail="Origin not found")

    # Fetch stations
    stations = list_stations()
    if req.preferred_charger:
        stations = [s for s in stations if s.charger_type.lower() == req.preferred_charger.lower()]
    if not stations:
        raise HTTPException(status_code=404, detail="No stations available for the given filter")

    # Filter by distance and build candidate list
    candidates: List[Dict[str, Any]] = []
    for s in stations:
        d_km = haversine_km(origin_coords, {"lat": s.latitude, "lon": s.longitude})
        if d_km <= req.max_distance_km:
            candidates.append({"station": s, "distance_km": d_km})

    if not candidates:
        # pick globally nearest if none within radius
        stations_with_dist = [
            {"station": s, "distance_km": haversine_km(origin_coords, {"lat": s.latitude, "lon": s.longitude})}
            for s in stations
        ]
        candidates = sorted(stations_with_dist, key=lambda x: x["distance_km"])[:5]
    else:
        candidates = sorted(candidates, key=lambda x: x["distance_km"])[:5]

    # Simple graph with origin and candidates fully connected using distance as weight
    G = nx.Graph()
    G.add_node("origin", **origin_coords)
    for idx, c in enumerate(candidates):
        s = c["station"]
        G.add_node(s.name, lat=s.latitude, lon=s.longitude)
        # connect origin to station
        G.add_edge(
            "origin",
            s.name,
            weight=c["distance_km"],
        )
    # Best path is simply the minimum weight edge here
    best = min(candidates, key=lambda x: x["distance_km"])  # Dijkstra trivial on star graph
    best_station: Station = best["station"]

    # Build a straight-line polyline from origin to station for visualization
    route_polyline = [
        [origin_coords["lat"], origin_coords["lon"]],
        [best_station.latitude, best_station.longitude],
    ]

    # Rough ETA assuming average 60 km/h
    eta_minutes = (best["distance_km"] / 60) * 60

    resp = OptimizeResponse(
        origin=origin_coords,
        best_station=best_station,
        distance_km=round(best["distance_km"], 2),
        eta_minutes=round(eta_minutes, 1),
        route_polyline=route_polyline,
        candidates=[
            {
                "name": c["station"].name,
                "latitude": c["station"].latitude,
                "longitude": c["station"].longitude,
                "distance_km": round(c["distance_km"], 2),
                "charger_type": c["station"].charger_type,
                "power_kw": c["station"].power_kw,
            }
            for c in candidates
        ],
    )
    return resp


@app.post("/api/chat")
def chatbot(msg: ChatMessage):
    text = msg.message.lower()
    # Lightweight intent handling to keep runtime lean
    if any(k in text for k in ["price", "cost", "kwh", "pricing"]):
        return {
            "reply": "Pricing varies by operator and power. Typical public fast charging ranges from $0.20 to $0.80 per kWh. Use the station details to compare."}
    if any(k in text for k in ["near", "nearest", "close", "around me"]):
        return {"reply": "Enter your location in the search box to see the nearest stations and optimized route."}
    if any(k in text for k in ["available", "availability", "busy", "free"]):
        return {"reply": "Live availability depends on operator integrations. This demo shows indicative availability; check operator apps for real-time slots."}
    if any(k in text for k in ["route", "navigate", "direction"]):
        return {"reply": "Provide your origin address and optional charger preference. The optimizer will pick the best nearby station and draw the route."}
    return {"reply": "I can help you find nearby charging stations, pricing info, and route guidance. Ask me about 'nearest station', 'pricing', or 'availability'."}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
