from __future__ import annotations

import logging
import sys

import requests

from matterlights.config import load_settings


LOGGER = logging.getLogger("matterlights.discover")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = load_settings(require_light_entities=False)

    try:
        response = requests.get(
            f"{settings.ha_url}/api/states",
            headers={
                "Authorization": f"Bearer {settings.ha_token}",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        response.raise_for_status()
    except requests.HTTPError as error:
        status_code = error.response.status_code if error.response is not None else None
        if status_code in {401, 403}:
            LOGGER.error("Home Assistant rejected the token. Create a new long-lived access token at %s/profile and try again.", settings.ha_url)
            return 1
        LOGGER.error("Home Assistant returned HTTP %s while listing lights.", status_code)
        return 1
    except requests.RequestException as error:
        LOGGER.error("Could not reach Home Assistant at %s: %s", settings.ha_url, error)
        return 1

    states = response.json()
    lights = [state for state in states if state.get("entity_id", "").startswith("light.")]
    if not lights:
        LOGGER.error("No Home Assistant light entities were found. Add your bulbs to Home Assistant first, then run discovery again.")
        return 2

    for light in sorted(lights, key=lambda item: item["entity_id"]):
        friendly_name = light.get("attributes", {}).get("friendly_name", "")
        if friendly_name:
            LOGGER.info("%s\t%s", light["entity_id"], friendly_name)
        else:
            LOGGER.info("%s", light["entity_id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())