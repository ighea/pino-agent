import ast
import datetime
import operator
import os

import requests as _requests

from app.tools.manager import ToolManager

tool_manager = ToolManager()

BRAVE_API_KEY = os.getenv("BRAVE_API_KEY", "")
BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
BRAVE_IMAGE_SEARCH_URL = "https://api.search.brave.com/res/v1/images/search"

OWM_API_KEY = os.getenv("OPENWEATHERMAP_API_KEY", "")
OWM_URL = "https://api.openweathermap.org/data/2.5/weather"


def _search_web(query: str, count: int = 5) -> str:
    if not BRAVE_API_KEY:
        return "Error: BRAVE_API_KEY environment variable is not set."
    resp = _requests.get(
        BRAVE_SEARCH_URL,
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": BRAVE_API_KEY,
        },
        params={"q": query, "count": count},
        timeout=10,
    )
    if resp.status_code == 401:
        return "Error: Brave Search API key is invalid or unauthorised."
    if not resp.ok:
        return f"Error: Brave Search returned HTTP {resp.status_code}."
    results = resp.json().get("web", {}).get("results", [])
    if not results:
        return "No results found."
    lines = []
    for r in results:
        lines.append(f"- {r.get('title', '')}\n  {r.get('url', '')}\n  {r.get('description', '')}")
    return "\n\n".join(lines)


def _search_images(query: str, count: int = 5) -> str:
    if not BRAVE_API_KEY:
        return "Error: BRAVE_API_KEY environment variable is not set."
    resp = _requests.get(
        BRAVE_IMAGE_SEARCH_URL,
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": BRAVE_API_KEY,
        },
        params={"q": query, "count": count},
        timeout=10,
    )
    if resp.status_code == 401:
        return "Error: Brave Search API key is invalid or unauthorised."
    if not resp.ok:
        return f"Error: Brave Image Search returned HTTP {resp.status_code}."
    results = resp.json().get("results", [])
    if not results:
        return "No image results found."
    lines = []
    for r in results:
        title = r.get("title", "")
        image_url = r.get("properties", {}).get("url", "") or r.get("url", "")
        source = r.get("url", "") or r.get("page_url", "")
        lines.append(f"- {title}\n  Image URL: {image_url}\n  Source: {source}")
    return "\n\n".join(lines)


def _wind_cardinal(deg: float) -> str:
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return dirs[round(deg / 22.5) % 16]


def _get_weather(location: str, units: str = "metric") -> str:
    if not OWM_API_KEY:
        return "Error: OPENWEATHERMAP_API_KEY environment variable is not set."
    resp = _requests.get(
        OWM_URL,
        params={"q": location, "appid": OWM_API_KEY, "units": units},
        timeout=10,
    )
    if resp.status_code == 401:
        return (
            "Error: OpenWeatherMap API key is invalid or not yet active. "
            "New keys can take up to 2 hours to activate after registration."
        )
    if resp.status_code == 404:
        return f"Error: Location '{location}' not found."
    if not resp.ok:
        return f"Error: OpenWeatherMap returned HTTP {resp.status_code}."
    d = resp.json()
    unit_symbol = "°C" if units == "metric" else "°F"
    speed_unit = "m/s" if units == "metric" else "mph"

    main = d.get("main", {})
    wind = d.get("wind", {})
    visibility = d.get("visibility")  # metres, may be absent

    temp = round(main.get("temp", 0), 1)
    feels_like = round(main.get("feels_like", 0), 1)
    humidity = main.get("humidity", "?")
    pressure = main.get("pressure", "?")
    wind_speed = round(wind.get("speed", 0), 1)
    wind_info = f"{wind_speed} {speed_unit}"
    if "deg" in wind:
        wind_info += f" {_wind_cardinal(wind['deg'])}"

    parts = [
        f"{d['name']}, {d['sys']['country']}: {d['weather'][0]['description'].capitalize()}.",
        f"Temperature: {temp}{unit_symbol} (feels like {feels_like}{unit_symbol}).",
        f"Humidity: {humidity}%.  Pressure: {pressure} hPa.",
        f"Wind: {wind_info}.",
    ]
    if visibility is not None:
        parts.append(f"Visibility: {round(visibility / 1000, 1)} km.")

    return "  ".join(parts)


_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _eval_node(node: ast.AST) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval_node(node.operand))
    raise ValueError(f"Unsupported operation: {ast.dump(node)}")


def _get_datetime() -> str:
    from app.tz import TZ, TZ_NAME
    now = datetime.datetime.now(TZ)
    return now.strftime(f"%A, %d %B %Y %H:%M:%S {TZ_NAME}")


def _calculate(expression: str) -> str:
    try:
        tree = ast.parse(expression, mode="eval")
        result = _eval_node(tree.body)
        return str(result)
    except ZeroDivisionError:
        return "Error: Division by zero."
    except ValueError as e:
        return f"Error: {e}"
    except Exception:
        return "Error: Invalid expression."


tool_manager.register(
    name="get_datetime",
    fn=_get_datetime,
    description="Return the current local date, time, and timezone.",
    parameters={"type": "object", "properties": {}, "required": []},
    status_template="Checking current date and time...",
)

tool_manager.register(
    name="search_web",
    fn=_search_web,
    description=(
        "Search the web using Brave Search and return titles, URLs, and descriptions. "
        "Use this for current events, factual lookups, or any question that requires up-to-date information. "
        "Follow up with fetch_page on a specific result URL to read the full content of a page."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query."},
            "count": {
                "type": "integer",
                "description": "Number of results to return (default 5, max 20). Increase for broader coverage.",
            },
        },
        "required": ["query"],
    },
    status_template='Searching the web for: "{query}"',
)

tool_manager.register(
    name="search_images",
    fn=_search_images,
    description=(
        "Search for images using Brave Image Search. Returns image URLs, titles, and source pages. "
        "Use download_file to save an image to the workspace, then share_file to deliver it to the user."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The image search query."},
        },
        "required": ["query"],
    },
    status_template='Searching images for: "{query}"',
)

tool_manager.register(
    name="get_weather",
    fn=_get_weather,
    description=(
        "Get current weather conditions for a city or location. "
        "Returns temperature (°C by default), feels-like, humidity, pressure, wind speed and direction, "
        "and visibility. Use units='imperial' for °F and mph."
    ),
    parameters={
        "type": "object",
        "properties": {
            "location": {
                "type": "string",
                "description": "City name, optionally with country code, e.g. 'Helsinki' or 'Helsinki,FI'.",
            },
            "units": {
                "type": "string",
                "enum": ["metric", "imperial"],
                "description": "Temperature units: 'metric' (Celsius, m/s) or 'imperial' (Fahrenheit, mph). Default: metric.",
            },
        },
        "required": ["location"],
    },
    status_template="Fetching weather for: {location}",
)

tool_manager.register(
    name="calculate",
    fn=_calculate,
    description="Evaluate a mathematical expression and return the numeric result.",
    parameters={
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "A Python arithmetic expression, e.g. '12 * 7' or '(100 - 32) / 1.8'.",
            },
        },
        "required": ["expression"],
    },
    status_template="Calculating: {expression}",
)
