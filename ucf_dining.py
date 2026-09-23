import requests
import json
from datetime import date, datetime
import os

# ============================================================
# UCF Dining Menu Fetcher
# Queries both 63 South and Knightros via the elevate-dxp API
# ============================================================

ENDPOINT = "https://api.elevate-dxp.com/api/mesh/c087f756-cc72-4649-a36f-3a41b700c519/graphql"

HEADERS = {
    "accept": "application/graphql-response+json,application/json;q=0.9",
    "accept-encoding": "gzip, deflate, br, zstd",
    "accept-language": "en-US,en;q=0.9",
    "aem-elevate-clientpath": "ch/ucf/en",
    "content-type": "application/json",
    "magento-customer-group": "b6589fc6ab0dc82cf12099d1c2d40ab994e8410c",
    "magento-store-code": "ch_ucf",
    "magento-store-view-code": "ch_ucf_en",
    "magento-website-code": "ch_ucf",
    "origin": "https://ucf.mydininghub.com",
    "referer": "https://ucf.mydininghub.com/",
    "store": "ch_ucf_en",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
    "x-api-key": "ElevateAPIProd",
}

GRAPHQL_QUERY = """query getLocationRecipes($campusUrlKey:String!$locationUrlKey:String!$date:String!$mealPeriod:Int$viewType:Commerce_MenuViewType!){getLocationRecipes(campusUrlKey:$campusUrlKey locationUrlKey:$locationUrlKey date:$date mealPeriod:$mealPeriod viewType:$viewType){locationRecipesMap{skus stationSkuMap{id skus __typename}dateSkuMap{date stations{id skus{simple configurable{sku variants __typename}__typename}__typename}__typename}__typename}products{items{id name sku images{label roles url __typename}attributes{name value __typename}...on Catalog_SimpleProductView{price{final{amount{currency value __typename}__typename}__typename}__typename}...on Catalog_ComplexProductView{options{title values{id title ...on Catalog_ProductViewOptionValueProduct{product{name sku attributes{name value __typename}price{final{amount{value currency __typename}__typename}__typename}__typename}__typename}__typename}__typename}__typename}__typename}__typename}__typename}}"""

LOCATIONS = {
    "63 South": "63-south",
    "Knightros": "knightros",
}

STATION_NAMES = {
    # Knightros
    3060: "Grill",
    3063: "Salad Bar",
    3066: "Sides",
    3069: "Feature",
    3072: "International",
    3075: "Entrees",
    3078: "Deli",
    3081: "Desserts",
    3084: "Pizza",
    3087: "Soup",
    # 63 South
    3102: "Breakfast Entrees",
    3111: "Bakery",
    3105: "Entrees",
    3108: "International",
    3114: "Pizza",
    3096: "Grill",
    3090: "Wraps",
    3120: "Specials",
    3117: "Soup",
    273449: "Tortilla Chips",
    273044: "Gluten-Free",
    293477: "Beverages",
}

MEAL_PERIODS = {
    "breakfast": 10,   # UNKNOWN — needs verification
    "lunch": 25,       # CONFIRMED from your captures
    "dinner": 16,      # UNKNOWN — needs verification
}


def get_attr(attributes, name):
    """Extract a named attribute value from the attributes list."""
    for attr in attributes:
        if attr.get("name") == name:
            return attr.get("value")
    return None


