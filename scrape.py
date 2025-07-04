"""Build M3U file from AceStream API."""

import re
import csv
import argparse
from dataclasses import dataclass
from pathlib import Path
from fuzzywuzzy import fuzz, process
from xml.etree import ElementTree as ET
import requests


REQUESTS_TIMEOUT = 10
API_URL = "https://api.acestream.me/all?api_version=1&api_key=test_api_key"
LOGOS_PATH = Path("channel_logos.xml")

REMOVE_COUNTRY_CODE_REGEX = re.compile(r"\s*\[\w{2}\]\s*$")

ACE_URL_PREFIXES_CONTENT_ID = [
    "acestream://",
    "http://127.0.0.1:6878/ace/getstream?id=",
    "http://127.0.0.1:6878/ace/getstream?content_id=",
    "http://127.0.0.1:6878/ace/manifest.m3u8?id=",
    "http://127.0.0.1:6878/ace/manifest.m3u8?content_id=",  # Side note, this is the good one when using ace
    "plugin://script.module.horus?action=play&id=",  # Horus Kodi plugin
]
ACE_URL_PREFIXES_INFOHASH = [
    "http://127.0.0.1:6878/ace/getstream?infohash=",
    "http://127.0.0.1:6878/ace/manifest.m3u8?infohash=",
]


@dataclass
class Channel:
    """Object representing a channel."""

    name: str
    tvg_logo: str
    infohash: str = ""
    content_id: str = ""
    category: str = ""


def get_logos() -> dict:
    with LOGOS_PATH.open("r", encoding="utf-8") as file:
        logos_xml = file.read()

    if not logos_xml:
        return {}

    root = ET.fromstring(logos_xml)
    logos = {}

    # Parse the XML structure: regions contain channels with name attributes
    for region in root.findall("region"):
        for channel in region.findall("channel"):
            channel_name = channel.get("name")
            if channel_name:
                for child in channel:
                    if child.tag == "logo_url":
                        logo_url = child.text.strip() if child.text else ""
                        break

                logos[channel_name] = logo_url

    return logos


def find_best_match(name, logos) -> str:
    """Find the best matching logo for a given channel name."""
    # Remove any country code from the name, indicated by a two-letter suffix between square brackets
    name = REMOVE_COUNTRY_CODE_REGEX.sub("", name.strip())

    # Use fuzzy matching to find the best match
    match = process.extractOne(
        name, logos.keys(), scorer=fuzz.token_sort_ratio, score_cutoff=80
    )
    if not match:
        match = process.extractOne(
            name, logos.keys(), scorer=fuzz.partial_ratio, score_cutoff=75
        )
    if match:
        return logos[match[0]]
    return ""


def get_name_replacements(replacements_csv: Path) -> dict[str, str]:
    """Get name replacements from a CSV file."""
    if not replacements_csv.exists():
        print(f"Replacements file {replacements_csv} does not exist!")
        return {}

    with replacements_csv.open("r", encoding="utf-8") as csvfile:
        reader = csv.reader(csvfile)
        replacements = {row[0]: row[1] for row in reader if len(row) == 2}

    return replacements


def do_name_replace(name: str, replacements: dict[str, str]) -> str:
    """Replace names based on a CSV file."""
    name = name.strip()

    new_name = name
    for old, new in replacements.items():
        if old in new_name:
            new_name = new_name.replace(old, new)

    if new_name != name:
        print(f"Replaced '{name}' with '{new_name}'")

    return new_name.strip()


def get_filter_list(filename: Path) -> list[str]:
    """Get a list of filters from a file."""
    if not filename.exists():
        print(f"Filter file {filename} does not exist!")
        return []

    with filename.open("r", encoding="utf-8") as file:
        reader = csv.reader(file)
        filters = [row[0].strip() for row in reader if row]

    return filters


def create_playlists(playlist_name: str, list_of_channels: list[Channel]) -> None:
    """Create M3U playlist file."""
    output_directory = Path("playlists")
    output_directory.mkdir(exist_ok=True)

    uri_schemes = {
        "local": "",
        "ace": "acestream://",
        "horus": "plugin://script.module.horus?action=play&id=",
    }

    local_prefixes = {
        "infohash": "http://127.0.0.1:6878/ace/manifest.m3u8?infohash=",
        "content_id": "http://127.0.0.1:6878/ace/manifest.m3u8?content_id=",
    }

    for uri_scheme, prefix in uri_schemes.items():
        playlist_path = output_directory / f"{playlist_name}_{uri_scheme}.m3u"
        with playlist_path.open("w", encoding="utf-8") as m3u_file:
            m3u_file.write("#EXTM3U\n")
            for channel in list_of_channels:
                if channel.infohash and uri_scheme == "local":
                    m3u_file.write(
                        f'#EXTINF:-1 tvg-logo="{channel.tvg_logo}" group-title="{channel.category}", {channel.name}\n'
                    )
                    m3u_file.write(f"{local_prefixes['infohash']}{channel.infohash}\n")
                elif channel.content_id:
                    prefix = uri_schemes.get(uri_scheme, "")
                    if uri_scheme == "local":
                        prefix = local_prefixes["content_id"]

                    m3u_file.write(
                        f'#EXTINF:-1 tvg-logo="{channel.tvg_logo}" group-title="{channel.category}", {channel.name}\n'
                    )
                    m3u_file.write(f"{prefix}{channel.content_id}\n")


