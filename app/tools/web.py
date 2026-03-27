import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime

from google.adk.auth.auth_credential import AuthCredential, OAuth2Auth
from google.adk.auth.auth_schemes import OAuth2, OAuthGrantType
from google.adk.auth.auth_tool import AuthConfig
from google.adk.tools.tool_context import ToolContext


def web_fetch(url: str, tool_context: ToolContext) -> dict:
    """Fetches the content of a web page and returns the parsed text.

    Use this tool to read the content of a website, blog post, or article.
    It uses httpx for fetching and BeautifulSoup for parsing the HTML.

    Args:
        url (str): The URL of the web page to fetch.

    Returns:
        dict: The status and the extracted text content.
    """
    import httpx
    from bs4 import BeautifulSoup

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }

    try:
        with httpx.Client(follow_redirects=True, headers=headers, timeout=30.0) as client:
            response = client.get(url)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, "lxml")
            
            # Remove script and style elements
            for script_or_style in soup(["script", "style"]):
                script_or_style.decompose()

            # Get text and clean up whitespace
            text = soup.get_text(separator="\n")
            lines = (line.strip() for line in text.splitlines())
            chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
            clean_text = "\n".join(chunk for chunk in chunks if chunk)

            return {
                "status": "success",
                "url": url,
                "content": clean_text[:10000],  # Limit content length
                "length": len(clean_text)
            }
    except Exception as e:
        return {"status": "error", "message": f"Failed to fetch {url}: {str(e)}"}

