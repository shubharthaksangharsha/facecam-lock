"""
Authentication helper for FaceCam Lock.
Validates user password fallback against system credentials.
"""

import os
import subprocess
import getpass
from typing import Tuple


def get_current_username() -> str:
    """Return the currently logged in user."""
    return (
        os.environ.get("SUDO_USER")
        or os.environ.get("USER")
        or getpass.getuser()
    )


def verify_user_password(password: str, username: str = None) -> Tuple[bool, str]:
    """
    Verify user password using system sudo/pam authentication.
    Returns (is_valid: bool, message: str).
    """
    if not password:
        return False, "Password cannot be empty"

    target_user = username or get_current_username()

    try:
        # Run sudo -S -k -v to check credentials without reusing timestamp
        proc = subprocess.run(
            ["sudo", "-S", "-k", "-v"],
            input=f"{password}\n",
            capture_output=True,
            text=True,
            timeout=5
        )
        if proc.returncode == 0:
            return True, "Authentication successful"
        else:
            return False, "Incorrect password"
    except subprocess.TimeoutExpired:
        return False, "Authentication timed out"
    except Exception as e:
        return False, f"Authentication error: {e}"