def extract_infohash_from_url(url: str) -> str:
    """Extract infohash from a URL."""
    for prefix in ACE_URL_PREFIXES_INFOHASH:
        if url.startswith(prefix):
            return url[len(prefix) :].strip()
    return ""


def extract_content_id_from_url(url: str) -> str:
    """Extract content ID from a URL."""
    for prefix in ACE_URL_PREFIXES_CONTENT_ID:
        if url.startswith(prefix):
            return url[len(prefix) :].strip()
    return ""


def populate_list_from_m3u(url: str) -> list[Channel]:
    """Populate a list of channels from an M3U file."""
    response = requests.get(url, timeout=REQUESTS_TIMEOUT)
    response.raise_for_status()
    m3u_content = response.text

    channels = []
    lines = m3u_content.splitlines()
    for i in range(len(lines)):
        if lines[i].startswith("#EXTINF:"):
            # Extract channel name and logo
            extinf_parts = lines[i][len("#EXTINF:") :].split(",")
            if len(extinf_parts) < 2:
                continue  # Skip malformed lines
            channel_info = extinf_parts[0].strip()
            channel_name = extinf_parts[1].strip()

            # Extract logo URL if available
            logo_match = re.search(r'tvg-logo="([^"]+)"', channel_info)
            logo_url = logo_match.group(1) if logo_match else ""

            # Extract infohash from the next line
            if i + 1 < len(lines):
                url_line = lines[i + 1].strip()
                infohash = extract_infohash_from_url(url_line)
                content_id = extract_content_id_from_url(url_line)

                channels.append(
                    Channel(
                        name=channel_name,
                        tvg_logo=logo_url,
                        infohash=infohash,
                        content_id=content_id,
                    )
                )

    return channels


def populate_list_from_api() -> list[Channel]:
    """Populate a list of channels from the AceStream API."""
    response = requests.get(API_URL, timeout=REQUESTS_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        print("Unexpected data format received from API.")
        raise ValueError("Data is not a list.")

    channel_list: list[Channel] = []

    for item in data:
        name = item.get("name", "Unknown")

        categories = item.get("categories", [])
        category = "" if not categories else categories[0]

        channel_list.append(
            Channel(
                name=name,
                tvg_logo="",
                category=category,
                infohash=item.get("infohash", ""),
            )
        )

    return channel_list


def main() -> None:
    """Scrape."""
    parser = argparse.ArgumentParser(description="Scrape AceStream API for M3U file.")
    parser.add_argument(
        "--playlist-name",
        type=str,
        default="default",
        help="Playlist name to be created, minus the .m3u extension.",
    )
    parser.add_argument(
        "--filter-file",
        type=str,
        default="",
        help="Specify a filter file to include only certain channels.",
    )
    parser.add_argument(
        "--name-replacements",
        type=str,
        default="channel_name_replacements.csv",
        help="Path to CSV file for name replacements",
    )
    parser.add_argument(
        "--m3u-url",
        type=str,
        default="",
        help="URL to the M3U file to scrape channels from.",
    )
    parser.add_argument(
        "--api-url",
        type=str,
        default=API_URL,
        help="URL to the AceStream API to scrape channels from.",
    )
    args = parser.parse_args()

    logos = get_logos()

    name_replacements = get_name_replacements(Path(args.name_replacements))

    filter_list = []
    if args.filter_file:
        filter_list = get_filter_list(Path(args.filter_file))

    channel_list_scratch: list[Channel] = []

    if args.m3u_url:
        channel_list_scratch.extend(populate_list_from_m3u(args.m3u_url))

    if args.api_url:
        channel_list_scratch.extend(populate_list_from_api())

    channel_list = []

    for channel in channel_list_scratch:
        # Replace channel names if replacements are provided
        if name_replacements:
            channel.name = do_name_replace(channel.name, name_replacements)

        # Continue only if we passed the filter
        if filter_list and not any(filter in channel.name for filter in filter_list):
            continue

        channel.tvg_logo = find_best_match(channel.name, logos)

        channel_list.append(channel)

    # Sort the channels by name
    channel_list.sort(key=lambda x: x.name.lower())

    # Create the M3U playlist
    create_playlists(args.playlist_name, channel_list)


if __name__ == "__main__":
    main()
