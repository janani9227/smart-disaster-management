from risk_model import predict_flood_risk
from risk_model import predict_flood_risk
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from risk_model import predict_flood_risk

import urllib.request
import urllib.parse
import json
import math
import os


# =========================================================
# APPLICATION PATHS
# =========================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIST = os.path.abspath(
    os.path.join(BASE_DIR, "..", "frontend", "dist")
)


# =========================================================
# FLASK APPLICATION
# =========================================================

app = Flask(
    __name__,
    static_folder=os.path.join(FRONTEND_DIST, "assets"),
    static_url_path="/assets"
)

CORS(app)

APP_USER_AGENT = "SmartDisasterManagement/1.0"


# =========================================================
# HELPER: HTTP GET JSON
# =========================================================

def get_json(url, headers=None, timeout=15):

    req = urllib.request.Request(
        url,
        headers=headers or {
            "User-Agent": APP_USER_AGENT
        }
    )

    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode())


# =========================================================
# LOCATION SEARCH
# =========================================================

def get_coordinates(location_name):

    # First try Open-Meteo
    encoded = urllib.parse.quote(location_name)

    url = (
        "https://geocoding-api.open-meteo.com/v1/search"
        f"?name={encoded}"
        "&count=10"
        "&language=en"
        "&format=json"
        "&countryCode=IN"
    )

    try:

        data = get_json(url)

        results = data.get("results", [])

        if results:

            # Prefer populated places/localities
            preferred = []

            for result in results:

                feature = result.get("feature_code", "")

                if feature.startswith("PPL"):
                    preferred.append(result)

            if preferred:
                result = preferred[0]
            else:
                result = results[0]

            return {
                "name": result.get("name", location_name),
                "latitude": result["latitude"],
                "longitude": result["longitude"],
                "country": result.get("country", "India"),
                "state": result.get("admin1", ""),
                "district": result.get("admin2", ""),
                "postcode": (
                    result.get("postcodes", [""])[0]
                    if result.get("postcodes")
                    else ""
                )
            }

    except Exception as error:

        print("Open-Meteo geocoding failed:", error)


    # Fallback to OpenStreetMap Nominatim
    try:

        params = urllib.parse.urlencode({
            "q": location_name + ", India",
            "format": "jsonv2",
            "limit": 1,
            "addressdetails": 1
        })

        url = (
            "https://nominatim.openstreetmap.org/search?"
            + params
        )

        data = get_json(
            url,
            headers={
                "User-Agent": APP_USER_AGENT
            }
        )

        if data:

            result = data[0]
            address = result.get("address", {})

            return {
                "name": (
                    address.get("village")
                    or address.get("town")
                    or address.get("city")
                    or address.get("suburb")
                    or result.get("display_name", location_name)
                ),
                "latitude": float(result["lat"]),
                "longitude": float(result["lon"]),
                "country": address.get("country", "India"),
                "state": address.get("state", ""),
                "district": (
                    address.get("state_district")
                    or address.get("district")
                    or ""
                ),
                "postcode": address.get("postcode", "")
            }

    except Exception as error:

        print("Nominatim fallback failed:", error)


    return None


# =========================================================
# LIVE WEATHER
# =========================================================

def get_live_weather(location_name):

    location = get_coordinates(location_name)

    if location is None:

        return {
            "error": "Location not found"
        }

    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={location['latitude']}"
        f"&longitude={location['longitude']}"
        "&current=temperature_2m,"
        "relative_humidity_2m,"
        "precipitation,"
        "wind_speed_10m"
        "&temperature_unit=celsius"
        "&wind_speed_unit=kmh"
        "&precipitation_unit=mm"
        "&timezone=auto"
    )

    data = get_json(url)

    current = data["current"]

    return {
        "location": location["name"],
        "country": location["country"],
        "state": location["state"],
        "district": location["district"],
        "postcode": location["postcode"],

        "latitude": location["latitude"],
        "longitude": location["longitude"],

        "temperature": round(
            current["temperature_2m"], 1
        ),

        "humidity": round(
            current["relative_humidity_2m"]
        ),

        "rainfall": round(
            current["precipitation"], 1
        ),

        "wind_speed": round(
            current["wind_speed_10m"], 1
        ),

        "source": "Open-Meteo Live Weather API"
    }


