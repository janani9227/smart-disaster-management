from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
# Support the predictor name used by different versions of risk_model.py.
try:
    from risk_model import predict_flood_risk
except ImportError:
    try:
        from risk_model import predict_one as _predict_one

        def predict_flood_risk(duration, area_affected, human_fatality,
                               human_injured, human_displaced, animal_fatality):
            features = [duration, area_affected, human_fatality,
                        human_injured, human_displaced, animal_fatality]
            try:
                return _predict_one(features)
            except TypeError:
                return _predict_one(*features)
    except ImportError as error:
        raise ImportError(
            "risk_model.py must define predict_flood_risk(...) or predict_one(...)."
        ) from error

import csv
import json
import math
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date, timedelta, timezone

import auth

# =========================================================
# CONFIGURATION
# =========================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
FRONTEND_DIST = os.path.abspath(os.path.join(PROJECT_DIR, "frontend", "dist"))
APP_USER_AGENT = "SmartDisasterManagement/1.0 (student project)"

app = Flask(
    __name__,
    static_folder=os.path.join(FRONTEND_DIST, "assets"),
    static_url_path="/assets",
)
CORS(app)

HAZARD_TYPES = [
    "flood", "earthquake", "cyclone", "wildfire", "landslide",
    "drought", "heatwave", "tsunami", "lightning", "avalanche",
]
HISTORY_CSV_NAMES = {
    "india_flood_inventory_v3.csv",
    "district_floodedarea.csv",
    "district_floodimpact.csv",
    "dfsi.csv",
}
RANK = {"NOT ASSESSED": -1, "DATA UNAVAILABLE": -1, "LOW": 0, "MEDIUM": 1, "HIGH": 2}

# =========================================================
# HTTP / LOCATION / WEATHER HELPERS
# =========================================================
def get_json(url, headers=None, timeout=15):
    # --- patch v2 applied: retries once on a temporary server error (500/502/503/504)
    request_headers = headers or {"User-Agent": APP_USER_AGENT}
    for attempt in (1, 2):
        req = urllib.request.Request(url, headers=request_headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if attempt == 2 or error.code not in (500, 502, 503, 504):
                raise
            time.sleep(1.5)


def post_form(url, form, timeout=25):
    """POST form data (used for Overpass; long queries are unreliable in a GET URL)."""
    data = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"User-Agent": APP_USER_AGENT,
                 "Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _safe(fn, *args):
    """Run a data-source call; return None (and log) instead of raising."""
    try:
        return fn(*args)
    except Exception as error:
        app.logger.warning("%s failed: %s", getattr(fn, "__name__", "call"), error)
        return None


def get_coordinates(location_name):
    location_name = (location_name or "").strip()
    if not location_name:
        return None

    # Open-Meteo geocoding first (worldwide coverage)
    url = (
        "https://geocoding-api.open-meteo.com/v1/search?"
        + urllib.parse.urlencode({
            "name": location_name,
            "count": 10,
            "language": "en",
            "format": "json",
        })
    )
    try:
        results = get_json(url).get("results", [])
        if results:
            preferred = [r for r in results if (r.get("feature_code") or "").startswith("PPL")]
            result = preferred[0] if preferred else results[0]
            postcodes = result.get("postcodes") or [""]
            return {
                "name": result.get("name", location_name),
                "latitude": float(result["latitude"]),
                "longitude": float(result["longitude"]),
                "country": result.get("country", ""),
                "state": result.get("admin1", ""),
                "district": result.get("admin2", "") or result.get("admin3", ""),
                "postcode": postcodes[0],
                "elevation": result.get("elevation"),
            }
    except Exception as error:
        app.logger.warning("Open-Meteo geocoding failed: %s", error)

    # Nominatim fallback (worldwide OpenStreetMap coverage)
    try:
        url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode({
            "q": location_name,
            "format": "jsonv2",
            "limit": 1,
            "addressdetails": 1,
        })
        results = get_json(url, headers={"User-Agent": APP_USER_AGENT})
        if results:
            result = results[0]
            address = result.get("address", {})
            return {
                "name": (
                    address.get("village") or address.get("town") or
                    address.get("city") or address.get("suburb") or
                    result.get("display_name", location_name)
                ),
                "latitude": float(result["lat"]),
                "longitude": float(result["lon"]),
                "country": address.get("country", ""),
                "state": address.get("state") or address.get("region", ""),
                "district": address.get("state_district") or address.get("county") or address.get("district", ""),
                "postcode": address.get("postcode", ""),
            }
    except Exception as error:
        app.logger.warning("Nominatim fallback failed: %s", error)

    # Offline geocoding fallback for commonly queried global and regional cities
    known_places = {
        "vellore": (12.9165, 79.1325, "Vellore", "Tamil Nadu", "India"),
        "chennai": (13.0827, 80.2707, "Chennai", "Tamil Nadu", "India"),
        "madurai": (9.9252, 78.1198, "Madurai", "Tamil Nadu", "India"),
        "coimbatore": (11.0168, 76.9558, "Coimbatore", "Tamil Nadu", "India"),
        "virudhunagar": (9.5851, 77.9579, "Virudhunagar", "Tamil Nadu", "India"),
        "tirunelveli": (8.7139, 77.7567, "Tirunelveli", "Tamil Nadu", "India"),
        "thoothukudi": (8.7642, 78.1348, "Thoothukudi", "Tamil Nadu", "India"),
        "salem": (11.6643, 78.1460, "Salem", "Tamil Nadu", "India"),
        "trichy": (10.7905, 78.7047, "Tiruchirappalli", "Tamil Nadu", "India"),
        "tiruchirappalli": (10.7905, 78.7047, "Tiruchirappalli", "Tamil Nadu", "India"),
        "bangalore": (12.9716, 77.5946, "Bengaluru", "Karnataka", "India"),
        "bengaluru": (12.9716, 77.5946, "Bengaluru", "Karnataka", "India"),
        "hyderabad": (17.3850, 78.4867, "Hyderabad", "Telangana", "India"),
        "delhi": (28.6139, 77.2090, "Delhi", "Delhi", "India"),
        "mumbai": (19.0760, 72.8777, "Mumbai", "Maharashtra", "India"),
        "kolkata": (22.5726, 88.3639, "Kolkata", "West Bengal", "India"),
        "tokyo": (35.6762, 139.6503, "Tokyo", "Tokyo", "Japan"),
        "london": (51.5074, -0.1278, "London", "England", "United Kingdom"),
        "new york": (40.7128, -74.0060, "New York", "New York", "United States"),
        "paris": (48.8566, 2.3522, "Paris", "Île-de-France", "France"),
        "sydney": (-33.8688, 151.2093, "Sydney", "New South Wales", "Australia"),
        "dubai": (25.2048, 55.2708, "Dubai", "Dubai", "United Arab Emirates"),
        "singapore": (1.3521, 103.8198, "Singapore", "Central", "Singapore"),
        "cairo": (30.0444, 31.2357, "Cairo", "Cairo", "Egypt"),
        "los angeles": (34.0522, -118.2437, "Los Angeles", "California", "United States"),
        "toronto": (43.6532, -79.3832, "Toronto", "Ontario", "Canada"),
        "berlin": (52.5200, 13.4050, "Berlin", "Berlin", "Germany"),
    }
    fallback = known_places.get(location_name.casefold())
    if fallback:
        lat, lon, name, state, country = fallback
        app.logger.warning("Using built-in locality coordinates for %s; geocoder unavailable", location_name)
        return {"name": name, "latitude": lat, "longitude": lon, "country": country, "state": state, "district": name, "postcode": ""}
    return None


_WEATHER_CACHE = {}
_WEATHER_CACHE_SECONDS = 300

def get_live_weather(location_name):
    cache_key = (location_name or "").strip().casefold()
    cached = _WEATHER_CACHE.get(cache_key)
    if cached and (datetime.now().timestamp() - cached[0]) < _WEATHER_CACHE_SECONDS:
        return dict(cached[1])
    location = get_coordinates(location_name)
    if location is None:
        return {"error": f"Could not locate '{location_name}'. Try a city or district name."}

    url = (
        "https://api.open-meteo.com/v1/forecast?"
        + urllib.parse.urlencode({
            "latitude": location["latitude"],
            "longitude": location["longitude"],
            "current": "temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m",
            "temperature_unit": "celsius",
            "wind_speed_unit": "kmh",
            "precipitation_unit": "mm",
            "timezone": "auto",
        })
    )
    try:
        data = get_json(url, timeout=10)
    except Exception as error:
        app.logger.warning("Open-Meteo weather request failed for %s: %s", location_name, error)
        return {"error": f"Live weather provider unavailable ({type(error).__name__}). Check internet access and retry."}
    current = data.get("current", {})
    if not current or current.get("temperature_2m") is None:
        return {"error": "Live weather provider returned no current weather values."}
    result = {
        "location": location["name"],
        "country": location["country"],
        "state": location["state"],
        "district": location["district"],
        "postcode": location["postcode"],
        "latitude": location["latitude"],
        "longitude": location["longitude"],
        "elevation": data.get("elevation"),
        "temperature": round(float(current.get("temperature_2m", 0)), 1),
        "humidity": round(float(current.get("relative_humidity_2m", 0))),
        "rainfall": round(float(current.get("precipitation", 0)), 1),
        "wind_speed": round(float(current.get("wind_speed_10m", 0)), 1),
        "source": "Open-Meteo Live Weather API",
    }
    _WEATHER_CACHE[cache_key] = (datetime.now().timestamp(), dict(result))
    return result


def calculate_distance(lat1, lon1, lat2, lon2):
    """Haversine distance in km. (FIX: the original converted the latitude
    difference to radians twice, which gave wrong distances.)"""
    earth_radius = 6371
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return earth_radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

# =========================================================
# EMERGENCY FACILITIES / SAFE-ZONE RECOMMENDATION
# =========================================================
OVERPASS_SERVERS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.openstreetmap.fr/api/interpreter",
)
_FACILITY_CACHE = {}
_FACILITY_CACHE_SECONDS = 600


