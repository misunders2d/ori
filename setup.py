import os
import sys

from dotenv import load_dotenv, set_key
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

console = Console()

ENV_FILE_PATH = os.environ.get("DOTENV_PATH", "./data/.env")

CONFIG_FIELDS = [
    {
        "key": "GOOGLE_API_KEY",
        "label": "Google Gemini API Key",
        "required": True,
        "section": "required",
        "help": "Powers the agent brain. Get one free at https://aistudio.google.com/app/apikey",
        "validate": True,
    },
    {
        "key": "TELEGRAM_BOT_TOKEN",
        "label": "Telegram Bot Token",
        "required": False,
        "section": "messaging",
        "help": "Get a free Telegram token by talking to @BotFather.",
    },
    {
        "key": "GITHUB_TOKEN",
        "label": "GitHub Personal Access Token",
        "required": False,
        "section": "optional",
        "help": "For self-evolution. Generate at github.com/settings/tokens with 'repo' scope.",
    },
    {
        "key": "GITHUB_REPO",
        "label": "GitHub Repository (owner/repo)",
        "required": False,
        "section": "optional",
        "help": "The repo the agent pushes code improvements to.",
    },
]


def validate_google_key(key: str) -> bool:
    """Checks if the Google API Key is valid by pinging the model list API."""
    try:
        os.environ["GOOGLE_API_KEY"] = key
        from google.genai import Client
        client = Client(api_key=key)
        client.models.get(model="gemini-3-flash-preview")
        return True
    except Exception as e:
        console.print(f"  [bold red]Invalid Key:[/bold red] {e}")
        return False


def mask(val: str) -> str:
    if not val:
        return "Not set"
    return f"{val[:4]}...{val[-4:]}" if len(val) > 8 else "********"


def run_setup():
    console.clear()
    console.print(Panel.fit(
        "[bold blue]Autonomous Amazon Manager[/bold blue]\n[italic]Setup Wizard[/italic]",
        border_style="blue"
    ))

    os.makedirs(os.path.dirname(os.path.abspath(ENV_FILE_PATH)), exist_ok=True)
    load_dotenv(ENV_FILE_PATH)

    current_section = None
    section_titles = {
        "required": "[bold red]Required[/bold red]",
        "messaging": "[bold yellow]Messaging[/bold yellow] (set up at least one to chat with your bot)",
        "optional": "[bold cyan]Optional Integrations[/bold cyan] (press Enter to skip any)",
    }

    for field in CONFIG_FIELDS:
        key = field["key"]
        label = field["label"]
        required = field["required"]
        help_text = field.get("help", "")
        section = field["section"]

        # Print section header
        if section != current_section:
            current_section = section
            console.print(f"\n{'─' * 50}")
            console.print(f"  {section_titles[section]}")
            console.print(f"{'─' * 50}")

        current_val = os.environ.get(key, "")

        while True:
            console.print(f"\n  [bold]{label}[/bold]")
            if help_text:
                console.print(f"  [dim]{help_text}[/dim]")
            if current_val:
                console.print(f"  Current: [cyan]{mask(current_val)}[/cyan]")

            if required and not current_val:
                prompt_text = f"  Enter {label}"
            elif current_val:
                prompt_text = "  New value (Enter to keep current)"
            else:
                prompt_text = "  Enter value (Enter to skip)"

            user_input = Prompt.ask(prompt_text, default="").strip()

            if not user_input:
                if required and not current_val:
                    console.print("  [red]This key is required.[/red]")
                    continue
                break

            if field.get("validate"):
                console.print("  [yellow]Validating...[/yellow]")
                if validate_google_key(user_input):
                    set_key(ENV_FILE_PATH, key, user_input)
                    os.environ[key] = user_input
                    console.print("  [green]Verified and saved.[/green]")
                    break
                else:
                    continue
            else:
                set_key(ENV_FILE_PATH, key, user_input)
                os.environ[key] = user_input
                console.print("  [green]Saved.[/green]")
                break

    # Generate admin passcode if missing
    import secrets
    if not os.environ.get("ADMIN_PASSCODE"):
        passcode = secrets.token_hex(16).upper()
        set_key(ENV_FILE_PATH, "ADMIN_PASSCODE", passcode)

    console.print(f"\n{'═' * 50}")
    console.print("[bold green]  Setup Complete![/bold green]")
    console.print(f"  Config saved to: [cyan]{ENV_FILE_PATH}[/cyan]")

    # Summary
    has_tg = bool(os.environ.get("TELEGRAM_BOT_TOKEN"))
    if has_tg:
        platforms = []
        if has_tg:
            platforms.append("Telegram (polling mode — no public URL needed)")
        console.print(f"  Messaging: {', '.join(platforms)}")
    else:
        console.print("  [yellow]No messaging platform configured. You can add one later via /setup.[/yellow]")

    console.print("\n  The server will start automatically.")
    console.print(f"{'═' * 50}\n")


if __name__ == "__main__":
    try:
        run_setup()
    except KeyboardInterrupt:
        console.print("\n[yellow]Setup cancelled.[/yellow]")
        sys.exit(0)
