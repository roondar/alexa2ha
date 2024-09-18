import requests
import pickle
from dotenv import load_dotenv
from http.cookies import SimpleCookie
from http import cookies
from collections import defaultdict
import os
import logging
import time
import sys


# Load environment variables from .env file
load_dotenv()

# Setup logging
log_level = os.getenv('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    level=getattr(logging, log_level)
)
logger = logging.getLogger(__name__)

# Define headers for requests
DEFAULT_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 13_5_1 like Mac OS X)"
                   " AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
                   " PitanguiBridge/2.2.345247.0-[HARDWARE=iPhone10_4][SOFTWARE=13.5.1]"),
    "Accept": "*/*",
    "Accept-Language": "*",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1"
}

logger.debug("sys.version_info: %s", sys.version_info[:2])
if sys.version_info[:2] < (3, 13):
    # See: https://github.com/python/cpython/issues/112713
    # 24-09-09 This patch may need to revisited as Python/Home Assistant evolve
    cookies.Morsel._reserved["partitioned"] = "Partitioned"  # type: ignore[attr-defined]
    cookies.Morsel._flags.add("partitioned")  # type: ignore[attr-defined]
    logger.debug("cookies.Morsel was patched")
else:
    logger.debug("cookies.Morsel was not patched")

def add_item_to_shopping_list(webhook_url, item_name):
    try:
        payload = {"name": item_name}
        headers = {
            'Accept': 'application/json',
            'Content-Type': 'application/json'
        }
        response = requests.post(webhook_url, headers=headers, json=payload)
        response.raise_for_status()
        logger.info(f'Successfully added item: {item_name}')
    except requests.RequestException as err:
        logger.error(f'Error adding item: {item_name} - {err}')
        return False
    return True


def initialize_environment_variables():
    webhook_url = os.getenv('HA_WEBHOOK_URL')
    cookie_path = os.getenv('COOKIE_PATH')
    amazon_api_url = os.getenv('AMAZON_URL')

    # Check for missing environment variables
    if not webhook_url or not cookie_path or not amazon_api_url:
        missing_vars = [var for var, value in {
            "HA_WEBHOOK_URL": webhook_url,
            "COOKIE_PATH": cookie_path,
            "AMAZON_URL": amazon_api_url
        }.items() if not value]
        logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
        raise EnvironmentError(f"Missing required environment variables: {', '.join(missing_vars)}")

    return {
        'webhook_url': webhook_url,
        'cookie_path': cookie_path,
        'amazon_api_url': amazon_api_url
    }


def load_cookies_from_file(cookie_file_path):
    try:
        with open(cookie_file_path, 'rb') as cookie_file:
            cookies = pickle.load(cookie_file)
        if isinstance(cookies, defaultdict) and all(
                isinstance(v, SimpleCookie) for v in cookies.values()):
            # Convert defaultdict of SimpleCookie to a simple dictionary
            cookie_dict = {}
            for domain, simple_cookie in cookies.items():
                for key, morsel in simple_cookie.items():
                    cookie_dict[key] = morsel.value
            return cookie_dict
        return cookies
    except Exception as err:
        logger.error(f"Failed to load cookies from {cookie_file_path}: {err}")
        return None


def make_authenticated_request(url, cookie_file_path, method='GET', payload=None):
    # Create a session
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)

    # Load cookies from file and update session cookies
    cookies = load_cookies_from_file(cookie_file_path)
    if not cookies:
        raise ValueError("No cookies loaded")
    session.cookies.update(cookies)

    # Make the HTTP request
    try:
        if method == 'GET':
            response = session.get(url)
        elif method == 'PUT':
            response = session.put(url, json=payload)
        response.raise_for_status()
    except requests.RequestException as err:
        logger.error(f"HTTP request failed: {err}")
        return None
    return response


def extract_list_items(response_data):
    # Extract the random key and access the desired content
    for key in response_data.keys():
        if isinstance(response_data[key], dict) and 'listItems' in response_data[key]:
            return response_data[key]['listItems']
    return None


def filter_incomplete_items(list_items):
    # Filter out items where `completed` is False
    return [list_item for list_item in list_items if not list_item.get('completed', False)]


def mark_item_as_completed(amazon_api_url, cookie_file_path, list_item):
    url = f"{amazon_api_url}/alexashoppinglists/api/updatelistitem"
    list_item['completed'] = True
    response = make_authenticated_request(url, cookie_file_path, method='PUT', payload=list_item)
    if response and response.status_code == 200:
        logger.info(f"Item marked as completed: {list_item.get('value', 'unknown')}")
    else:
        logger.error(f"Failed to update item: {list_item.get('value', 'unknown')}")

def main():
    try:
        # Initialize environment variables
        env_vars = initialize_environment_variables()
        webhook_url = env_vars.get('webhook_url')
        cookie_file_path = env_vars.get('cookie_path')
        amazon_api_url = env_vars.get('amazon_api_url')

        list_items_url = f"{amazon_api_url}/alexashoppinglists/api/getlistitems"

        # Retrieve items from list
        response = make_authenticated_request(list_items_url, cookie_file_path)
        if response and response.status_code == 200:
            logger.debug("Successfully retrieved data.")
            response_data = response.json()

            # Extract list items
            list_items = extract_list_items(response_data)
            if list_items:
                # Filter incomplete items
                incomplete_items = filter_incomplete_items(list_items)

                for incomplete_item in incomplete_items:
                    item_name = incomplete_item.get("value")
                    if add_item_to_shopping_list(webhook_url, item_name):
                        logger.info(f"Marking item as completed: {item_name}")
                        mark_item_as_completed(amazon_api_url, cookie_file_path, incomplete_item)
            else:
                logger.error("Unable to find 'listItems' in the response.")
        else:
            logger.error(f"Failed to retrieve data, status code: {response.status_code}")
            if response:
                logger.error(response.text)
    except EnvironmentError as env_err:
        logger.critical(env_err)
    except Exception as exc:
        logger.exception("Unhandled exception occurred")

if __name__ == "__main__":
    while True:
        main()
        time.sleep(10)  # Wait for 10 seconds before running again
