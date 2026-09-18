"""
Script to download media recordings from Vobiz API.
Requires X-Auth-ID and X-Auth-Token headers for authentication.
Credentials are loaded from .env file.
"""

import os
import requests
from pathlib import Path
from dotenv import load_dotenv
from urllib.parse import urlparse

# Load environment variables
load_dotenv()

# Get auth credentials from .env
AUTH_ID = os.getenv("VOBIZ_AUTH_ID")
AUTH_TOKEN = os.getenv("VOBIZ_AUTH_TOKEN")

# Base output directory
OUTPUT_DIR = Path(__file__).parent / "manual_record"


def download_recording(url: str, filename: str = None) -> str:
    """
    Download a recording from Vobiz media server.
    
    Args:
        url: The full URL of the recording to download
        filename: Optional custom filename. If not provided, extracts from URL.
    
    Returns:
        Path to the downloaded file
    """
    if not AUTH_ID or not AUTH_TOKEN:
        raise ValueError("VOBIZ_AUTH_ID and VOBIZ_AUTH_TOKEN must be set in .env file")
    
    # Create output directory if it doesn't exist
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    # Extract filename from URL if not provided
    if not filename:
        parsed_url = urlparse(url)
        filename = Path(parsed_url.path).name
    
    # Keep the extension Vobiz actually served. The <Record> element
    # chooses the format (fileFormat="wav" by default here), so forcing
    # .mp3 produced files that were RIFF/WAVE bytes with an .mp3 name.
    if not Path(filename).suffix:
        filename += '.wav'
    
    output_path = OUTPUT_DIR / filename
    
    # Set up headers with authentication
    headers = {
        "X-Auth-ID": AUTH_ID,
        "X-Auth-Token": AUTH_TOKEN
    }
    
    print(f"Downloading: {url}")
    print(f"Output: {output_path}")
    
    # Make the request
    response = requests.get(url, headers=headers, stream=True)
    
    if response.status_code == 200:
        with open(output_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        print(f"✓ Downloaded successfully: {output_path}")
        return str(output_path)
    else:
        print(f"✗ Download failed with status {response.status_code}")
        print(f"Response: {response.text}")
        raise Exception(f"Download failed: {response.status_code} - {response.text}")


if __name__ == "__main__":
    # Example URL from user
    url = "https://media.vobiz.ai/v1/Account/auth id /Recording/call uuid.mp3"
    
    download_recording(url)