# =========================================================
# DISTANCE
# =========================================================

def calculate_distance(
    lat1,
    lon1,
    lat2,
    lon2
):

    earth_radius = 6371

    lat1 = math.radians(lat1)
    lat2 = math.radians(lat2)

    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_lat / 2) ** 2
        +
        math.cos(lat1)
        * math.cos(lat2)
        * math.sin(delta_lon / 2) ** 2
    )

    c = 2 * math.atan2(
        math.sqrt(a),
        math.sqrt(1 - a)
    )

    return earth_radius * c


# =========================================================
# OPENSTREETMAP EMERGENCY FACILITIES
# =========================================================

# =========================================================
# OPENSTREETMAP EMERGENCY FACILITIES
# =========================================================

def find_emergency_facilities(latitude, longitude):

    query = f"""
    [out:json][timeout:30];

    (
        nwr["amenity"="hospital"](around:10000,{latitude},{longitude});
        nwr["amenity"="fire_station"](around:10000,{latitude},{longitude});
        nwr["amenity"="police"](around:10000,{latitude},{longitude});
        nwr["amenity"="shelter"](around:10000,{latitude},{longitude});
        nwr["amenity"="community_centre"](around:10000,{latitude},{longitude});
        nwr["amenity"="school"](around:10000,{latitude},{longitude});
    );

    out center tags;
    """

    encoded_query = urllib.parse.quote(query)

    # Try multiple Overpass servers.
    # If one server is busy/unavailable, another can respond.
    overpass_servers = [
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
        "https://overpass.private.coffee/api/interpreter"
    ]

    data = None

    for server in overpass_servers:

        url = (
            server
            + "?data="
            + encoded_query
        )

        try:

            print(
                "Trying Overpass server:",
                server
            )

            data = get_json(
                url,
                headers={
                    "User-Agent":
                    APP_USER_AGENT
                },
                timeout=40
            )

            print(
                "Overpass request successful:",
                server
            )

            break

        except Exception as error:

            print(
                "Overpass server failed:",
                server,
                error
            )

    # If all servers failed
    if data is None:

        print(
            "All Overpass servers failed."
        )

        return []


    facilities = []

    type_names = {

        "hospital":
            "Hospital",

        "fire_station":
            "Fire Station",

        "police":
            "Police Station",

        "shelter":
            "Shelter",

        "community_centre":
            "Community Centre",

        "school":
            "School"
    }


    for element in data.get(
        "elements",
        []
    ):

        tags = element.get(
            "tags",
            {}
        )

        amenity = tags.get(
            "amenity"
        )

        if amenity not in type_names:
            continue


        # -----------------------------
        # GET FACILITY COORDINATES
        # -----------------------------

        if (
            "lat" in element
            and
            "lon" in element
        ):

            facility_lat = element["lat"]
            facility_lon = element["lon"]

        elif "center" in element:

            facility_lat = (
                element["center"]["lat"]
            )

            facility_lon = (
                element["center"]["lon"]
            )

        else:

            continue


        # -----------------------------
        # CALCULATE DISTANCE
        # -----------------------------

        distance = calculate_distance(

            latitude,
            longitude,

            facility_lat,
            facility_lon

        )


        # -----------------------------
        # FACILITY NAME
        # -----------------------------

        name = tags.get(
            "name"
        )

        if not name:

            name = type_names[
                amenity
            ]


        # -----------------------------
        # OPENING HOURS
        # -----------------------------

        opening_hours = tags.get(

            "opening_hours",

            "Not available in map data"

        )


        # -----------------------------
        # PHONE
        # -----------------------------

        phone = (

            tags.get("phone")

            or

            tags.get("contact:phone")

            or

            "Not available"

        )


        facilities.append({

            "name":
                name,

            "type":
                type_names[amenity],

            "latitude":
                facility_lat,

            "longitude":
                facility_lon,

            "distance":
                round(
                    distance,
                    2
                ),

            "opening_hours":
                opening_hours,

            "phone":
                phone,

            "source":
                "OpenStreetMap"

        })


    # -----------------------------
    # REMOVE DUPLICATES
    # -----------------------------

    unique = {}

    for facility in facilities:

        key = (

            facility["name"],

            facility["type"]

        )

        if key not in unique:

            unique[key] = facility

        elif (

            facility["distance"]

            <

            unique[key]["distance"]

        ):

            unique[key] = facility


    facilities = list(
        unique.values()
    )


    # -----------------------------
    # CLOSEST FIRST
    # -----------------------------

    facilities.sort(

        key=lambda x:
        x["distance"]

    )


    print(
        "Total mapped emergency facilities:",
        len(facilities)
    )


    return facilities

