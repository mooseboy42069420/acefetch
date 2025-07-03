"""Build M3U file from AceStream API."""

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


@dataclass
class Channel:
    """Object representing a channel."""

    name: str
    infohash: str
    tvg_logo: str
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


def do_name_replace(name: str, replacements_csv: Path) -> str:
    """Replace names based on a CSV file."""
    name = name.strip()

    if not replacements_csv.exists():
        print(f"Replacements file {replacements_csv} does not exist!")
        return name

    with replacements_csv.open("r", encoding="utf-8") as csvfile:
        reader = csv.reader(csvfile)
        replacements = {row[0]: row[1] for row in reader if len(row) == 2}

    new_name = name
    for old, new in replacements.items():
        if old in new_name:
            new_name = new_name.replace(old, new)

    if new_name != name:
        print(f"Replaced '{name}' with '{new_name}'")

    return new_name


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
        "local": "http://127.0.0.1:6878/ace/manifest.m3u8?content_id=",
        "ace": "acestream://",
        "horus": "plugin://script.module.horus?action=play&id=",
    }

    for uri_scheme, prefix in uri_schemes.items():
        playlist_path = output_directory / f"{playlist_name}_{uri_scheme}.m3u"
        with playlist_path.open("w", encoding="utf-8") as m3u_file:
            m3u_file.write("#EXTM3U\n")
            for channel in list_of_channels:
                if channel.infohash:
                    m3u_file.write(
                        f'#EXTINF:-1 tvg-logo="{channel.tvg_logo}" group-title="{channel.category}",{channel.name}\n'
                    )
                    m3u_file.write(f"{prefix}{channel.infohash}\n")


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
    args = parser.parse_args()

    logos = get_logos()

    try:
        response = requests.get(API_URL, timeout=REQUESTS_TIMEOUT)
        response.raise_for_status()
        data = response.json()

        if isinstance(data, list):
            # Create the M3U content

            channel_list: list[Channel] = []

            if args.filter_file == "":
                name_filter = []
            else:
                name_filter = get_filter_list(Path(args.filter_file))

            for item in data:
                name = item.get("name", "Unknown")
                name = do_name_replace(name, Path(args.name_replacements))

                # If a filter is specified, check if the channel name matches
                if name_filter and not any(code in name for code in name_filter):
                    continue  # Skip this channel if it doesn't match the filter

                categories = item.get("categories", [])
                category = "" if not categories else categories[0]

                # Try to find a logo for this channel
                logo_url = item.get("tvg_logo", "")
                if not logo_url and logos:
                    logo_url = find_best_match(name, logos)

                channel_list.append(
                    Channel(
                        name=name,
                        tvg_logo=logo_url,
                        category=category,
                        infohash=item.get("infohash", ""),
                    )
                )

            # Sort the channels by name
            channel_list.sort(key=lambda x: x.name.lower())

            # Create the M3U playlist
            create_playlists(args.playlist_name, channel_list)

        else:
            print("Not a list?")

    except Exception as e:
        print(f"API Error: {e}")


if __name__ == "__main__":
    main()
