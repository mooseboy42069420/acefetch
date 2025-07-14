"""Build M3U file from AceStream API."""

import argparse
import csv
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

import requests
from fuzzywuzzy import (  # type: ignore[import-untyped] # This has been replaced by thefuzz, but I can't get it working as nice
    fuzz,
    process,
)

REQUESTS_TIMEOUT = 10
API_URL = "https://api.acestream.me/all?api_version=1&api_key=test_api_key"
LOGOS_PATH = Path("channel_logos.xml")

FIND_COUNTRY_CODE_REGEX = re.compile(r"\s*\[\w{2}\]\s*$")

TVG_ID_COUNTRY_CODE_REGEX_1 = re.compile(r"\.(\w{2})\s*$")  # Matches .uk
TVG_ID_COUNTRY_CODE_REGEX_2 = re.compile(r"^(\w{2})[ :]")  # Matches "UK " or "UK: "

TVG_LOGO_REGEX = re.compile(r'tvg-logo="([^"]+)"')
TVG_ID_REGEX = re.compile(r'tvg-id="([^"]+)"')
LAST_FOUND_REGEX = re.compile(r'x-last-found="(\d+)"')

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

M3U_URI_SCHEMES = {
    "local_infohash": "http://127.0.0.1:6878/ace/manifest.m3u8?infohash=",
    "local_content_id": "http://127.0.0.1:6878/ace/manifest.m3u8?content_id=",
    "ace": "acestream://",
    "horus": "plugin://script.module.horus?action=play&id=",
}


SPORT_WORDS = {
    "football",
    "soccer",
    "basketball",
    "nba",
    "sport",
    "tennis",
    "espn",
    "moto",
    "formula 1",
    "f1",
    "hockey",
    "cricket",
    "rugby",
    "golf",
    "fórmula 1",
}

CURRENT_TIME = datetime.now(tz=UTC)
STALE_CHANNEL_TIME_THRESHOLD = timedelta(days=3)


# region Classes
@dataclass
class Channel:
    """Object representing a channel."""

    name: str
    tvg_logo: str
    last_not_found: int
    tvg_id: str = ""
    infohash: str = ""
    content_id: str = ""
    category: str = ""


# Region PreviousChannelProcessor
class PreviousChannelProcessor:
    """Class to handle previously processed channels."""

    def __init__(self, playlist_name: str) -> None:
        """Initialize the processor with a playlist name."""
        self.previous_channels: list[Channel] = []
        for uri_scheme in M3U_URI_SCHEMES:
            file_path = Path("playlists") / f"{playlist_name}_{uri_scheme}.m3u"
            self.load_from_file(file_path)

        print(f"Loaded {len(self.previous_channels)} previous channels from files.")

    def load_from_file(self, file_path: Path) -> None:
        """Load previously processed channels from a file."""
        if not file_path.exists():
            print("Warning: Previous channels file does not exist:", file_path)
            return

        with file_path.open("r", encoding="utf-8") as file:
            line_one = ""
            for line in file:
                line_normalised = line.replace("#EXTINF:-1,", "#EXTINF:-1").strip()

                # First line of an entry
                if line.startswith("#EXTINF:"):
                    line_one = line_normalised
                    continue

                # Second line of an entry
                if not line.startswith("#EXTINF:") and line_one:
                    content_id = extract_content_id_from_url(line)
                    infohash = extract_infohash_from_url(line)
                    tvg_logo = TVG_LOGO_REGEX.search(line_one)
                    tvg_id = TVG_ID_REGEX.search(line_one)

                    last_found = LAST_FOUND_REGEX.search(line_one)

                    self.previous_channels.append(
                        Channel(
                            name=line_one.split(",")[-1].strip(),
                            tvg_logo=tvg_logo.group(1) if tvg_logo else "",
                            tvg_id=tvg_id.group(1) if tvg_id else "",
                            infohash=infohash,
                            content_id=content_id,
                            last_not_found=int(last_found.group(1)) if last_found else 0,
                        )
                    )
                    line_one = ""

    def get_recent_missing_channels(self, current_channels: list[Channel]) -> list[Channel]:
        """Get channels that were in the previous list but not in the current one."""
        skipped_old_channels = 0
        missing_channels: list[Channel] = []

        for previous_channel in self.previous_channels:
            found_content_id = any(
                previous_channel.content_id == current_channel.content_id
                for current_channel in current_channels
                if current_channel.content_id
            )
            found_infohash = any(
                previous_channel.infohash == current_channel.infohash
                for current_channel in current_channels
                if current_channel.infohash
            )
            if not found_infohash and not found_content_id:
                # If the channel is not found in the current list, add it to missing channels
                msg = f"Channel '{previous_channel.name}' is missing in the current list."
                last_added = datetime.fromtimestamp(previous_channel.last_not_found, tz=UTC)
                if CURRENT_TIME - last_added > STALE_CHANNEL_TIME_THRESHOLD:
                    msg += " It has been missing for a while, not adding it."
                    skipped_old_channels += 1
                else:
                    msg += " Adding it to the missing channels list, it's not too old."
                    previous_channel.last_not_found = int(CURRENT_TIME.timestamp())
                    missing_channels.append(previous_channel)

                print(msg)

        print(f"Added {len(missing_channels)} missing channels, ignored {skipped_old_channels} from previous scrape.")
        return missing_channels