# =========================================================
# SAFE-ZONE RECOMMENDATION
# =========================================================

def choose_safe_zone(facilities):

    if not facilities:

        return {
            "found": False,
            "message": (
                "No suitable nearby facility "
                "was found in the map data."
            )
        }


    # Prototype priority.
    # NOT an official government safety certification.

    priority = {
        "Shelter": 1,
        "Community Centre": 2,
        "School": 3,
        "Hospital": 4,
        "Fire Station": 5,
        "Police Station": 6
    }


    candidates = []

    for facility in facilities:

        score = (
            priority.get(
                facility["type"],
                10
            ),
            facility["distance"]
        )

        candidates.append(
            (score, facility)
        )


    candidates.sort(
        key=lambda x: x[0]
    )


    recommended = candidates[0][1]


    return {

        "found": True,

        "name": recommended["name"],

        "type": recommended["type"],

        "latitude": recommended["latitude"],

        "longitude": recommended["longitude"],

        "distance": recommended["distance"],

        "opening_hours": (
            recommended["opening_hours"]
        ),

        "message": (
            "Recommended nearby emergency "
            "location based on mapped facility "
            "type and distance."
        ),

        "warning": (
            "This is a map-based recommendation, "
            "not an official declaration that the "
            "location is safe during an active disaster."
        )
    }


# =========================================================
# SERVE REACT APPLICATION
# =========================================================

@app.route("/")
def serve_react():

    return send_from_directory(
        FRONTEND_DIST,
        "index.html"
    )


@app.route("/<path:path>")
def serve_react_files(path):

    requested_file = os.path.join(
        FRONTEND_DIST,
        path
    )

    if os.path.isfile(requested_file):

        return send_from_directory(
            FRONTEND_DIST,
            path
        )

    # React routing fallback
    return send_from_directory(
        FRONTEND_DIST,
        "index.html"
    )


# =========================================================
# STATUS
# =========================================================

@app.route("/api/status")
def status():

    return jsonify({

        "status": "online",

        "system":
        "Smart Flood Disaster Management System",

        "disaster":
        "Flood",

        "application":
        "React + Flask"
    })


# =========================================================
# WEATHER
# =========================================================

@app.route("/api/weather")
def weather():

    location = request.args.get(
        "city",
        "Vellore"
    )

    try:

        data = get_live_weather(location)

        if "error" in data:

            return jsonify(data), 404

        return jsonify(data)

    except Exception as error:

        return jsonify({
            "error": str(error)
        }), 500


# =========================================================
# FLOOD RISK
# =========================================================

