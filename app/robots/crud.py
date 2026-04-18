"""
CRUD operations for robot management.
"""
from datetime import datetime, timedelta
from typing import Optional, List
import json
import random
import string
import sqlite3
from ..database.connection import get_db_connection
from .models import RobotCreate, RobotUpdate, RobotInDB, RobotConfigCreate, RobotConfigUpdate, RobotConfigInDB, RobotStatus


def get_robot_by_mac(mac_address: str) -> Optional[RobotInDB]:
    """Get a robot by MAC address."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT mac_address, robot_id, name, owner_username, is_online, last_seen, created_at, updated_at
            FROM robots WHERE mac_address = ?
            """,
            (mac_address,)
        )
        row = cursor.fetchone()
        
        if row:
            return RobotInDB(
                mac_address=row['mac_address'],
                robot_id=row['robot_id'],
                name=row['name'],
                owner_username=row['owner_username'],
                is_online=bool(row['is_online']),
                last_seen=row['last_seen'],
                created_at=row['created_at'],
                updated_at=row['updated_at']
            )
        return None


def create_robot(robot: RobotCreate, owner_username: Optional[str] = None) -> Optional[RobotInDB]:
    """Create a new robot with optional owner."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO robots (
                    mac_address, robot_id, name, owner_username, 
                    is_online, last_seen, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 0, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (robot.mac_address, robot.robot_id, robot.name, owner_username)
            )
            conn.commit()
            
            # Return the created robot
            return get_robot_by_mac(robot.mac_address)
        except sqlite3.IntegrityError:
            raise ValueError(f"Robot with MAC '{robot.mac_address}' already exists")


def update_robot(mac_address: str, robot_update: RobotUpdate) -> Optional[RobotInDB]:
    """Update robot information."""
    db_robot = get_robot_by_mac(mac_address)
    if not db_robot:
        return None
    
    # Prepare update fields
    updates = []
    params = []
    
    if robot_update.name is not None:
        updates.append("name = ?")
        params.append(robot_update.name)
    
    # Add updated_at timestamp
    updates.append("updated_at = CURRENT_TIMESTAMP")
    
    # Add MAC address to the end of params for WHERE clause
    params.append(mac_address)
    
    if updates:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            query = f"UPDATE robots SET {', '.join(updates)} WHERE mac_address = ?"
            cursor.execute(query, params)
            conn.commit()
    
    return get_robot_by_mac(mac_address)


def delete_robot(mac_address: str) -> bool:
    """Delete a robot."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM robots WHERE mac_address = ?", (mac_address,))
        conn.commit()
        return cursor.rowcount > 0