# region get_logos
def get_logos() -> dict[str, str]:
    """Load logos from the XML file to a dictionary channel_name,url."""
    print_heading("Loading Logos")
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

    print(f"Loaded {len(logos)} logos from {LOGOS_PATH}")
    return logos


# region Names
def find_best_logo_match(name: str, logos: dict[str, str]) -> str:
    """Find the best matching logo for a given channel name."""
    # Remove any country code from the name, indicated by a two-letter suffix between square brackets
    name = FIND_COUNTRY_CODE_REGEX.sub("", name.strip())

    for logo_name, url in logos.items():
        if name.lower() == logo_name.lower():
            return url

    # Use fuzzy matching to find the best match
    match = process.extractOne(name, logos.keys(), scorer=fuzz.token_sort_ratio, score_cutoff=80)

    if not match:
        match = process.extractOne(name, logos.keys(), scorer=fuzz.partial_ratio, score_cutoff=75)
    if match:
        print(f"Found fuzzy match for '{name}': {match[0]} with score {match[1]}")
        return logos[match[0]]  # This is the URL
    return ""


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


# region File Wrangling
def get_filter_list(filename: Path) -> list[str]:
    """Get a list of filters from a file."""
    print_heading("Loading Filters")
    if not filename.exists():
        print(f"Filter file {filename} does not exist!")
        return []

    with filename.open("r", encoding="utf-8") as file:
        reader = csv.reader(file)
        filters = [row[0].strip() for row in reader if row]

    print(f"Loaded {len(filters)} filters from {filename}")
    return filters


def get_name_replacements(replacements_csv: Path) -> dict[str, str]:
    """Get name replacements from a CSV file."""
    csv_columns = 2
    print_heading("Loading Name Replacements")
    if not replacements_csv.exists():
        print(f"Replacements file {replacements_csv} does not exist!")
        return {}

    with replacements_csv.open("r", encoding="utf-8") as csvfile:
        reader = csv.reader(csvfile)
        replacements = {row[0]: row[1] for row in reader if len(row) == csv_columns}

    print(f"Loaded {len(replacements)} name replacements from {replacements_csv}")
    return replacements


# region TVG Handling
def get_country_code_from_tvg_id(tvg_id: str) -> str:
    """Extract country code from the tvg_id."""
    matches = TVG_ID_COUNTRY_CODE_REGEX_1.findall(tvg_id)
    if matches:
        return f"[{matches[0].upper()}]"

    matches = TVG_ID_COUNTRY_CODE_REGEX_2.findall(tvg_id)
    if matches:
        return f"[{matches[0].upper()}]"

    return "[?]"


def get_tvg_id_from_title(title: str) -> str:
    """Extract the TVG ID from the title."""
    country_code_regex = FIND_COUNTRY_CODE_REGEX.search(title)
    if not country_code_regex:
        return ""

    try:
        country_code_regex.group(0)
    except IndexError:
        print(f"Very strange title: {title}")
        return ""

    if isinstance(country_code_regex.group(0), str):
        country_code = country_code_regex.group(0).replace("[", "").replace("]", "").strip()
        title_no_cc = title.replace(f"[{country_code}]", "").strip()

        return f"{title_no_cc}.{country_code.lower()}"
    return ""


def is_sport_channel(channel_name: str) -> bool:
    """Check if a channel is a sport channel based on its name."""
    # Normalize the channel name to lowercase for case-insensitive comparison
    normalized_name = channel_name.lower()
    # Check if any of the sport words are in the channel name
    return any(sport_word.lower() in normalized_name for sport_word in SPORT_WORDS)


# region Playlist
def create_playlists(playlist_name: str, list_of_channels: list[Channel]) -> None:
    """Create M3U playlist file."""
    print_heading("Creating Playlists")
    output_directory = Path("playlists")
    output_directory.mkdir(exist_ok=True)

    for uri_scheme, prefix in M3U_URI_SCHEMES.items():
        playlist_path = output_directory / f"{playlist_name}_{uri_scheme}.m3u"
        with playlist_path.open("w", encoding="utf-8") as m3u_file:
            m3u_file.write("#EXTM3U\n")
            for channel in list_of_channels:
                top_line = f'#EXTINF:-1 tvg-logo="{channel.tvg_logo}" tvg-id="{channel.tvg_id}" group-title="{channel.category}" x-last-found="{channel.last_not_found}", {channel.name}\n'  # noqa: E501 This line can be long
                if channel.infohash != "" and uri_scheme == "local_infohash":
                    m3u_file.write(top_line)
                    m3u_file.write(f"{prefix}{channel.infohash}\n")
                elif channel.content_id != "" and uri_scheme != "local_infohash":
                    m3u_file.write(top_line)
                    m3u_file.write(f"{prefix}{channel.content_id}\n")

        print(f"Created playlist {playlist_path} with {len(list_of_channels)} channels.")


