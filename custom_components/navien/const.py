"""Constants for Navien Smart."""

DOMAIN = "navien"
STORAGE_KEY = f"{DOMAIN}_state"
STORAGE_VERSION = 1

API_BASE_URL = "https://nskr.naviensmartcontrol.com"
MEMBER_BASE_URL = "https://member.naviensmartcontrol.com"

SERVICE_AIRONE = 300
SERVICE_MATE = 200
SUPPORTED_SERVICE_CODES = {SERVICE_AIRONE, SERVICE_MATE}

TOPIC_PREFIX = {
    SERVICE_AIRONE: "airone",
    SERVICE_MATE: "mate",
}