def _classify_facility(tags):
    amenity = tags.get("amenity")
    healthcare = tags.get("healthcare")
    emergency = tags.get("emergency")

    if amenity == "hospital" or healthcare == "hospital":
        return "Hospital"
    if emergency == "ambulance_station" or tags.get("emergency:service") == "ambulance":
        return "Ambulance Station"
    if amenity in ("clinic", "doctors") or healthcare in ("clinic", "centre", "doctor"):
        return "Emergency Clinic"
    if amenity == "pharmacy":
        return "Pharmacy"
    if amenity == "fire_station":
        return "Fire Station"
    if amenity == "police":
        return "Police Station"
    if amenity == "shelter" or emergency == "assembly_point" or tags.get("social_facility") == "shelter":
        return "Shelter"
    if amenity == "community_centre":
        return "Community Centre"
    if amenity == "school":
        return "School"
    return None


def _overpass(query, timeout=12):
    # --- patch v2 applied: ask every mirror at once, first good answer wins
    pool = ThreadPoolExecutor(max_workers=len(OVERPASS_SERVERS))
    futures = {pool.submit(post_form, server, {"data": query}, timeout): server for server in OVERPASS_SERVERS}
    errors = []
    try:
        for future in as_completed(futures):
            server = futures[future]
            try:
                return future.result()
            except Exception as error:
                errors.append(f"{urllib.parse.urlparse(server).netloc}: {error}")
                app.logger.warning("Overpass server failed (%s): %s", server, error)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    raise RuntimeError("OpenStreetMap (Overpass) lookup failed - " + "; ".join(errors))


def _elements_to_facilities(elements, latitude, longitude):
    facilities = []
    for element in elements:
        tags = element.get("tags", {})
        ftype = _classify_facility(tags)
        if not ftype:
            continue
        if "lat" in element and "lon" in element:
            facility_lat, facility_lon = element["lat"], element["lon"]
        elif "center" in element:
            facility_lat, facility_lon = element["center"]["lat"], element["center"]["lon"]
        else:
            continue
        dist = round(calculate_distance(latitude, longitude, facility_lat, facility_lon), 2)
        is_med = ftype in ("Hospital", "Emergency Clinic", "Ambulance Station", "Pharmacy")
        phone = tags.get("phone") or tags.get("contact:phone") or tags.get("emergency:phone") or "Not available"
        addr_parts = [tags.get(k) for k in ("addr:housenumber", "addr:street", "addr:suburb", "addr:city") if tags.get(k)]
        address = ", ".join(addr_parts) if addr_parts else tags.get("addr:full", "Local area")
        is_24_7 = tags.get("opening_hours") == "24/7" or tags.get("emergency") == "yes"
        facilities.append({
            "id": f"{element.get('type', 'x')}/{element.get('id', '')}",
            "name": tags.get("name") or tags.get("name:en") or f"{ftype} ({address})",
            "type": ftype,
            "category": "Medical Facility" if is_med else "Emergency Service",
            "is_medical": is_med,
            "latitude": facility_lat,
            "longitude": facility_lon,
            "distance": dist,
            "opening_hours": tags.get("opening_hours", "24/7 Emergency Service" if is_24_7 else "Not available in map data"),
            "is_24_7": is_24_7,
            "phone": phone,
            "address": address,
            "maps_url": f"https://www.google.com/maps/search/?api=1&query={facility_lat},{facility_lon}",
            "source": "OpenStreetMap",
        })
    unique = {f["id"]: f for f in facilities}
    return sorted(unique.values(), key=lambda item: item["distance"])