# region URL Handling
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


# region Download/API
def populate_list_from_m3u(url: str) -> list[Channel]:
    """Populate a list of channels from an M3U file."""
    csv_columns = 2
    print_heading(f"Scraping M3U from {url}")
    response = requests.get(url, timeout=REQUESTS_TIMEOUT)
    response.raise_for_status()
    m3u_content = response.text

    channels = []
    lines = m3u_content.splitlines()
    for i in range(len(lines)):
        if lines[i].startswith("#EXTINF:"):
            # Extract channel name and logo
            extinf_parts = lines[i][len("#EXTINF:") :].split(",")
            if len(extinf_parts) < csv_columns:
                continue  # Skip malformed lines
            channel_info = extinf_parts[0].strip()
            channel_name = extinf_parts[1].strip()

            # Extract logo URL if available
            logo_match = TVG_LOGO_REGEX.search(channel_info)
            logo_url = logo_match.group(1) if logo_match else ""

            # Extract tvg_id if available
            tvg_id_match = TVG_ID_REGEX.search(channel_info)
            tvg_id = tvg_id_match.group(1) if tvg_id_match else ""

            # Extract infohash from the next line
            if i + 1 < len(lines):
                url_line = lines[i + 1].strip()
                infohash = extract_infohash_from_url(url_line)
                content_id = extract_content_id_from_url(url_line)

                channels.append(
                    Channel(
                        name=channel_name,
                        tvg_logo=logo_url,
                        tvg_id=tvg_id,
                        infohash=infohash,
                        content_id=content_id,
                        last_not_found=0,
                    )
                )

    print(f"Found {len(channels)} channels in M3U file.")
    return channels


def populate_list_from_api(api_url: str) -> list[Channel]:
    """Populate a list of channels from the AceStream API."""
    print_heading(f"Scraping API from {api_url}")
    response = requests.get(API_URL, timeout=REQUESTS_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        msg = "Unexpected data format received from API, got a list"
        print(msg)
        raise TypeError(msg)

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
                last_not_found=0,
            )
        )

    print(f"Found {len(channel_list)} channels in API response.")
    return channel_list


# region Helpers


def print_heading(heading: str) -> None:
    """Print a heading with a separator."""
    print(f"\n{'=' * 10} {heading} {'=' * 10}")


def deduplicate_channels(channel_list: list[Channel]) -> list[Channel]:
    """Remove duplicate channels based on infohash or content_id."""
    seen_infohashes = set()
    seen_content_ids = set()

    found_channels: list[Channel] = []

    for channel in channel_list:
        if channel.infohash and channel.infohash in seen_infohashes:
            continue
        if channel.content_id and channel.content_id in seen_content_ids:
            continue

        if channel.infohash:
            seen_infohashes.add(channel.infohash)
        if channel.content_id:
            seen_content_ids.add(channel.content_id)

        found_channels.append(channel)

    return found_channels


# region Main
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

    print_heading("AceStream M3U Scraper")
    print(f"Playlist name: {args.playlist_name}")

    # Logos
    logos = get_logos()

    # Filter list
    filter_list = []
    if args.filter_file:
        filter_list = get_filter_list(Path(args.filter_file))

    # Populate
    channel_list_scratch: list[Channel] = []
    if args.m3u_url:
        channel_list_scratch.extend(populate_list_from_m3u(args.m3u_url))

    if args.api_url:
        channel_list_scratch.extend(populate_list_from_api(args.api_url))

    channel_list: list[Channel] = []

    # Name replacements
    name_replacements = get_name_replacements(Path(args.name_replacements))

    print_heading("Processing Channels")
    for channel in channel_list_scratch:
        # Replace channel names if replacements are provided

        if not FIND_COUNTRY_CODE_REGEX.search(channel.name):
            channel.name = f"{channel.name} {get_country_code_from_tvg_id(channel.tvg_id)}"

        if name_replacements:
            channel.name = do_name_replace(channel.name, name_replacements)

        # Continue only if we passed the filter
        if filter_list and not any(filter_str in channel.name for filter_str in filter_list):
            continue

        channel.tvg_logo = find_best_logo_match(channel.name, logos)

        if not channel.tvg_id:
            channel.tvg_id = get_tvg_id_from_title(channel.name)

        if is_sport_channel(channel.name):
            channel.category = "Sports"

        channel_list.append(channel)

    # Grab old channels that were not found in the current scrape
    print_heading("Checking for Old Channels")
    old_channels = PreviousChannelProcessor(args.playlist_name).get_recent_missing_channels(channel_list)
    channel_list.extend(old_channels)

    print_heading("Post-Processing Channels")
    channel_list = deduplicate_channels(channel_list)
    channel_list.sort(key=lambda x: x.name.lower())
    print("Deduplicated, sorted channels by name.")

    # Create the M3U playlist

    create_playlists(args.playlist_name, channel_list)


if __name__ == "__main__":
    main()