@app.route("/api/ml-risk")
def ml_risk():

    location = request.args.get(
        "city",
        "Vellore"
    )

    try:

        # Get live weather information
        weather_data = get_live_weather(location)

        if "error" in weather_data:
            return jsonify(weather_data), 404

        # -------------------------------------------------
        # ML FLOOD RISK PREDICTION
        # -------------------------------------------------
        #
        # IMPORTANT:
        # The current trained model uses historical
        # flood-impact features.
        #
        # Live weather values are therefore used as a
        # proxy/input for the current application demo.
        #
        # This is NOT being presented as a scientifically
        # validated real-time flood forecast.
        # -------------------------------------------------

        rainfall = float(weather_data.get("rainfall", 0))
        temperature = float(weather_data.get("temperature", 0))
        humidity = float(weather_data.get("humidity", 0))
        wind_speed = float(weather_data.get("wind_speed", 0))

        # Convert live weather into model-compatible
        # demonstration inputs.
        duration = 1

        area_affected = max(
            0,
            rainfall * 10
        )

        human_fatality = 0

        human_injured = 0

        human_displaced = max(
            0,
            int(rainfall * 2)
        )

        animal_fatality = 0

        risk_level = predict_flood_risk(
            duration,
            area_affected,
            human_fatality,
            human_injured,
            human_displaced,
            animal_fatality
        )

        return jsonify({

            "disaster": "Flood",

            "risk_level": risk_level,

            "location":
                weather_data["location"],

            "temperature":
                temperature,

            "humidity":
                humidity,

            "rainfall":
                rainfall,

            "wind_speed":
                wind_speed,

            "latitude":
                weather_data["latitude"],

            "longitude":
                weather_data["longitude"],

            "weather_source":
                weather_data["source"],

            "ml_model":
                "Custom Decision Tree",

            "ml_status":
                "Trained model active"

        })

    except Exception as error:

        return jsonify({
            "error": str(error)
        }), 500


# =========================================================
# REAL EMERGENCY RESOURCES
# =========================================================
@app.route("/api/resources")
def resources():

    location = request.args.get(
        "city",
        "Vellore"
    )


    try:

        weather_data = get_live_weather(
            location
        )

        if "error" in weather_data:

            return jsonify(
                weather_data
            ), 404


        facilities = find_emergency_facilities(

            weather_data["latitude"],

            weather_data["longitude"]

        )


        # Categorize real mapped facilities

        hospitals = [
            x for x in facilities
            if x["type"] == "Hospital"
        ]

        fire_stations = [
            x for x in facilities
            if x["type"] == "Fire Station"
        ]

        police_stations = [
            x for x in facilities
            if x["type"] == "Police Station"
        ]

        shelters = [
            x for x in facilities
            if x["type"] == "Shelter"
        ]

        community_centres = [
            x for x in facilities
            if x["type"] == "Community Centre"
        ]

        schools = [
            x for x in facilities
            if x["type"] == "School"
        ]


        safe_zone = choose_safe_zone(
            facilities
        )


        return jsonify({

            "location":
                weather_data["location"],

            "latitude":
                weather_data["latitude"],

            "longitude":
                weather_data["longitude"],

            "source":
                "Live OpenStreetMap facility data",

            "total_facilities":
                len(facilities),

            "hospitals":
                hospitals[:5],

            "fire_stations":
                fire_stations[:5],

            "police_stations":
                police_stations[:5],

            "shelters":
                shelters[:5],

            "community_centres":
                community_centres[:5],

            "schools":
                schools[:5],

            "safe_zone":
                safe_zone,

            "availability_note":
                (
                    "These are real mapped facilities. "
                    "The system does not invent ambulance, "
                    "bed, food or water quantities. "
                    "Actual real-time inventory requires "
                    "a connected emergency-service "
                    "inventory system."
                )
        })


    except Exception as error:

        return jsonify({
            "error": str(error)
        }), 500


# =========================================================
# RUN SERVER
# =========================================================

if __name__ == "__main__":

    print("=" * 60)
    print("SMART FLOOD DISASTER MANAGEMENT APPLICATION")
    print("=" * 60)
    print("Frontend:", FRONTEND_DIST)
    print("Server: http://127.0.0.1:5000")
    print("=" * 60)

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False
    )

