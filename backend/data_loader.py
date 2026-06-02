# backend/data_loader.py
import json
from itertools import permutations
from pathlib import Path

from langchain.text_splitter import RecursiveCharacterTextSplitter

DATASET_FILE = Path("data.json")
POLICY_FOLDER = Path("attachment")
HOLIDAY_FILE = Path("holiday_calendar.json")

with open(DATASET_FILE, "r", encoding="utf-8") as f:
    data = json.load(f)

districts = data["districts"]
providers = data["bus_providers"]
routes = data.get("routes", [])
special_services = data.get("special_services", {})
general_info = data.get("general_info", {})

if HOLIDAY_FILE.exists():
    with open(HOLIDAY_FILE, "r", encoding="utf-8") as f:
        holiday_calendar = json.load(f)
else:
    holiday_calendar = {"holidays": []}

provider_name_lookup = {
    provider["name"].lower().replace("_", " "): provider["name"].lower()
    for provider in providers
}


def format_value(value):
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    if isinstance(value, dict):
        return "; ".join(f"{key}: {format_value(item)}" for key, item in value.items())
    return str(value)


def format_extra_fields(item, exclude_keys):
    lines = []
    for key, value in item.items():
        if key in exclude_keys:
            continue
        lines.append(f"{key.replace('_', ' ').title()}: {format_value(value)}")
    return "\n".join(lines)


def format_dropping_point(point):
    return f"- {point['name']}: {point.get('price', point.get('fare', 'N/A'))} Taka"


def route_key(route):
    return (route.get("from"), route.get("to"))


def provider_coverage():
    coverage = {provider["name"]: set() for provider in providers}
    for route in routes:
        for schedule in route.get("provider_schedules", []):
            provider = schedule.get("provider")
            if provider in coverage:
                coverage[provider].update([route.get("from"), route.get("to")])
    return coverage


def format_service(provider_name, service):
    times = ", ".join(service.get("departure_times", [])) or "not listed"
    return (
        f"- Provider: {provider_name}; Bus type: {service.get('bus_type')}; "
        f"Fare: {service.get('fare')} Taka; Departure times: {times}"
    )


all_chunks = []

for district in districts:
    dropping_points = "\n".join(format_dropping_point(point) for point in district["dropping_points"])
    all_chunks.append({
        "content": f"District: {district['name']}\nDropping points and local fares:\n{dropping_points}",
        "metadata": {
            "type": "district",
            "district": district["name"]
        }
    })

    for point in district["dropping_points"]:
        all_chunks.append({
            "content": (
                f"Dropping point: {point['name']} in {district['name']}.\n"
                f"Local listed fare: {point.get('price', point.get('fare', 'N/A'))} Taka."
            ),
            "metadata": {
                "type": "dropping_point",
                "district": district["name"],
                "point": point["name"],
                "price": point.get("price", point.get("fare", 0))
            }
        })

coverage_by_provider = provider_coverage()
for provider in providers:
    coverage = sorted(coverage_by_provider.get(provider["name"], []))
    extra_fields = format_extra_fields(provider, {"name"})
    provider_text = (
        f"Bus Provider: {provider['name']}\n"
        f"Scheduled districts: {', '.join(coverage) if coverage else 'None listed'}\n"
        f"{extra_fields}"
    ).strip()
    all_chunks.append({
        "content": provider_text,
        "metadata": {
            "type": "provider",
            "provider": provider["name"].lower(),
            "districts": coverage
        }
    })