def get_all_robots(owner_username: Optional[str] = None) -> List[RobotInDB]:
    """Get all robots, optionally filtered by owner."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        if owner_username:
            cursor.execute(
                """
                SELECT mac_address, robot_id, name, owner_username, is_online, last_seen, created_at, updated_at
                FROM robots WHERE owner_username = ?
                ORDER BY created_at DESC
                """,
                (owner_username,)
            )
        else:
            cursor.execute(
                """
                SELECT mac_address, robot_id, name, owner_username, is_online, last_seen, created_at, updated_at
                FROM robots
                ORDER BY created_at DESC
                """
            )
        rows = cursor.fetchall()
        
        return [
            RobotInDB(
                mac_address=row['mac_address'],
                robot_id=row['robot_id'],
                name=row['name'],
                owner_username=row['owner_username'],
                is_online=bool(row['is_online']),
                last_seen=row['last_seen'],
                created_at=row['created_at'],
                updated_at=row['updated_at']
            )
            for row in rows
        ]


def update_robot_status(mac_address: str, is_online: bool) -> bool:
    """Update robot online status."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE robots 
            SET is_online = ?, last_seen = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
            WHERE mac_address = ?
            """,
            (int(is_online), mac_address)
        )
        conn.commit()
        return cursor.rowcount > 0


def touch_robot_last_seen(mac_address: str) -> bool:
    """Update only last_seen timestamp without changing online state."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE robots
            SET last_seen = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
            WHERE mac_address = ?
            """,
            (mac_address,)
        )
        conn.commit()
        return cursor.rowcount > 0


def get_robot_config(mac_address: str) -> Optional[RobotConfigInDB]:
    """Get robot configuration."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT mac_address, config, created_at, updated_at
            FROM robots WHERE mac_address = ?
            """,
            (mac_address,)
        )
        row = cursor.fetchone()
        
        if row:
            config_data = json.loads(row['config'])
            return RobotConfigInDB(
                mac_address=row['mac_address'],
                system_prompt=config_data.get('system_prompt'),
                voice_config=config_data.get('voice_config'),
                llm_config=config_data.get('llm_config'),
                tts_config=config_data.get('tts_config'),
                stt_config=config_data.get('stt_config'),
                version=config_data.get('version', 1) if config_data.get('version') is not None else 1,
                created_at=row['created_at'],
                updated_at=row['updated_at']
            )
        return None


def update_robot_config(mac_address: str, config_update: RobotConfigUpdate) -> Optional[RobotConfigInDB]:
    """Update robot configuration."""
    # Get current config
    current_config = get_robot_config(mac_address)
    if not current_config:
        # Create default config if it doesn't exist
        current_config_data = {}
    else:
        # Convert current config to dict
        current_config_data = {
            'system_prompt': current_config.system_prompt,
            'voice_config': current_config.voice_config,
            'llm_config': current_config.llm_config,
            'tts_config': current_config.tts_config,
            'stt_config': current_config.stt_config,
            'version': current_config.version + 1
        }
    
    # Update config with new values
    if config_update.system_prompt is not None:
        current_config_data['system_prompt'] = config_update.system_prompt
    if config_update.voice_config is not None:
        current_config_data['voice_config'] = config_update.voice_config
    if config_update.llm_config is not None:
        current_config_data['llm_config'] = config_update.llm_config
    if config_update.tts_config is not None:
        current_config_data['tts_config'] = config_update.tts_config
    if config_update.stt_config is not None:
        current_config_data['stt_config'] = config_update.stt_config
    
    # Increment version
    current_config_data['version'] = current_config_data.get('version', 1) + 1
    
    # Save updated config
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE robots 
            SET config = ?, updated_at = CURRENT_TIMESTAMP
            WHERE mac_address = ?
            """,
            (json.dumps(current_config_data), mac_address)
        )
        conn.commit()
        
        if cursor.rowcount > 0:
            # Return updated config
            return RobotConfigInDB(
                mac_address=mac_address,
                system_prompt=current_config_data.get('system_prompt'),
                voice_config=current_config_data.get('voice_config'),
                llm_config=current_config_data.get('llm_config'),
                tts_config=current_config_data.get('tts_config'),
                stt_config=current_config_data.get('stt_config'),
                version=int(current_config_data.get('version', 1)),
                created_at=datetime.now(),
                updated_at=datetime.now()
            )
        return None


def reset_robot_config(mac_address: str) -> bool:
    """Reset robot configuration to default."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE robots 
            SET config = '{}', updated_at = CURRENT_TIMESTAMP
            WHERE mac_address = ?
            """,
            (mac_address,)
        )
        conn.commit()
        return cursor.rowcount > 0


def get_robot_status(mac_address: str) -> Optional[RobotStatus]:
    """Get robot status."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT mac_address, robot_id, name, is_online, last_seen
            FROM robots WHERE mac_address = ?
            """,
            (mac_address,)
        )
        row = cursor.fetchone()
        
        if row:
            return RobotStatus(
                mac_address=row['mac_address'],
                robot_id=row['robot_id'],
                name=row['name'],
                is_online=bool(row['is_online']),
                last_seen=row['last_seen']
            )
        return None


# ── OTP functions ──

MAX_OTP_ATTEMPTS = 5  # Lock OTP after this many wrong attempts