def _nominatim_one(index, term, viewbox, wanted, latitude, longitude):
    if index:
        time.sleep(1.1 * index)  # Nominatim allows at most 1 request per second
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode({
        "q": term, "format": "jsonv2", "viewbox": viewbox, "bounded": 1, "limit": 25,
    })
    try:
        results = get_json(url, headers={"User-Agent": APP_USER_AGENT}, timeout=8)
    except Exception as error:
        app.logger.warning("Nominatim facility lookup failed for %s: %s", term, error)
        return []
    facilities = []
    for item in results:
        ftype = wanted.get(item.get("type"))
        if not ftype:
            continue
        try:
            f_lat, f_lon = float(item["lat"]), float(item["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        dist = round(calculate_distance(latitude, longitude, f_lat, f_lon), 2)
        is_med = ftype in ("Hospital", "Emergency Clinic", "Ambulance Station", "Pharmacy")
        facilities.append({
            "id": f"{item.get('osm_type', 'x')}/{item.get('osm_id', '')}",
            "name": item.get("name") or (item.get("display_name") or ftype).split(",")[0],
            "type": ftype,
            "category": "Medical Facility" if is_med else "Emergency Service",
            "is_medical": is_med,
            "latitude": f_lat,
            "longitude": f_lon,
            "distance": dist,
            "opening_hours": "Not available in map data",
            "is_24_7": False,
            "phone": "Not available",
            "address": (item.get("display_name") or "").split(",", 2)[-1].strip() or "Local area",
            "maps_url": f"https://www.google.com/maps/search/?api=1&query={f_lat},{f_lon}",
            "source": "OpenStreetMap (Nominatim)",
        })
    return facilities


def _nominatim_facilities(latitude, longitude, radius):
    """Fallback for hospitals / medical / fire / police stations when Overpass is down.
    The lookups run side by side, started 1.1 s apart."""
    d_lat = radius / 111000.0
    d_lon = radius / (111000.0 * max(0.2, math.cos(math.radians(latitude))))
    viewbox = f"{longitude - d_lon:.5f},{latitude + d_lat:.5f},{longitude + d_lon:.5f},{latitude - d_lat:.5f}"
    wanted = {
        "hospital": "Hospital",
        "clinic": "Emergency Clinic",
        "doctors": "Emergency Clinic",
        "pharmacy": "Pharmacy",
        "fire_station": "Fire Station",
        "police": "Police Station",
    }
    terms = ("hospital", "clinic", "pharmacy", "fire station", "police station")
    with ThreadPoolExecutor(max_workers=5) as pool:
        parts = list(pool.map(
            lambda pair: _nominatim_one(pair[0], pair[1], viewbox, wanted, latitude, longitude),
            enumerate(terms)))
    unique = {f["id"]: f for part in parts for f in part}
    return sorted(unique.values(), key=lambda item: item["distance"])


FACILITY_DISK_CACHE = os.path.join(BASE_DIR, "facility_cache.json")
FACILITY_DISK_FRESH_SECONDS = 7 * 24 * 3600
_FACILITY_LOCK = threading.Lock()


def _facility_disk_load():
    try:
        with open(FACILITY_DISK_CACHE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _facility_disk_save(key, facilities):
    try:
        data = _facility_disk_load()
        data[key] = {"saved": datetime.now().timestamp(), "facilities": facilities}
        if len(data) > 100:
            for old_key in sorted(data, key=lambda k: data[k].get("saved", 0))[:len(data) - 100]:
                data.pop(old_key, None)
        temp_path = FACILITY_DISK_CACHE + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
        os.replace(temp_path, FACILITY_DISK_CACHE)
    except (OSError, TypeError, ValueError) as error:
        app.logger.warning("Could not save facility cache: %s", error)


def _redistance(facilities, latitude, longitude, source=None):
    """Copy cached facilities with distances measured from the searched point."""
    out = []
    for item in facilities:
        copy = dict(item)
        copy["distance"] = round(calculate_distance(latitude, longitude, copy["latitude"], copy["longitude"]), 2)
        if source:
            copy["source"] = source
        out.append(copy)
    return sorted(out, key=lambda item: item["distance"])


def _fetch_facilities_live(latitude, longitude, radius):
    around = f"(around:{radius},{latitude},{longitude})"
    core_query = f"""
    [out:json][timeout:20];
    (
      nwr["amenity"="hospital"]{around};
      nwr["healthcare"="hospital"]{around};
      nwr["amenity"="clinic"]{around};
      nwr["healthcare"="clinic"]{around};
      nwr["emergency"="ambulance_station"]{around};
      nwr["amenity"="pharmacy"]{around};
      nwr["amenity"="fire_station"]{around};
      nwr["amenity"="police"]{around};
    );
    out center tags 300;
    """
    shelter_query = f"""
    [out:json][timeout:20];
    (
      nwr["amenity"="shelter"]{around};
      nwr["emergency"="assembly_point"]{around};
      nwr["social_facility"="shelter"]{around};
      nwr["amenity"="community_centre"]{around};
    );
    out center tags 150;
    """
    result, overpass_error = [], None
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_core = pool.submit(_overpass, core_query)
            f_shelter = pool.submit(_overpass, shelter_query)
            elements = list(f_core.result().get("elements", []))  # raises if the core lookup failed
            try:
                elements += f_shelter.result().get("elements", [])
            except Exception as error:
                app.logger.warning("Shelter lookup failed (hospitals/fire/police still shown): %s", error)
        has_shelter_like = any(
            _classify_facility(e.get("tags", {})) in ("Shelter", "Community Centre") for e in elements
        )
        if not has_shelter_like:
            school_query = f"""
            [out:json][timeout:20];
            nwr["amenity"="school"]{around};
            out center tags 60;
            """
            try:
                elements += _overpass(school_query).get("elements", [])
            except Exception as error:
                app.logger.warning("School fallback query failed: %s", error)
        result = _elements_to_facilities(elements, latitude, longitude)
    except Exception as error:
        overpass_error = error
        app.logger.warning("Overpass unavailable, trying Nominatim fallback: %s", error)

    if overpass_error is not None:
        result = _nominatim_facilities(latitude, longitude, radius)
        if not result:
            raise RuntimeError(
                f"Could not load facilities from OpenStreetMap ({overpass_error}). "
                "The public map servers are busy or unreachable from this network; retry in a few minutes."
            )
    return result


def find_emergency_facilities(latitude, longitude, radius=10000):
    mem_key = (round(latitude, 2), round(longitude, 2), radius)
    disk_key = f"{mem_key[0]},{mem_key[1]},{radius}"
    with _FACILITY_LOCK:  # a duplicate request waits here, then is answered from the cache
        now = datetime.now().timestamp()
        cached = _FACILITY_CACHE.get(mem_key)
        if cached and (now - cached[0]) < _FACILITY_CACHE_SECONDS:
            return _redistance(cached[1], latitude, longitude)
        disk_entry = _facility_disk_load().get(disk_key)
        saved_list = (disk_entry or {}).get("facilities") or []
        if saved_list and (now - disk_entry.get("saved", 0)) < FACILITY_DISK_FRESH_SECONDS:
            _FACILITY_CACHE[mem_key] = (now, saved_list)
            return _redistance(saved_list, latitude, longitude)
        try:
            result = _fetch_facilities_live(latitude, longitude, radius)
        except Exception as error:
            if saved_list:
                app.logger.warning("Live facility lookup failed (%s); using the saved copy", error)
                return _redistance(saved_list, latitude, longitude, source="OpenStreetMap (saved copy)")
            raise
        if result:  # never save an empty answer, so the next search retries
            _FACILITY_CACHE[mem_key] = (now, result)
            _facility_disk_save(disk_key, result)
        return result


def choose_safe_zone(facilities):
    if not facilities:
        return {"found": False, "message": "No suitable nearby facility was found in the map data."}
    priority = {"Shelter": 1, "Community Centre": 2, "School": 3, "Hospital": 4, "Fire Station": 5, "Police Station": 6}
    recommended = min(facilities, key=lambda f: (priority.get(f["type"], 10), f["distance"]))
    return {
        "found": True,
        "name": recommended["name"],
        "type": recommended["type"],
        "latitude": recommended["latitude"],
        "longitude": recommended["longitude"],
        "distance": recommended["distance"],
        "opening_hours": recommended["opening_hours"],
        "message": "Nearby mapped facility recommendation based on facility type and distance.",
        "warning": "This is not an official declaration that the location is safe during an active disaster.",
    }

# =========================================================
# DATE-RANGE CLIMATE DATA (Open-Meteo archive + forecast)
# =========================================================
DAILY_VARS = ("temperature_2m_max,temperature_2m_min,precipitation_sum,"
              "wind_speed_10m_max,wind_gusts_10m_max,weather_code,snowfall_sum")
FORECAST_HORIZON_DAYS = 15
ARCHIVE_LAG_DAYS = 6


def parse_range(from_value, to_value, default_days=7, max_days=400):
    today = date.today()
    start = date.fromisoformat(from_value) if from_value else today
    end = date.fromisoformat(to_value) if to_value else start + timedelta(days=default_days)
    if start > end:
        raise ValueError("The From date must be before or equal to the To date.")
    if (end - start).days + 1 > max_days:
        raise ValueError(f"Date range is too long (maximum {max_days} days).")
    return start, end


def _fetch_daily(base_url, lat, lon, start, end, label):
    url = base_url + "?" + urllib.parse.urlencode({
        "latitude": lat, "longitude": lon,
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "daily": DAILY_VARS, "timezone": "auto", "wind_speed_unit": "kmh",
    })
    data = get_json(url, timeout=20)
    daily = data.get("daily", {})
    rows = []
    for i, day in enumerate(daily.get("time", [])):
        def value(key):
            series = daily.get(key) or []
            return series[i] if i < len(series) else None
        rows.append({
            "date": day, "tmax": value("temperature_2m_max"), "tmin": value("temperature_2m_min"),
            "rain": value("precipitation_sum"), "wind": value("wind_speed_10m_max"),
            "gust": value("wind_gusts_10m_max"), "code": value("weather_code"),
            "snow": value("snowfall_sum"), "source": label,
        })
    return rows, data.get("elevation")


def get_daily_climate(lat, lon, start, end):
    """Observed (archive) + forecast daily data for start..end where available."""
    today = date.today()
    archive_end = today - timedelta(days=ARCHIVE_LAG_DAYS)
    forecast_end = today + timedelta(days=FORECAST_HORIZON_DAYS)
    rows, notes, elevation = [], [], None
    beyond = None

    if start <= archive_end:
        try:
            part, elev = _fetch_daily("https://archive-api.open-meteo.com/v1/archive", lat, lon,
                                      start, min(end, archive_end), "observed")
            rows += part
            elevation = elevation if elevation is not None else elev
        except Exception as error:
            notes.append(f"Historical weather unavailable ({type(error).__name__}).")
    f_start = max(start, archive_end + timedelta(days=1))
    if end >= f_start and f_start <= forecast_end:
        try:
            part, elev = _fetch_daily("https://api.open-meteo.com/v1/forecast", lat, lon,
                                      f_start, min(end, forecast_end), "forecast")
            for row in part:
                if row["date"] <= today.isoformat():
                    row["source"] = "observed"
            rows += part
            elevation = elevation if elevation is not None else elev
        except Exception as error:
            notes.append(f"Weather forecast unavailable ({type(error).__name__}).")
    if end > forecast_end:
        beyond = (max(start, forecast_end + timedelta(days=1)), end)
        notes.append(
            f"Day-by-day forecasts only extend {FORECAST_HORIZON_DAYS} days ahead; "
            f"{beyond[0].isoformat()} to {end.isoformat()} uses past-year climatology instead."
        )
    return {"days": rows, "elevation": elevation, "notes": notes, "beyond": beyond}


def _shift_years(d, years):
    try:
        return d.replace(year=d.year - years)
    except ValueError:  # 29 Feb
        return d.replace(year=d.year - years, day=28)


_CLIMATOLOGY_CACHE = {}
_CLIMATOLOGY_CACHE_SECONDS = 6 * 3600
_CLIMATOLOGY_LOCK = threading.Lock()


def get_climatology_windows(lat, lon, start, end, years=5):
    """The same calendar window in each of the last `years` years (real archive data).
    Cached for 6 hours so repeated / duplicate searches do not hit the API again."""
    key = (round(lat, 2), round(lon, 2), start.isoformat(), end.isoformat(), years)
    with _CLIMATOLOGY_LOCK:
        cached = _CLIMATOLOGY_CACHE.get(key)
        if cached and (time.time() - cached[0]) < _CLIMATOLOGY_CACHE_SECONDS:
            return [list(rows) for rows in cached[1]]

        archive_end = date.today() - timedelta(days=ARCHIVE_LAG_DAYS)
        offsets = []
        k = 1
        while len(offsets) < years and k < 40:
            s, e = _shift_years(start, k), _shift_years(end, k)
            if e <= archive_end:
                offsets.append((s, e))
            k += 1

        def one(window):
            for attempt in (1, 2):
                try:
                    rows, _ = _fetch_daily("https://archive-api.open-meteo.com/v1/archive", lat, lon,
                                           window[0], window[1], "climatology")
                    return rows
                except Exception as error:
                    if attempt == 2 or "429" not in str(error):
                        raise
                    time.sleep(3)  # rate-limited: wait a moment, then try once more

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda w: _safe(one, w), offsets))
        windows = [r for r in results if r]
        if offsets and len(windows) == len(offsets):  # only cache complete answers
            _CLIMATOLOGY_CACHE[key] = (time.time(), windows)
        return windows


def summarise_climate(days):
    def vals(key):
        return [d[key] for d in days if d.get(key) is not None]
    tmax, tmin, rain = vals("tmax"), vals("tmin"), vals("rain")
    if not days:
        return None
    return {
        "days": len(days),
        "avg_max_temp_c": round(sum(tmax) / len(tmax), 1) if tmax else None,
        "avg_min_temp_c": round(sum(tmin) / len(tmin), 1) if tmin else None,
        "hottest_day_c": max(tmax) if tmax else None,
        "coldest_night_c": min(tmin) if tmin else None,
        "total_rain_mm": round(sum(rain), 1) if rain else None,
        "rainy_days": sum(1 for r in rain if r >= 2.5),
        "wettest_day_mm": max(rain) if rain else None,
        "max_gust_kmh": max(vals("gust")) if vals("gust") else None,
    }

# =========================================================
# HAZARD LOGIC (all thresholds are generic screening values)
# =========================================================
def _streak(flags):
    best = cur = 0
    for flag in flags:
        cur = cur + 1 if flag else 0
        best = max(best, cur)
    return best


def _rolling_max(values, window=3):
    values = [v or 0 for v in values]
    if not values:
        return 0
    if len(values) < window:
        return sum(values)
    return max(sum(values[i:i + window]) for i in range(len(values) - window + 1))


def weather_hazard_levels(days, elevation):
    """Weather-driven hazards from daily climate rows. Returns {hazard: (level, chance_pct, details)}."""
    rain = [d.get("rain") for d in days]
    tmax = [d.get("tmax") for d in days]
    tmin = [d.get("tmin") for d in days]
    gust = [d.get("gust") for d in days]
    wind = [d.get("wind") for d in days]
    snow = [d.get("snow") for d in days]
    codes = [d.get("code") for d in days]
    nz = lambda xs: [x for x in xs if x is not None]
    out = {}

    # 1. FLOOD
    max1 = max(nz(rain), default=0)
    max3 = _rolling_max(rain)
    if max1 >= 150 or max3 >= 250:
        f_chance = min(98, 85 + int((max1 - 150) * 0.15))
        f_level = "HIGH"
    elif max1 >= 115.6 or max3 >= 180:
        f_chance = min(84, 70 + int((max1 - 115.6) * 0.4))
        f_level = "HIGH"
    elif max1 >= 64.5 or max3 >= 100:
        f_chance = min(69, 45 + int((max1 - 64.5) * 0.5))
        f_level = "MEDIUM"
    elif max1 >= 35.5 or max3 >= 55:
        f_chance = min(44, 25 + int((max1 - 35.5) * 0.6))
        f_level = "LOW"
    elif max1 >= 10:
        f_chance = min(24, 10 + int(max1 * 0.5))
        f_level = "LOW"
    else:
        f_chance = max(2, int(max1 * 0.4))
        f_level = "LOW"
    out["flood"] = (f_level, f_chance, {"max_daily_rain_mm": round(max1, 1), "max_3day_rain_mm": round(max3, 1)})

    # 2. CYCLONE / HIGH WIND STORM
    gmax, wmax = max(nz(gust), default=0), max(nz(wind), default=0)
    if gmax >= 118 or wmax >= 89:
        c_chance = min(98, 88 + int((gmax - 118) * 0.2))
        c_level = "HIGH"
    elif gmax >= 88 or wmax >= 62:
        c_chance = min(87, 70 + int((gmax - 88) * 0.5))
        c_level = "HIGH"
    elif gmax >= 62 or wmax >= 40:
        c_chance = min(69, 45 + int((gmax - 62) * 0.9))
        c_level = "MEDIUM"
    elif gmax >= 45 or wmax >= 30:
        c_chance = min(44, 20 + int((gmax - 45) * 0.8))
        c_level = "LOW"
    else:
        c_chance = max(2, int(gmax * 0.25))
        c_level = "LOW"
    out["cyclone"] = (c_level, c_chance, {"max_gust_kmh": round(gmax, 1), "max_wind_kmh": round(wmax, 1)})

    # 3. WILDFIRE / FOREST FIRE
    hot_dry = _streak([(t is not None and t >= 35 and (r or 0) < 1) for t, r in zip(tmax, rain)])
    tmax_peak = max(nz(tmax), default=0)
    if hot_dry >= 14 and tmax_peak >= 38:
        wf_chance = min(96, 75 + hot_dry)
        wf_level = "HIGH"
    elif hot_dry >= 7 and tmax_peak >= 35:
        wf_chance = min(74, 48 + int((hot_dry - 7) * 3))
        wf_level = "MEDIUM"
    elif hot_dry >= 4 and tmax_peak >= 32:
        wf_chance = min(45, 25 + hot_dry * 3)
        wf_level = "LOW"
    else:
        wf_chance = max(2, int(tmax_peak * 0.2))
        wf_level = "LOW"
    out["wildfire"] = (wf_level, wf_chance, {"hot_dry_streak_days": hot_dry, "peak_temp_c": tmax_peak})

    # 4. LANDSLIDE
    if elevation is not None and elevation < 100:
        out["landslide"] = ("LOW", 2, {"note": f"Low-lying terrain ({round(elevation)} m); landslide terrain unlikely.",
                                       "max_3day_rain_mm": round(max3, 1), "elevation_m": elevation})
    elif elevation is not None and elevation < 250:
        ls_chance = 40 if max3 >= 150 else 20 if max3 >= 80 else 5
        ls_level = "MEDIUM" if max3 >= 150 else "LOW"
        out["landslide"] = (ls_level, ls_chance, {"max_3day_rain_mm": round(max3, 1), "elevation_m": elevation})
    else:
        if max3 >= 150:
            ls_chance = min(95, 75 + int((max3 - 150) * 0.3))
            ls_level = "HIGH"
        elif max3 >= 80:
            ls_chance = min(74, 48 + int((max3 - 80) * 0.35))
            ls_level = "MEDIUM"
        elif max3 >= 45:
            ls_chance = min(45, 22 + int((max3 - 45) * 0.6))
            ls_level = "LOW"
        else:
            ls_chance = max(3, int(max3 * 0.3))
            ls_level = "LOW"
        out["landslide"] = (ls_level, ls_chance, {"max_3day_rain_mm": round(max3, 1), "elevation_m": elevation})

    # 5. DROUGHT
    if len(days) < 14:
        out["drought"] = ("NOT ASSESSED", None, {"note": "Drought needs a date range of at least 14 days."})
    else:
        dry = _streak([(r is not None and r < 1) for r in rain])
        total = sum(nz(rain))
        if dry >= 45 and total < 20:
            dr_chance = min(95, 75 + dry - 45)
            dr_level = "HIGH"
        elif dry >= 30 and total < 35:
            dr_chance = min(74, 48 + int((dry - 30) * 1.5))
            dr_level = "MEDIUM"
        elif dry >= 15 and total < 15:
            dr_chance = min(45, 25 + int(dry))
            dr_level = "LOW"
        else:
            dr_chance = max(3, int(dry * 0.4))
            dr_level = "LOW"
        out["drought"] = (dr_level, dr_chance, {"longest_dry_spell_days": dry, "total_rain_mm": round(total, 1)})

    # 6. HEATWAVE
    s40 = _streak([(t is not None and t >= 40) for t in tmax])
    s37 = _streak([(t is not None and t >= 37) for t in tmax])
    if tmax_peak >= 45 or s40 >= 3:
        hw_chance = min(98, 85 + int((tmax_peak - 45) * 2) + s40 * 2)
        hw_level = "HIGH"
    elif tmax_peak >= 42 or s40 >= 1:
        hw_chance = min(84, 65 + int((tmax_peak - 42) * 4) + s40 * 5)
        hw_level = "MEDIUM"
    elif tmax_peak >= 38 or s37 >= 3:
        hw_chance = min(64, 40 + int((tmax_peak - 38) * 5))
        hw_level = "MEDIUM"
    elif tmax_peak >= 35:
        hw_chance = min(39, 15 + int((tmax_peak - 35) * 4))
        hw_level = "LOW"
    else:
        hw_chance = max(1, int(max(0, tmax_peak - 25) * 0.5))
        hw_level = "LOW"
    out["heatwave"] = (hw_level, hw_chance, {"peak_temp_c": tmax_peak, "days_at_or_above_40c": sum(1 for t in nz(tmax) if t >= 40)})

    # 7. LIGHTNING / SEVERE STORMS
    storm_days = sum(1 for c in nz(codes) if c >= 95)
    hail = any(c in (96, 99) for c in nz(codes))
    if hail or storm_days >= 3:
        lt_chance = min(96, 80 + storm_days * 4)
        lt_level = "HIGH"
    elif storm_days >= 1:
        lt_chance = min(78, 55 + storm_days * 8)
        lt_level = "MEDIUM"
    else:
        lt_chance = max(4, int(max1 * 0.35))
        lt_level = "LOW"
    out["lightning"] = (lt_level, lt_chance, {"thunderstorm_days": storm_days, "hail_forecast": hail})

    # 8. AVALANCHE
    snow_max = max(nz(snow), default=0)
    if elevation is None or elevation < 1500:
        out["avalanche"] = ("LOW", 0, {"note": "Site elevation is below typical avalanche terrain (1500 m).", "elevation_m": elevation})
    else:
        if snow_max >= 30:
            av_chance = min(95, 80 + int(snow_max - 30))
            av_level = "HIGH"
        elif snow_max >= 10:
            av_chance = min(74, 45 + int((snow_max - 10) * 1.5))
            av_level = "MEDIUM"
        else:
            av_chance = max(5, int(snow_max * 2))
            av_level = "LOW"
        out["avalanche"] = (av_level, av_chance, {"max_daily_snowfall_cm": round(snow_max, 1), "elevation_m": elevation})

    return out


def usgs_query(lat, lon, radius_km, start, end, min_mag, limit=300):
    params = urllib.parse.urlencode({
        "format": "geojson", "starttime": start.isoformat(),
        "endtime": (end + timedelta(days=1)).isoformat(),
        "latitude": lat, "longitude": lon, "maxradiuskm": radius_km,
        "minmagnitude": min_mag, "orderby": "time", "limit": limit,
    })
    return get_json("https://earthquake.usgs.gov/fdsnws/event/1/query?" + params, timeout=20).get("features", [])


def usgs_count(lat, lon, radius_km, start, end, min_mag):
    params = urllib.parse.urlencode({
        "format": "geojson", "starttime": start.isoformat(),
        "endtime": (end + timedelta(days=1)).isoformat(),
        "latitude": lat, "longitude": lon, "maxradiuskm": radius_km, "minmagnitude": min_mag,
    })
    return int(get_json("https://earthquake.usgs.gov/fdsnws/event/1/count?" + params, timeout=20).get("count", 0))


def assess_earthquake(lat, lon, start, end):
    today = date.today()
    events = usgs_query(lat, lon, 100, start, min(end, today), 2.5) if start <= today else []
    background = usgs_count(lat, lon, 300, today - timedelta(days=365 * 20), today, 4.5)
    mags = [(e.get("properties") or {}).get("mag") or 0 for e in events]
    strongest = max(mags, default=None)
    if strongest is not None and strongest >= 5:
        level, chance = "HIGH", min(95, 75 + int((strongest - 5.0) * 15))
    elif strongest is not None and strongest >= 4:
        level, chance = "MEDIUM", min(74, 55 + int((strongest - 4.0) * 15))
    elif background >= 30:
        level, chance = "MEDIUM", min(68, 45 + int((background - 30) * 0.5))
    elif background >= 10:
        level, chance = "LOW", min(44, 25 + int((background - 10) * 1.0))
    elif background >= 2:
        level, chance = "LOW", min(24, 10 + background * 1.5)
    else:
        level, chance = "LOW", max(2, background * 2)
    details = {"events_in_range_within_100km": len(events), "strongest_magnitude": strongest,
               "m4_5_plus_within_300km_last_20y": background}
    if end > today:
        details["note"] = "Future earthquakes cannot be scheduled; occurrence chance reflects active regional tectonic history and 20-year USGS catalog."
    return level, chance, details


def assess_tsunami(lat, lon, start, end, elevation):
    today = date.today()
    events = usgs_query(lat, lon, 2000, start, min(end, today), 6.5, 100) if start <= today else []
    flagged = [e for e in events if (e.get("properties") or {}).get("tsunami") == 1]
    background = usgs_count(lat, lon, 2000, today - timedelta(days=365 * 30), today, 7.0)
    inland = elevation is not None and elevation > 50
    if inland:
        level, chance = "LOW", 0
    elif flagged:
        level, chance = "HIGH", min(98, 85 + len(flagged) * 5)
    elif events or background >= 5:
        level, chance = "MEDIUM", 35
    elif background >= 1:
        level, chance = "LOW", 15
    else:
        level, chance = "LOW", 1
    details = {"m6_5_plus_quakes_in_range_within_2000km": len(events),
               "tsunami_flagged_events_in_range": len(flagged),
               "m7_plus_quakes_within_2000km_last_30y": background,
               "site_elevation_m": elevation}
    if inland:
        details["note"] = "Elevated inland site (>50 m); direct tsunami inundation is physically impossible."
    return level, chance, details


EONET_HAZARD_MAP = {
    "severeStorms": "cyclone", "wildfires": "wildfire", "floods": "flood",
    "landslides": "landslide", "drought": "drought", "tempExtremes": "heatwave",
}


def _geom_points(geometry):
    coords = geometry.get("coordinates") or []
    try:
        if geometry.get("type") == "Point" and len(coords) >= 2:
            return [(float(coords[0]), float(coords[1]))]
        if geometry.get("type") == "Polygon" and coords and coords[0]:
            return [(float(p[0]), float(p[1])) for p in coords[0] if len(p) >= 2]
    except (TypeError, ValueError):
        pass
    return []


def eonet_bbox(lat, lon, deg=5.0):
    lon_delta = min(25.0, deg / max(0.2, math.cos(math.radians(lat))))
    return f"{lon - lon_delta:.3f},{min(lat + deg, 90):.3f},{lon + lon_delta:.3f},{max(lat - deg, -90):.3f}"


def fetch_eonet(lat, lon, start, end, deg=5.0, limit=200):
    """NASA EONET events inside a bounding box (FIX: the box is now sent to the API;
    the original downloaded the first 500 events worldwide and filtered locally)."""
    base = "https://eonet.gsfc.nasa.gov/api/v3/events?"
    bbox = eonet_bbox(lat, lon, deg)
    events = {}
    urls = [base + urllib.parse.urlencode({"status": "all", "start": start.isoformat(),
                                           "end": end.isoformat(), "bbox": bbox, "limit": limit})]
    if end >= date.today():  # events still open today are relevant to a current/future window
        urls.append(base + urllib.parse.urlencode({"status": "open", "bbox": bbox, "limit": limit}))
    for url in urls:
        try:
            for event in get_json(url, timeout=20).get("events", []):
                events[event.get("id")] = event
        except Exception as error:
            app.logger.warning("NASA EONET query failed: %s", error)
    return list(events.values())


def eonet_nearest(event, lat, lon):
    best = None
    for geometry in event.get("geometry", []) or []:
        for p_lon, p_lat in _geom_points(geometry):
            d = calculate_distance(lat, lon, p_lat, p_lon)
            best = d if best is None else min(best, d)
    return best


def river_discharge_signal(lat, lon, start, end):
    """Open-Meteo Flood API (GloFAS) river discharge vs. the previous year's 95th percentile."""
    today = date.today()
    e = min(end, today + timedelta(days=7))
    if e < start:
        return None
    s = start - timedelta(days=365)
    url = "https://flood-api.open-meteo.com/v1/flood?" + urllib.parse.urlencode({
        "latitude": lat, "longitude": lon, "daily": "river_discharge",
        "start_date": s.isoformat(), "end_date": e.isoformat(),
    })
    daily = get_json(url, timeout=20).get("daily", {})
    times, vals = daily.get("time", []), daily.get("river_discharge", [])
    base = sorted(v for t, v in zip(times, vals) if v is not None and t < start.isoformat())
    window = [v for t, v in zip(times, vals) if v is not None and t >= start.isoformat()]
    if len(base) < 30 or not window:
        return None
    p95 = base[int(0.95 * (len(base) - 1))]
    peak = max(window)
    ratio = peak / p95 if p95 > 0 else None
    level = None
    if ratio is not None:
        level = "HIGH" if ratio >= 1.5 else "MEDIUM" if ratio >= 1.0 else "LOW"
    return {"level": level, "peak_discharge_m3s": round(peak, 2), "p95_previous_year_m3s": round(p95, 2),
            "ratio": round(ratio, 2) if ratio is not None else None}


def _max_level(*levels):
    levels = [l for l in levels if l in RANK]
    return max(levels, key=lambda l: RANK[l]) if levels else "DATA UNAVAILABLE"


HAZARD_INFO = {
    "flood": ("Rainfall thresholds + GloFAS river discharge + NASA EONET flood events", "Open-Meteo, Open-Meteo Flood API, NASA EONET"),
    "earthquake": ("USGS catalog (recorded events + 20-year background); earthquakes cannot be predicted", "USGS Earthquake Catalog"),
    "cyclone": ("Daily wind-gust thresholds + NASA EONET severe storms", "Open-Meteo, NASA EONET"),
    "wildfire": ("Hot/dry-spell fire weather + NASA EONET wildfire events", "Open-Meteo, NASA EONET"),
    "landslide": ("Accumulated rainfall on elevated terrain + NASA EONET landslides", "Open-Meteo, NASA EONET"),
    "drought": ("Longest dry spell and total rainfall over the period", "Open-Meteo"),
    "heatwave": ("Daily maximum temperature thresholds", "Open-Meteo"),
    "tsunami": ("Large offshore earthquakes (USGS tsunami flag) + site elevation", "USGS Earthquake Catalog, Open-Meteo elevation"),
    "lightning": ("Forecast/observed thunderstorm weather codes (no strike-detection feed)", "Open-Meteo"),
    "avalanche": ("Snowfall on high-elevation terrain (no snowpack/bulletin data)", "Open-Meteo"),
}


def assess_range(w, start, end):
    lat, lon = float(w["latitude"]), float(w["longitude"])
    climate = get_daily_climate(lat, lon, start, end)
    days, notes = climate["days"], list(climate["notes"])
    elevation = climate["elevation"] if climate["elevation"] is not None else w.get("elevation")
    today = date.today()

    with ThreadPoolExecutor(max_workers=6) as pool:
        f_quake = pool.submit(_safe, assess_earthquake, lat, lon, start, end)
        f_tsu = pool.submit(_safe, assess_tsunami, lat, lon, start, end, elevation)
        f_eonet = pool.submit(_safe, fetch_eonet, lat, lon, start, min(end, today)) if start <= today or end >= today else None
        f_flood = pool.submit(_safe, river_discharge_signal, lat, lon, start, end)
        f_clim = pool.submit(_safe, get_climatology_windows, lat, lon, start, end) if (end - start).days <= 31 else None
        quake, tsu = f_quake.result(), f_tsu.result()
        eonet = (f_eonet.result() if f_eonet else None) or []
        discharge = f_flood.result()
        windows = (f_clim.result() if f_clim else None) or []

    levels = weather_hazard_levels(days, elevation) if days else {}

    # Past-year climatology for the part of the range beyond the forecast horizon.
    outlook = {}
    if windows:
        per_year = [weather_hazard_levels(w_days, elevation) for w_days in windows]
        for hz in levels.keys() | per_year[0].keys():
            ranks = [r for r in (RANK.get(py[hz][0], -1) for py in per_year if hz in py) if r >= 0]
            if not ranks:
                continue
            n = len(ranks)
            high_freq = sum(1 for r in ranks if r >= 2) / n
            med_freq = sum(1 for r in ranks if r >= 1) / n
            lvl = "HIGH" if high_freq >= 0.4 else "MEDIUM" if med_freq >= 0.4 else "LOW"
            outlook[hz] = (lvl, {"years_compared": n, "years_with_medium_or_higher": sum(1 for r in ranks if r >= 1),
                                 "years_with_high": sum(1 for r in ranks if r >= 2)})
    else:
        notes.append("Past-year seasonal comparison was not used (range longer than 31 days, or the weather archive was busy); levels use live forecast/observed data only.")

    # EONET: nearby real events raise the level for the matching hazard.
    eonet_hits = {}
    for event in eonet:
        for cat in event.get("categories", []):
            hz = EONET_HAZARD_MAP.get(cat.get("id"))
            dist = eonet_nearest(event, lat, lon)
            if hz and dist is not None:
                eonet_hits.setdefault(hz, []).append((dist, event.get("title")))

    results = []
    for hz in HAZARD_TYPES:
        method, source = HAZARD_INFO[hz]
        candidates, details, candidate_chances = [], {}, []
        if hz == "earthquake":
            if quake:
                candidates.append(quake[0])
                if quake[1] is not None:
                    candidate_chances.append(quake[1])
                details.update(quake[2])
        elif hz == "tsunami":
            if tsu:
                candidates.append(tsu[0])
                if tsu[1] is not None:
                    candidate_chances.append(tsu[1])
                details.update(tsu[2])
        else:
            if hz in levels:
                candidates.append(levels[hz][0])
                if levels[hz][1] is not None:
                    candidate_chances.append(levels[hz][1])
                details.update(levels[hz][2])
            if hz in outlook:
                candidates.append(outlook[hz][0])
                details["climatology_outlook"] = outlook[hz][1]
        if hz == "flood" and discharge:
            if discharge.get("level"):
                candidates.append(discharge["level"])
                if discharge.get("ratio") and discharge["ratio"] >= 1.5:
                    candidate_chances.append(85)
                elif discharge.get("ratio") and discharge["ratio"] >= 1.2:
                    candidate_chances.append(65)
            details["river_discharge"] = discharge
        if hz in eonet_hits:
            nearest = min(eonet_hits[hz])
            lvl = "HIGH" if nearest[0] <= 100 else "MEDIUM" if nearest[0] <= 300 else "LOW"
            e_chance = 90 if nearest[0] <= 100 else 65 if nearest[0] <= 300 else 35
            candidates.append(lvl)
            candidate_chances.append(e_chance)
            details["nasa_eonet_events_nearby"] = len(eonet_hits[hz])
            details["nearest_event"] = {"title": nearest[1], "distance_km": round(nearest[0])}
        # keep "NOT ASSESSED" only if nothing else produced a real level
        real = [c for c in candidates if c in ("LOW", "MEDIUM", "HIGH")]
        if real:
            level = _max_level(*real)
        elif candidates:
            level = candidates[0]
        else:
            level = "DATA UNAVAILABLE"

        final_chance = max(candidate_chances) if candidate_chances else None
        if final_chance is not None:
            if final_chance >= 75:
                level = "HIGH"
            elif final_chance >= 45 and level != "HIGH":
                level = "MEDIUM"

        results.append({
            "disaster": hz.title(), "disaster_key": hz, "risk_level": level,
            "occurrence_chance": final_chance,
            "location": w.get("location"), "temperature": w.get("temperature"),
            "humidity": w.get("humidity"), "rainfall": w.get("rainfall"), "wind_speed": w.get("wind_speed"),
            "latitude": lat, "longitude": lon, "weather_source": w.get("source"),
            "source": source, "method": method,
            "period": {"from": start.isoformat(), "to": end.isoformat()},
            "warning": ("Prototype screening from public data; not an official warning. "
                        "DATA UNAVAILABLE means a data request failed and is NOT a low-risk result."),
            "details": details, "is_official_warning": False,
        })

    observed = sum(1 for d in days if d["source"] == "observed")
    forecast = sum(1 for d in days if d["source"] == "forecast")
    return {
        "hazards": results,
        "climate": {"summary": summarise_climate(days), "daily": days},
        "coverage": {"observed_days": observed, "forecast_days": forecast,
                     "climatology_years": len(windows), "notes": notes},
    }

# =========================================================
# AUTHENTICATION API ROUTES
# =========================================================
@app.route("/api/auth/register", methods=["POST"])
def auth_register():
    data = request.get_json(force=True, silent=True) or {}
    ok, res = auth.register_user(data.get("email"), data.get("password"), data.get("name"))
    return jsonify(res), 200 if ok else 400


@app.route("/api/auth/verify", methods=["POST"])
def auth_verify():
    data = request.get_json(force=True, silent=True) or {}
    ok, res = auth.verify_user_code(data.get("email"), data.get("code"))
    return jsonify(res), 200 if ok else 400


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    data = request.get_json(force=True, silent=True) or {}
    ok, res = auth.login_user(data.get("email"), data.get("password"))
    return jsonify(res), 200 if ok else 401


@app.route("/api/auth/resend-code", methods=["POST"])
def auth_resend():
    data = request.get_json(force=True, silent=True) or {}
    ok, res = auth.resend_verification_code(data.get("email"))
    return jsonify(res), 200 if ok else 400


@app.route("/api/auth/me", methods=["GET"])
def auth_me():
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.replace("Bearer ", "").strip()
    user = auth.get_user_from_token(token)
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify({"user": user}), 200


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.replace("Bearer ", "").strip()
    auth.logout_user(token)
    return jsonify({"message": "Logged out"}), 200


# =========================================================
# API ROUTES
# =========================================================
@app.route("/api/status")
def status():
    return jsonify({
        "status": "online",
        "system": "Smart Multi-Hazard Disaster Management System",
        "disaster": "Multiple hazards",
        "application": "React + Flask",
    })


@app.route("/api/weather")
def weather():
    city = request.args.get("city", "Vellore").strip()
    try:
        result = get_live_weather(city)
        return (jsonify(result), 404) if "error" in result else jsonify(result)
    except Exception as error:
        app.logger.exception("Weather endpoint failed")
        return jsonify({"error": f"Weather request failed: {type(error).__name__}. Check backend terminal for details."}), 502


@app.route("/api/ml-risk")
def ml_risk():
    city = request.args.get("city", "Vellore").strip()
    try:
        w = get_live_weather(city)
        if "error" in w:
            return jsonify(w), 404
        rainfall = float(w.get("rainfall", 0))
        risk = predict_flood_risk(
            1, max(0, rainfall * 10), 0, 0, max(0, int(rainfall * 2)), 0
        )
        return jsonify({
            "disaster": "Flood", "risk_level": risk, "location": w["location"],
            "temperature": w["temperature"], "humidity": w["humidity"],
            "rainfall": rainfall, "wind_speed": w["wind_speed"],
            "latitude": w["latitude"], "longitude": w["longitude"],
            "weather_source": w["source"], "ml_model": "Custom Decision Tree",
            "ml_status": "Trained model active",
            "notice": "Weather-derived proxy inputs are not a validated real-time flood forecast.",
        })
    except Exception:
        app.logger.exception("Flood model endpoint failed")
        return jsonify({"error": "Unable to calculate flood risk."}), 502


@app.route("/api/resources")
def resources():
    city = request.args.get("city", "Vellore").strip()
    try:
        radius = int(request.args.get("radius", "10000"))
        radius = max(1000, min(radius, 30000))
    except ValueError:
        radius = 10000
    try:
        try:
            lat = float(request.args.get("lat", ""))
            lon = float(request.args.get("lon", ""))
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError("coordinates out of range")
            w = {"location": city, "latitude": lat, "longitude": lon}
        except (TypeError, ValueError):
            w = get_live_weather(city)
            if "error" in w:
                return jsonify(w), 404
        facilities = find_emergency_facilities(w["latitude"], w["longitude"], radius)
        by_type = lambda name: [f for f in facilities if f["type"] == name][:8]
        medical_facilities = [f for f in facilities if f.get("is_medical")]
        return jsonify({
            "location": w["location"], "latitude": w["latitude"], "longitude": w["longitude"],
            "radius_m": radius,
            "source": "Live OpenStreetMap facility data",
            "total_facilities": len(facilities),
            "total_medical_facilities": len(medical_facilities),
            "medical_facilities": medical_facilities[:30],
            "hospitals": by_type("Hospital"),
            "emergency_clinics": by_type("Emergency Clinic"),
            "ambulance_stations": by_type("Ambulance Station"),
            "pharmacies": by_type("Pharmacy"),
            "fire_stations": by_type("Fire Station"),
            "police_stations": by_type("Police Station"),
            "shelters": by_type("Shelter"),
            "community_centres": by_type("Community Centre"),
            "schools": by_type("School"),
            "facilities": facilities[:60],
            "safe_zone": choose_safe_zone(facilities),
            "availability_note": "Mapped facilities only. Direct emergency routing and real-time medical facility locations loaded from live OpenStreetMap records.",
        })
    except Exception as error:
        app.logger.exception("Emergency resources endpoint failed")
        return jsonify({"error": str(error), "facilities": [], "total_facilities": 0}), 502


def weather_from_request_or_lookup(city):
    """Reuse client-fetched live weather when supplied; otherwise fetch it."""
    try:
        lat = float(request.args.get("lat", ""))
        lon = float(request.args.get("lon", ""))
        temp = float(request.args.get("temperature", ""))
        humidity = float(request.args.get("humidity", ""))
        rain = float(request.args.get("rainfall", ""))
        wind = float(request.args.get("wind_speed", ""))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("coordinates out of range")
        if not all(math.isfinite(value) for value in (temp, humidity, rain, wind)):
            raise ValueError("weather values must be finite numbers")
        return {
            "location": city, "latitude": lat, "longitude": lon,
            "temperature": temp, "humidity": humidity, "rainfall": rain,
            "wind_speed": wind, "source": "Open-Meteo Live Weather API",
        }
    except (TypeError, ValueError):
        return get_live_weather(city)


def _range_from_request():
    return parse_range(request.args.get("from", "").strip(), request.args.get("to", "").strip())


@app.route("/api/climate")
def climate_range():
    """Daily climate (observed + forecast) for the selected dates."""
    city = request.args.get("city", "Vellore").strip()
    try:
        start, end = _range_from_request()
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    try:
        w = weather_from_request_or_lookup(city)
        if "error" in w:
            return jsonify(w), 404
        data = get_daily_climate(float(w["latitude"]), float(w["longitude"]), start, end)
        return jsonify({"location": w.get("location"), "from": start.isoformat(), "to": end.isoformat(),
                        "summary": summarise_climate(data["days"]), "daily": data["days"],
                        "notes": data["notes"], "source": "Open-Meteo historical archive + forecast"})
    except Exception as error:
        app.logger.exception("Climate endpoint failed")
        return jsonify({"error": f"Unable to load climate data: {type(error).__name__}"}), 502


@app.route("/api/disaster-risk")
def disaster_risk():
    city = request.args.get("city", "Vellore").strip()
    hazard = request.args.get("disaster", "flood").strip().lower()
    if hazard not in HAZARD_TYPES:
        return jsonify({"error": "Unsupported disaster type", "supported": HAZARD_TYPES}), 400
    try:
        start, end = _range_from_request()
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    try:
        w = weather_from_request_or_lookup(city)
        if "error" in w:
            return jsonify(w), 404
        result = assess_range(w, start, end)
        return jsonify(next(h for h in result["hazards"] if h["disaster_key"] == hazard))
    except Exception:
        app.logger.exception("Single hazard endpoint failed")
        return jsonify({"error": "Unable to calculate risk for this hazard."}), 502


@app.route("/api/all-disaster-risks")
def all_disaster_risks():
    """All 10 hazards for the selected dates. Pass ?from=YYYY-MM-DD&to=YYYY-MM-DD
    (defaults to today .. today+7 if omitted)."""
    city = request.args.get("city", "Vellore").strip()
    try:
        start, end = _range_from_request()
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    try:
        w = weather_from_request_or_lookup(city)
        if "error" in w:
            return jsonify(w), 404
        result = assess_range(w, start, end)
        hazards = result["hazards"]
        return jsonify({
            "status": "success", "location": w.get("location"),
            "from": start.isoformat(), "to": end.isoformat(),
            "count": len(hazards), "hazards": hazards, "assessments": hazards, "results": hazards,
            "climate": result["climate"], "coverage": result["coverage"],
            "notice": "Prototype screening from public data (Open-Meteo, USGS, NASA EONET). DATA UNAVAILABLE means a required request failed; NOT ASSESSED means the period is too short. These are not official warnings.",
        })
    except Exception as error:
        app.logger.exception("All-disaster endpoint failed")
        return jsonify({"error": f"Unable to assess hazards: {type(error).__name__}", "detail": str(error)[:240]}), 502

# =========================================================
# HISTORICAL RECORDS (local CSV datasets + live public catalogs)
# =========================================================
def _normalise_text(value):
    return " ".join(str(value or "").strip().lower().replace("_", " ").replace("-", " ").split())


def _parse_date_or_year(value):
    value = str(value or "").strip()
    if not value:
        return None, None
    # A bare year such as "2023" is a year, NOT 1 January of that year.
    if re.fullmatch(r"(19|20)\d{2}(\.0)?", value):
        return None, int(float(value))
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d", "%d-%b-%Y", "%b %d, %Y"):
        try:
            parsed = datetime.strptime(value, fmt).date()
            return parsed, parsed.year
        except (ValueError, TypeError):
            pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        return parsed, parsed.year
    except (ValueError, TypeError):
        match = re.search(r"\b(19\d{2}|20\d{2})\b", value)
        return (None, int(match.group(1))) if match else (None, None)


_CSV_PATHS = None

def _find_csv_paths():
    global _CSV_PATHS
    if _CSV_PATHS is not None:
        return _CSV_PATHS
    paths, seen = [], set()
    for search_root in (BASE_DIR, PROJECT_DIR):
        for root, dirs, files in os.walk(search_root):
            dirs[:] = [d for d in dirs if d.lower() not in {".git", "node_modules", ".venv", "venv", "__pycache__", "dist"}]
            for filename in files:
                if filename.lower() in HISTORY_CSV_NAMES:
                    path = os.path.abspath(os.path.join(root, filename))
                    if path not in seen:
                        seen.add(path)
                        paths.append(path)
    _CSV_PATHS = paths
    return paths


HISTORY_YEARS = 1
# True  -> events that happened on the SAME calendar dates in each of the previous 3 years
# False -> events from the whole previous 3 years (ending at the start of the searched range)
HISTORY_SAME_DATES = True


HISTORY_YEARS_BACK = 1  # same month and day as entered, one year earlier


def history_windows(start_date, end_date):
    """History covers the exact calendar dates the user entered, shifted back to the previous recorded year."""
    today = date.today()
    years_back = 1
    w_start = _shift_years(start_date, years_back)
    w_end = _shift_years(end_date, years_back)
    while w_start > today and years_back < 10:
        years_back += 1
        w_start = _shift_years(start_date, years_back)
        w_end = _shift_years(end_date, years_back)

    w_end_capped = min(w_end, today)
    if w_start <= w_end_capped:
        return [(w_start, w_end_capped)], years_back
    return [], years_back


def _in_windows(day, windows):
    return any(w_start <= day <= w_end for w_start, w_end in windows)


def _csv_events(csv_paths, location_query, windows, allow_year_only=False):
    """Local CSV records for the location that fall inside the history windows.
    Returns (events, number_of_year_only_records_skipped)."""
    query_norm = _normalise_text(location_query)
    query_terms = [t for t in re.split(r"[,\s]+", query_norm) if len(t) > 2 and t not in {"india", "district", "state", "village", "town"}]
    first_year = min(w[0].year for w in windows)
    last_year = max(w[1].year for w in windows)
    events, skipped_year_only = [], 0
    for path in csv_paths:
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                if not reader.fieldnames:
                    continue
                headers = {h: _normalise_text(h) for h in reader.fieldnames if h}
                location_headers = [h for h, name in headers.items() if any(k in name for k in ("location", "district", "state", "village", "town", "city", "place", "region", "taluk", "tehsil", "area"))]
                date_headers = [h for h, name in headers.items() if any(k in name for k in ("date", "year", "time", "period"))]
                for row in reader:
                    row = {str(k or ""): (v if v is not None else "") for k, v in row.items()}
                    location_values = [str(row.get(h, "")) for h in location_headers] if location_headers else [str(v) for v in row.values()]
                    location_blob = _normalise_text(" ".join(location_values))
                    if not location_blob:
                        continue
                    matched = bool(query_norm and (query_norm in location_blob or (len(location_blob) >= 4 and location_blob in query_norm)))
                    if not matched:
                        matched = any(term in location_blob for term in query_terms)
                    if not matched:
                        continue
                    # Prefer a full date; fall back to a bare year only if no full date exists.
                    raw_date, parsed_date, parsed_year, year_only = "", None, None, None
                    for header in date_headers:
                        candidate = str(row.get(header, "")).strip()
                        parsed, year = _parse_date_or_year(candidate)
                        if parsed:
                            raw_date, parsed_date, parsed_year = candidate, parsed, year
                            break
                        if year and year_only is None:
                            year_only = (candidate, year)
                    if parsed_date is None and year_only:
                        raw_date, parsed_year = year_only
                    if parsed_date:
                        if not _in_windows(parsed_date, windows):
                            continue
                    elif parsed_year:
                        if not (first_year <= parsed_year <= last_year):
                            continue
                        if not allow_year_only:
                            skipped_year_only += 1  # cannot prove it happened on the searched dates
                            continue
                    else:
                        continue
                    display_location = next((str(row.get(h, "")).strip() for h in location_headers if str(row.get(h, "")).strip()), location_query)
                    filename = os.path.basename(path)
                    events.append({
                        "disaster": "Flood" if "flood" in filename.lower() or "flood" in " ".join(headers.values()) else "Historical disaster record",
                        "date": raw_date or str(parsed_year), "location": display_location,
                        "source_file": filename, "details": row,
                    })
                    if len(events) >= 300:
                        break
        except (OSError, csv.Error, UnicodeError) as error:
            app.logger.warning("Could not read history CSV %s: %s", path, error)
    return events, skipped_year_only


def _usgs_history(lat, lon, start_date, end_date, location_query):
    events = []
    for feature in usgs_query(lat, lon, 250, start_date, end_date, 2.0, 500):
        props = feature.get("properties", {})
        coords = (feature.get("geometry") or {}).get("coordinates") or []
        if not props.get("time"):
            continue
        event_date = datetime.fromtimestamp(props["time"] / 1000, tz=timezone.utc).date()
        events.append({
            "disaster": "Earthquake", "date": event_date.isoformat(),
            "location": props.get("place") or location_query,
            "source_file": "USGS Earthquake Catalog (live API)", "source_url": props.get("url"),
            "details": {"magnitude": props.get("mag"), "depth_km": coords[2] if len(coords) > 2 else None,
                        "latitude": coords[1] if len(coords) > 1 else None,
                        "longitude": coords[0] if len(coords) > 0 else None,
                        "status": props.get("status"), "event_id": feature.get("id"),
                        "distance_filter": "within 250 km of searched coordinates"},
        })
    return events


def _eonet_history(lat, lon, start_date, end_date, location_query):
    events = []
    for event in fetch_eonet(lat, lon, start_date, end_date, deg=2.5, limit=200):
        dist = eonet_nearest(event, lat, lon)
        if dist is None or dist > 250:
            continue
        day = None
        for geometry in event.get("geometry", []) or []:
            try:
                d = date.fromisoformat(str(geometry.get("date", ""))[:10])
            except ValueError:
                continue
            if start_date <= d <= end_date:
                day = d
                break
        if day is None:
            continue
        names = [c.get("title", "") for c in event.get("categories", [])]
        events.append({
            "disaster": ", ".join(names) or "Natural event", "date": day.isoformat(),
            "location": event.get("title") or location_query,
            "source_file": "NASA EONET (live API)", "source_url": event.get("link"),
            "details": {"event_id": event.get("id"), "title": event.get("title"),
                        "categories": names, "distance_km": round(dist),
                        "distance_filter": "event within ~250 km of searched coordinates"},
        })
    return events


def _weather_extreme_history(lat, lon, start_date, end_date, location_query):
    """Real observed extreme-weather days (Open-Meteo archive / ERA5 reanalysis)."""
    today = date.today()
    if start_date > today:
        return []
    data = get_daily_climate(lat, lon, start_date, min(end_date, today))
    events = []
    for d in data["days"]:
        checks = []
        if (d.get("rain") or 0) >= 115.6:
            checks.append(("Very heavy rainfall", f"{d['rain']} mm in 24 h"))
        elif (d.get("rain") or 0) >= 64.5:
            checks.append(("Heavy rainfall", f"{d['rain']} mm in 24 h"))
        elif (d.get("rain") or 0) >= 35.5:
            checks.append(("Moderate-heavy rainfall", f"{d['rain']} mm in 24 h"))
        if (d.get("tmax") or 0) >= 42:
            checks.append(("Severe heatwave", f"peak {d['tmax']} °C"))
        elif (d.get("tmax") or 0) >= 39:
            checks.append(("Extreme heat", f"max {d['tmax']} °C"))
        if (d.get("gust") or 0) >= 88:
            checks.append(("Violent storm gusts", f"gusts {d['gust']} km/h"))
        elif (d.get("gust") or 0) >= 60:
            checks.append(("Damaging winds", f"gusts {d['gust']} km/h"))
        if (d.get("snow") or 0) >= 10:
            checks.append(("Heavy snowfall / blizzard", f"snow {d['snow']} cm"))
        for kind, detail in checks:
            events.append({
                "disaster": kind, "date": d["date"], "location": location_query,
                "source_file": "Open-Meteo historical weather (reanalysis)",
                "details": {"observation": detail, "note": "Real recorded meteorological extreme event on this date."},
            })
    return events


@app.route("/api/history")
def disaster_history():
    location_query = request.args.get("city", "Vellore").strip()
    from_value = request.args.get("from", "").strip()
    to_value = request.args.get("to", "").strip()
    try:
        start_date = date.fromisoformat(from_value) if from_value else date.today()
        end_date = date.fromisoformat(to_value) if to_value else start_date + timedelta(days=7)
    except ValueError:
        return jsonify({"error": "Dates must use YYYY-MM-DD format."}), 400
    if start_date > end_date:
        return jsonify({"error": "The From date must be before or equal to the To date."}), 400

    windows, years_back = history_windows(start_date, end_date)
    mode = f"Exact matching dates from previous year ({years_back} year{'s' if years_back > 1 else ''} prior)"
    if not windows:
        return jsonify({"location": location_query, "from": start_date.isoformat(), "to": end_date.isoformat(),
                        "history_mode": mode, "windows": [], "total_events": 0, "events": [],
                        "sources_found": [], "sources_failed": [],
                        "notice": "The selected dates are in the future and have not yet completed a previous year cycle."})

    csv_paths = _find_csv_paths()
    events, skipped_year_only = _csv_events(csv_paths, location_query, windows,
                                            allow_year_only=False)

    location = None
    try:
        lat = float(request.args.get("lat", ""))
        lon = float(request.args.get("lon", ""))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("coordinates out of range")
        location = {"latitude": lat, "longitude": lon}
    except (TypeError, ValueError):
        location = get_coordinates(location_query)

    source_ok = {}
    if location:
        lat, lon = float(location["latitude"]), float(location["longitude"])
        jobs = []
        for w_start, w_end in windows:
            args = (lat, lon, w_start, w_end, location_query)
            jobs.append(("USGS Earthquake Catalog", _usgs_history, args))
            jobs.append(("NASA EONET", _eonet_history, args))
            jobs.append(("Open-Meteo historical weather", _weather_extreme_history, args))
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [(name, pool.submit(_safe, fn, *args)) for name, fn, args in jobs]
            for name, future in futures:
                result = future.result()
                source_ok[name] = source_ok.get(name, True) and result is not None
                if result:
                    events += result

    unique_events, seen_events = [], set()
    for event in events:
        key = (event.get("source_file"), event.get("date"), event.get("location"), json.dumps(event.get("details", {}), sort_keys=True, ensure_ascii=False, default=str))
        if key not in seen_events:
            seen_events.add(key)
            unique_events.append(event)
    unique_events.sort(key=lambda item: str(item.get("date", "")), reverse=True)

    sources = sorted(set([os.path.basename(p) for p in csv_paths] + [n for n, ok in source_ok.items() if ok]))
    notice = (
        f"History window: {mode.lower()}. Only events that were recorded inside that window are listed. "
        "Sources: matching local CSV records, USGS earthquakes within 100 km, NASA EONET events within ~150 km, "
        "and observed extreme-weather days (Open-Meteo reanalysis). These catalogs are not comprehensive; "
        "no result does not prove no disaster occurred."
    )
    if skipped_year_only:
        notice += (f" {skipped_year_only} local CSV record(s) only had a year (no exact date), so they cannot be "
                   "matched to the searched dates and were left out.")
    return jsonify({
        "location": location_query,
        "from": min(w[0] for w in windows).isoformat(), "to": max(w[1] for w in windows).isoformat(),
        "history_mode": mode,
        "windows": [{"from": w[0].isoformat(), "to": w[1].isoformat()} for w in windows],
        "total_events": len(unique_events), "events": unique_events[:200],
        "sources_found": sources,
        "sources_failed": sorted(n for n, ok in source_ok.items() if not ok),
        "year_only_records_excluded": skipped_year_only,
        "notice": notice,
    })


# =========================================================
# REACT FRONTEND FALLBACK (KEEP AFTER API ROUTES)
# =========================================================
@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_react(path):
    if path and os.path.isfile(os.path.join(FRONTEND_DIST, path)):
        return send_from_directory(FRONTEND_DIST, path)
    index_path = os.path.join(FRONTEND_DIST, "index.html")
    if os.path.isfile(index_path):
        return send_from_directory(FRONTEND_DIST, "index.html")
    return jsonify({
        "application": "React + Flask",
        "status": "online",
        "message": "Backend API is running. Start the Vite frontend separately during local development.",
    }), 200


if __name__ == "__main__":
    print("=" * 60)
    print("SMART MULTI-HAZARD DISASTER MANAGEMENT APPLICATION")
    print("=" * 60)
    print("Frontend:", FRONTEND_DIST)
    print("Server: http://127.0.0.1:5001 (local development)")
    print("=" * 60)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5001")), debug=False)