route_pairs = {route_key(route) for route in routes}
for route in routes:
    source = route.get("from")
    destination = route.get("to")
    service_lines = []
    available_providers = []

    for schedule in route.get("provider_schedules", []):
        provider = schedule.get("provider")
        available_providers.append(provider)
        for service in schedule.get("services", []):
            service_lines.append(format_service(provider, service))

    services_text = "\n".join(service_lines) if service_lines else "No scheduled services listed."
    route_text = (
        f"Route availability summary:\n"
        f"Route: from {source} to {destination}\n"
        f"Common user question: buses from {source} to {destination}\n"
        f"Source district: {source}\n"
        f"Destination district: {destination}\n"
        f"Distance: {route.get('distance_km')} km\n"
        f"Average duration: {route.get('avg_duration_hours')} hours\n"
        f"Available providers: {', '.join(available_providers) if available_providers else 'None'}\n"
        f"Scheduled services, fares, bus types, and departure times:\n{services_text}"
    )
    all_chunks.append({
        "content": route_text,
        "metadata": {
            "type": "route_summary",
            "from_district": source,
            "to_district": destination,
            "providers": available_providers
        }
    })

    for schedule in route.get("provider_schedules", []):
        provider = schedule.get("provider")
        provider_services = "\n".join(format_service(provider, service) for service in schedule.get("services", []))
        all_chunks.append({
            "content": (
                f"Route provider availability:\n"
                f"Route: from {source} to {destination}\n"
                f"Provider: {provider}\n"
                f"Status: available\n"
                f"Distance: {route.get('distance_km')} km\n"
                f"Average duration: {route.get('avg_duration_hours')} hours\n"
                f"Services:\n{provider_services}"
            ),
            "metadata": {
                "type": "route_provider",
                "provider": provider.lower(),
                "from_district": source,
                "to_district": destination,
                "available": "true"
            }
        })

district_names = [district["name"] for district in districts]
for source, destination in permutations(district_names, 2):
    if (source, destination) in route_pairs:
        continue
    all_chunks.append({
        "content": (
            f"Route availability summary:\n"
            f"Route: from {source} to {destination}\n"
            f"Available providers: None\n"
            f"No scheduled service is listed in data.json for this exact direction."
        ),
        "metadata": {
            "type": "route_summary",
            "from_district": source,
            "to_district": destination,
            "providers": []
        }
    })

if special_services:
    all_chunks.append({
        "content": (
            "Special services and discounts. Mention these only when the user asks about "
            f"special services/discounts/holidays:\n{format_value(special_services)}"
        ),
        "metadata": {"type": "special_services"}
    })

if holiday_calendar.get("holidays"):
    all_chunks.append({
        "content": (
            "Holiday calendar for automatic holiday fare rules. Mention holiday effects only "
            f"when the travel date falls within these ranges or the user asks about holidays:\n"
            f"{format_value(holiday_calendar)}"
        ),
        "metadata": {"type": "holiday_calendar"}
    })

if general_info:
    all_chunks.append({
        "content": f"General bus booking information:\n{format_value(general_info)}",
        "metadata": {"type": "general_info"}
    })

text_splitter = RecursiveCharacterTextSplitter(
    chunk_size=500,
    chunk_overlap=150
)

policy_files = list(POLICY_FOLDER.glob("*.txt"))

for txt_file in policy_files:
    provider_name = txt_file.stem.lower().replace("_", " ")
    provider_key = provider_name_lookup.get(provider_name, provider_name)

    with open(txt_file, "r", encoding="utf-8") as f:
        policy_text = f.read().strip()

    wrapped_text = f"Policy and contact information of {provider_key} bus:\n\n{policy_text}"
    for i, chunk in enumerate(text_splitter.split_text(wrapped_text)):
        all_chunks.append({
            "content": chunk,
            "metadata": {
                "type": "policy",
                "provider": provider_key,
                "chunk_index": i
            }
        })

print("\n======================================")
print("Total Chunks Created:", len(all_chunks))
print("======================================\n")

OUTPUT_FILE = "chunks.txt"

with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    for index, chunk in enumerate(all_chunks):
        f.write("============================================================\n")
        f.write(f"CHUNK #{index}\n")
        f.write("------------------------------------------------------------\n")
        f.write("CONTENT:\n")
        f.write(chunk["content"] + "\n\n")
        f.write("METADATA:\n")
        for key, value in chunk["metadata"].items():
            f.write(f"  {key}: {value}\n")
        f.write("============================================================\n\n")

print(f"Dumped {len(all_chunks)} chunks to chunks.txt")
