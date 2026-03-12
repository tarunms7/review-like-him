"""Interactive GitHub App setup helper for CLI init command."""

import logging

logger = logging.getLogger("review-bot")

# Default permissions required by the review bot
_DEFAULT_PERMISSIONS = {
    "pull_requests": "write",
    "issues": "write",
    "contents": "read",
    "metadata": "read",
}

_DEFAULT_EVENTS = [
    "pull_request",
    "pull_request_review",
    "pull_request_review_comment",
]


def generate_app_manifest(webhook_url: str) -> dict:
    """Generate a GitHub App manifest for browser-based creation.

    The manifest pre-fills webhook URL, permissions, and events so the user
    only needs to confirm in the browser.

    Args:
        webhook_url: The public URL that will receive webhook events.

    Returns:
        A dict suitable for POSTing to GitHub's app manifest creation flow.
    """
    return {
        "name": "review-like-him-bot",
        "url": "https://github.com/apps/review-like-him-bot",
        "hook_attributes": {
            "url": webhook_url,
            "active": True,
        },
        "redirect_url": f"{webhook_url}/setup/callback",
        "setup_url": f"{webhook_url}/setup/complete",
        "public": False,
        "default_permissions": _DEFAULT_PERMISSIONS,
        "default_events": _DEFAULT_EVENTS,
    }


def guide_app_creation() -> dict:
    """Guide the user through interactive GitHub App creation.

    Walks through the steps to create a GitHub App, collect the App ID,
    download the private key, and set the webhook secret.

    Returns:
        A dict with keys: app_id, private_key_path, webhook_secret.
    """
    print("\n=== GitHub App Setup ===\n")
    print("Follow these steps to create your GitHub App:\n")
    print("1. Go to https://github.com/settings/apps/new")
    print("2. Fill in the app name (e.g. 'review-like-him-bot')")
    print("3. Set the webhook URL to your server's public URL + /webhook")
    print("4. Under Permissions, grant:")
    for perm, access in _DEFAULT_PERMISSIONS.items():
        print(f"   - {perm}: {access}")
    print("5. Subscribe to events:")
    for event in _DEFAULT_EVENTS:
        print(f"   - {event}")
    print("6. Generate a private key and download it")
    print("7. Note down your App ID and webhook secret\n")

    app_id = input("Enter your GitHub App ID: ").strip()
    private_key_path = input("Enter the path to your private key PEM file: ").strip()
    webhook_secret = input("Enter your webhook secret: ").strip()

    result = {
        "app_id": app_id,
        "private_key_path": private_key_path,
        "webhook_secret": webhook_secret,
    }

    logger.info("GitHub App setup complete: App ID %s", app_id)
    return result