def generate_otp(mac_address: str, ttl_minutes: int = 10) -> Optional[str]:
    """Generate a 6-digit OTP for a robot. Resets attempt counter. Returns the OTP string or None if robot not found."""
    robot = get_robot_by_mac(mac_address)
    if not robot:
        return None

    otp = ''.join(random.choices(string.digits, k=6))
    expires_at = (datetime.utcnow() + timedelta(minutes=ttl_minutes)).isoformat()

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE robots SET otp = ?, otp_expires_at = ?, otp_attempts = 0, updated_at = CURRENT_TIMESTAMP WHERE mac_address = ?",
            (otp, expires_at, mac_address),
        )
        conn.commit()
    return otp


def get_otp_attempts(mac_address: str) -> int:
    """Get the current number of failed OTP attempts for a robot."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT otp_attempts FROM robots WHERE mac_address = ?",
            (mac_address,),
        )
        row = cursor.fetchone()
        return int(row['otp_attempts'] or 0) if row else 0


def _increment_otp_attempts(mac_address: str) -> int:
    """Increment failed OTP attempt counter. Returns new count."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE robots SET otp_attempts = COALESCE(otp_attempts, 0) + 1, updated_at = CURRENT_TIMESTAMP WHERE mac_address = ?",
            (mac_address,),
        )
        conn.commit()
        # Return new count
        cursor.execute("SELECT otp_attempts FROM robots WHERE mac_address = ?", (mac_address,))
        row = cursor.fetchone()
        return int(row['otp_attempts'] or 0) if row else 0


def _lock_otp(mac_address: str):
    """Clear OTP so no more attempts are possible (locked)."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE robots SET otp = NULL, otp_expires_at = NULL, updated_at = CURRENT_TIMESTAMP WHERE mac_address = ?",
            (mac_address,),
        )
        conn.commit()


def find_robot_by_otp(otp: str) -> Optional[dict]:
    """
    Find an unclaimed robot by its OTP code (no MAC needed).
    Returns dict with mac_address + otp info, or None.
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT mac_address, otp, otp_expires_at, otp_attempts
            FROM robots
            WHERE otp = ? AND owner_username IS NULL
            """,
            (otp,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return {
            "mac_address": row["mac_address"],
            "otp": row["otp"],
            "otp_expires_at": row["otp_expires_at"],
            "otp_attempts": int(row["otp_attempts"] or 0),
        }


def claim_robot_by_otp(otp: str, owner_username: str) -> dict:
    """
    Claim a robot using only a 6-digit OTP (no MAC required).
    Looks up the robot by OTP, checks attempts / expiry, assigns owner.
    Returns dict: {"ok": bool, "error": str|None, "attempts_left": int, "robot": RobotInDB|None}
    """
    info = find_robot_by_otp(otp)
    if not info:
        # OTP not found at all – could be wrong code. We can't track per-robot attempts
        # without knowing the robot. Return generic error.
        return {"ok": False, "error": "not_found", "attempts_left": -1, "robot": None}

    mac = info["mac_address"]
    attempts_used = info["otp_attempts"]

    # Already locked out?
    if attempts_used >= MAX_OTP_ATTEMPTS:
        return {"ok": False, "error": "locked", "attempts_left": 0, "robot": None}

    # Check expiry
    if info["otp_expires_at"]:
        expires = datetime.fromisoformat(info["otp_expires_at"])
        if datetime.utcnow() > expires:
            return {"ok": False, "error": "expired", "attempts_left": MAX_OTP_ATTEMPTS - attempts_used, "robot": None}

    # OTP matches (we already found the row by otp value) → claim!
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE robots 
            SET owner_username = ?, otp = NULL, otp_expires_at = NULL, otp_attempts = 0, updated_at = CURRENT_TIMESTAMP
            WHERE mac_address = ?
            """,
            (owner_username, mac),
        )
        conn.commit()

    robot = get_robot_by_mac(mac)
    return {"ok": True, "error": None, "attempts_left": MAX_OTP_ATTEMPTS, "robot": robot}


def increment_global_otp_fail(otp: str):
    """
    When an OTP is entered but doesn't match any robot, we can't track per-robot.
    This is a no-op placeholder; rate-limiting should be done at the API layer.
    """
    pass