def fetch_menu(location_url_key, meal_period, target_date=None):
    """Fetch raw menu data from the API."""
    if target_date is None:
        target_date = date.today().isoformat()

    params = {
        "query": GRAPHQL_QUERY,
        "operationName": "getLocationRecipes",
        "variables": json.dumps({
            "campusUrlKey": "campus",
            "locationUrlKey": location_url_key,
            "date": target_date,
            "mealPeriod": meal_period,
            "viewType": "DAILY"
        }),
        "extensions": json.dumps({
            "clientLibrary": {
                "name": "@apollo/client",
                "version": "4.2.3"
            }
        })
    }

    response = requests.get(ENDPOINT, params=params, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return response.json()


def parse_menu(raw_data):
    """
    Parse the API response into a clean dict:
    { station_id: [{ name, calories, description, allergens, ingredients }, ...] }
    
    The API returns:
    - dateSkuMap: which SKUs belong to which station on which date
    - products: full product details keyed by SKU
    
    We join them to get station -> items.
    """
    try:
        location_data = raw_data["data"]["getLocationRecipes"]
    except (KeyError, TypeError):
        return {}

    recipes_map = location_data.get("locationRecipesMap", {})
    products_list = location_data.get("products", {}).get("items", [])

    # Build SKU -> product lookup
    sku_to_product = {}
    for product in products_list:
        sku = product.get("sku")
        if sku:
            sku_to_product[sku] = product

    # Build stationId -> station name lookup (IDs only — names not in this response)
    # We'll use the station IDs as keys for now
    station_sku_map = {}
    for station in recipes_map.get("stationSkuMap", []):
        station_id = station.get("id")
        station_skus = station.get("skus", [])
        station_sku_map[station_id] = station_skus

    # Get today's date stations from dateSkuMap
    date_sku_map = recipes_map.get("dateSkuMap", [])
    if not date_sku_map:
        return {}

    today_entry = date_sku_map[0]  # First (and usually only) date entry
    stations_today = today_entry.get("stations", [])

    result = {}
    for station in stations_today:
        station_id = station.get("id")
        skus_obj = station.get("skus", {})

        # Collect all SKUs for this station (simple + configurable base SKUs)
        simple_skus = skus_obj.get("simple", [])
        configurable_items = skus_obj.get("configurable", [])
        configurable_skus = [c.get("sku") for c in configurable_items if c.get("sku")]

        all_skus = simple_skus + configurable_skus

        items = []
        seen_names = set()  # Deduplicate by name

        for sku in all_skus:
            product = sku_to_product.get(sku)
            if not product:
                continue

            name = product.get("name", "Unknown")
            if name in seen_names:
                continue
            seen_names.add(name)

            attributes = product.get("attributes", [])

            # Skip ingredient/MTO items (toppings, condiments that are add-ons)
            is_ingredient = get_attr(attributes, "ingredient_item")
            is_minimized = get_attr(attributes, "minimized")
            if is_ingredient == "yes" or is_minimized == "yes":
                continue

            calories = get_attr(attributes, "calories")
            description = get_attr(attributes, "marketing_description")
            allergens = get_attr(attributes, "allergen_statement")
            ingredients = get_attr(attributes, "recipe_ingredients")
            serving = get_attr(attributes, "serving_combined")

            # Round calories
            try:
                calories_rounded = round(float(calories)) if calories else None
            except (ValueError, TypeError):
                calories_rounded = None

            items.append({
                "name": name,
                "calories": calories_rounded,
                "description": description,
                "allergens": allergens,
                "serving": serving,
                "ingredients": ingredients,
            })

        if items:
            result[station_id] = items

    return result


def format_summary(parsed_menu, location_name, meal_name, target_date):
    lines = []
    lines.append(f"**{'='*40}**")
    lines.append(f"**{location_name} — {meal_name.title()}**")
    lines.append(f"*{target_date}*")
    lines.append(f"**{'='*40}**")

    if not parsed_menu:
        lines.append("No menu data available.")
        return "\n".join(lines)

    for station_id, items in parsed_menu.items():
        station_label = STATION_NAMES.get(station_id, f"Station {station_id}")
        lines.append(f"\n**{station_label}**")
        for item in items:
            cal_str = f"  {item['calories']} cal" if item['calories'] else ""
            lines.append(f"**• {item['name']}**{cal_str}")
            if item.get("description"):
                lines.append(f"  {item['description']}")
            if item.get("allergens") and item["allergens"] not in ("Contains: ", ""):
                lines.append(f"  ⚠ {item['allergens']}")

    lines.append("")
    return "\n".join(lines)

def save_to_csv(parsed_menu, location_name, meal_name, target_date):
    """Append today's menu to a CSV history file for pattern analysis."""
    import csv
    from pathlib import Path

    filepath = Path("menu_history.csv")
    file_exists = filepath.exists()
    day_of_week = datetime.strptime(target_date, "%Y-%m-%d").strftime("%A")

    with open(filepath, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow([
                "date", "day_of_week", "location", "meal",
                "station_id", "item_name", "calories", "description", "allergens"
            ])

        for station_id, items in parsed_menu.items():
            for item in items:
                writer.writerow([
                    target_date,
                    day_of_week,
                    location_name,
                    meal_name,
                    station_id,
                    item["name"],
                    item.get("calories", ""),
                    item.get("description", ""),
                    item.get("allergens", ""),
                ])


def send_discord(summary, webhook_url):
    """Send summary to a Discord webhook."""
    # Discord has a 2000 char limit per message — split if needed
    chunks = [summary[i:i+1900] for i in range(0, len(summary), 1900)]
    for chunk in chunks:
        payload = {"content": chunk}
        requests.post(webhook_url, json=payload, timeout=10)


def main():
    target_date = date.today().isoformat()
    full_summary = f"🍽️  UCF Dining — {datetime.today().strftime('%A, %B %d, %Y')}\n\n"

    for meal_name, meal_period in MEAL_PERIODS.items():
        for location_name, location_key in LOCATIONS.items():
            print(f"Fetching {location_name} {meal_name}...")
            try:
                raw = fetch_menu(location_key, meal_period, target_date)
                parsed = parse_menu(raw)
                summary = format_summary(parsed, location_name, meal_name, target_date)
                full_summary += summary + "\n"
                save_to_csv(parsed, location_name, meal_name, target_date)
                print(f"  ✓ {sum(len(v) for v in parsed.values())} items found")
            except Exception as e:
                full_summary += f"\n  ❌ Error fetching {location_name}: {e}\n"
                print(f"  ✗ Error: {e}")

    print("\n" + full_summary)

    discord_webhook = os.environ.get("DISCORD_WEBHOOK")
    if discord_webhook:
        send_discord(full_summary, discord_webhook)

    return full_summary

if __name__ == "__main__":
    main()