"""
Active Directory / LDAP authentication module.

Supports two roles based on OU container paths:
  - student: can view printers, upload files, and print
  - staff:   full admin access (add/remove printers, config, etc.)
"""

from __future__ import annotations

import logging

from ldap3 import Server, Connection, ALL, SUBTREE
from ldap3.core.exceptions import LDAPException

logger = logging.getLogger(__name__)


def _build_server(config: dict) -> Server:
    """Build an ldap3 Server from the AD config block."""
    host = config.get("server", "")
    port = int(config.get("port", 389))
    use_ssl = config.get("use_ssl", False)
    return Server(host, port=port, use_ssl=use_ssl, get_info=ALL, connect_timeout=10)


def test_ad_connection(config: dict) -> dict:
    """Test whether we can bind to the AD server with the service account."""
    try:
        server = _build_server(config)
        bind_user = config.get("bind_user", "")
        bind_password = config.get("bind_password", "")
        conn = Connection(server, user=bind_user, password=bind_password, auto_bind=True,
                          read_only=True, receive_timeout=10)
        conn.unbind()
        return {"ok": True, "message": "Connection successful"}
    except LDAPException as e:
        logger.warning(f"AD connection test failed: {e}")
        return {"ok": False, "message": str(e)}
    except Exception as e:
        logger.warning(f"AD connection test failed: {e}")
        return {"ok": False, "message": str(e)}


def authenticate_user(username: str, password: str, config: dict) -> dict:
    """Authenticate a user against AD and determine their role.

    Returns:
        {"ok": True, "role": "staff"|"student", "display_name": "...", "username": "..."}
        or {"ok": False, "error": "reason"}
    """
    if not config.get("enabled"):
        return {"ok": False, "error": "Active Directory is not enabled"}

    server_addr = config.get("server", "")
    if not server_addr:
        return {"ok": False, "error": "AD server not configured"}

    base_dn = config.get("base_dn", "")
    bind_user = config.get("bind_user", "")
    bind_password = config.get("bind_password", "")
    student_ou = config.get("student_ou", "")
    staff_ou = config.get("staff_ou", "")

    try:
        server = _build_server(config)

        # Step 1: Bind with service account to find the user's DN
        svc_conn = Connection(server, user=bind_user, password=bind_password,
                              auto_bind=True, read_only=True, receive_timeout=10)

        # Search for the user by sAMAccountName
        search_filter = f"(sAMAccountName={_escape_ldap(username)})"
        svc_conn.search(
            search_base=base_dn,
            search_filter=search_filter,
            search_scope=SUBTREE,
            attributes=["distinguishedName", "displayName", "sAMAccountName", "memberOf"],
        )

        if not svc_conn.entries:
            svc_conn.unbind()
            return {"ok": False, "error": "User not found"}

        entry = svc_conn.entries[0]
        user_dn = str(entry.distinguishedName)
        display_name = str(entry.displayName) if entry.displayName else username
        svc_conn.unbind()

        # Step 2: Bind as the user to verify their password
        user_conn = Connection(server, user=user_dn, password=password,
                               auto_bind=True, read_only=True, receive_timeout=10)
        user_conn.unbind()

        # Step 3: Determine role based on OU path
        role = _determine_role(user_dn, staff_ou, student_ou)
        if role is None:
            return {"ok": False, "error": "User is not in an authorised OU"}

        logger.info(f"AD login: {username} ({display_name}) -> role={role}")
        return {"ok": True, "role": role, "display_name": display_name, "username": username}

    except LDAPException as e:
        msg = str(e)
        if "invalidCredentials" in msg:
            return {"ok": False, "error": "Invalid username or password"}
        logger.warning(f"AD auth error for {username}: {e}")
        return {"ok": False, "error": "Authentication failed"}
    except Exception as e:
        logger.error(f"AD auth unexpected error: {e}")
        return {"ok": False, "error": "Authentication error"}


def lookup_user(username: str, config: dict) -> dict:
    """Look up a user in AD by sAMAccountName (no password required).

    Used for SSO/Kerberos passthrough where Apache has already verified identity.

    Returns:
        {"ok": True, "role": "staff"|"student", "display_name": "...", "username": "..."}
        or {"ok": False, "error": "reason"}
    """
    if not config.get("enabled"):
        return {"ok": False, "error": "Active Directory is not enabled"}

    server_addr = config.get("server", "")
    if not server_addr:
        return {"ok": False, "error": "AD server not configured"}

    base_dn = config.get("base_dn", "")
    bind_user = config.get("bind_user", "")
    bind_password = config.get("bind_password", "")
    student_ou = config.get("student_ou", "")
    staff_ou = config.get("staff_ou", "")

    try:
        server = _build_server(config)
        svc_conn = Connection(server, user=bind_user, password=bind_password,
                              auto_bind=True, read_only=True, receive_timeout=10)

        search_filter = f"(sAMAccountName={_escape_ldap(username)})"
        svc_conn.search(
            search_base=base_dn,
            search_filter=search_filter,
            search_scope=SUBTREE,
            attributes=["distinguishedName", "displayName", "sAMAccountName"],
        )

        if not svc_conn.entries:
            svc_conn.unbind()
            return {"ok": False, "error": "User not found"}

        entry = svc_conn.entries[0]
        user_dn = str(entry.distinguishedName)
        display_name = str(entry.displayName) if entry.displayName else username
        svc_conn.unbind()

        role = _determine_role(user_dn, staff_ou, student_ou)
        if role is None:
            return {"ok": False, "error": "User is not in an authorised OU"}

        logger.info(f"SSO login: {username} ({display_name}) -> role={role}")
        return {"ok": True, "role": role, "display_name": display_name, "username": username}

    except LDAPException as e:
        logger.warning(f"AD lookup error for {username}: {e}")
        return {"ok": False, "error": "AD lookup failed"}
    except Exception as e:
        logger.error(f"AD lookup unexpected error: {e}")
        return {"ok": False, "error": "AD lookup error"}


def _determine_role(user_dn: str, staff_ou: str, student_ou: str) -> str | None:
    """Check which OU the user DN falls under.

    Staff OU is checked first — if a user matches both, they get staff access.
    Returns 'staff', 'student', or None.
    """
    dn_upper = user_dn.upper()
    if staff_ou and staff_ou.upper() in dn_upper:
        return "staff"
    if student_ou and student_ou.upper() in dn_upper:
        return "student"
    return None


def _escape_ldap(value: str) -> str:
    """Escape special characters for LDAP search filter (RFC 4515)."""
    replacements = {
        "\\": "\\5c",
        "*": "\\2a",
        "(": "\\28",
        ")": "\\29",
        "\x00": "\\00",
    }
    for char, escaped in replacements.items():
        value = value.replace(char, escaped)
    return value
