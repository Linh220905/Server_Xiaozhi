"""
Database connection module for robot and user management.
Uses SQLite as the primary database.
"""
import sqlite3
import os
import bcrypt
from pathlib import Path
from contextlib import contextmanager
from typing import Generator


DATABASE_PATH = Path(__file__).parent.parent / "data" / "nexus.db"

# Create data directory if it doesn't exist
DATABASE_PATH.parent.mkdir(exist_ok=True)


def init_database():
    """Initialize the database with required tables."""
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    
    # Create users table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'user',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Create robots table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS robots (
            mac_address TEXT PRIMARY KEY,
            robot_id TEXT UNIQUE NOT NULL,
            name TEXT,
            config TEXT DEFAULT '{}',
            owner_username TEXT,
            is_online INTEGER DEFAULT 0,
            last_seen TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (owner_username) REFERENCES users(username)
        )
    """)

    # Migrate legacy robots table (older schema may miss these columns)
    cursor.execute("PRAGMA table_info(robots)")
    robot_columns = {row[1] for row in cursor.fetchall()}
    if "name" not in robot_columns:
        cursor.execute("ALTER TABLE robots ADD COLUMN name TEXT")
    if "is_online" not in robot_columns:
        cursor.execute("ALTER TABLE robots ADD COLUMN is_online INTEGER DEFAULT 0")
    if "last_seen" not in robot_columns:
        cursor.execute("ALTER TABLE robots ADD COLUMN last_seen TIMESTAMP")
    if "otp" not in robot_columns:
        cursor.execute("ALTER TABLE robots ADD COLUMN otp TEXT")
    if "otp_expires_at" not in robot_columns:
        cursor.execute("ALTER TABLE robots ADD COLUMN otp_expires_at TIMESTAMP")
    if "otp_attempts" not in robot_columns:
        cursor.execute("ALTER TABLE robots ADD COLUMN otp_attempts INTEGER DEFAULT 0")
    
    # Create chat_sessions table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            robot_mac TEXT NOT NULL,
            session_id TEXT NOT NULL,
            messages TEXT DEFAULT '[]',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (robot_mac) REFERENCES robots(mac_address) ON DELETE CASCADE
        )
    """)

    # Create indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_robots_owner ON robots(owner_username)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_sessions_robot ON chat_sessions(robot_mac)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_sessions_session ON chat_sessions(session_id)")
    
    # Insert default admin user if not exists, lấy từ biến môi trường nếu có
    import os
    admin_username = os.environ.get("ADMIN_USERNAME", "admin")
    admin_password = os.environ.get("ADMIN_PASSWORD", "admin1234")
    admin_hash = bcrypt.hashpw(admin_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    cursor.execute("""
        INSERT OR IGNORE INTO users (username, password_hash, role) 
        VALUES (?, ?, ?)
    """, (admin_username, admin_hash, "admin"))

    # Migrate legacy invalid admin hash to a valid bcrypt hash
    cursor.execute("SELECT password_hash FROM users WHERE username = ?", (admin_username,))
    row = cursor.fetchone()
    if row:
        stored_hash = row[0] if isinstance(row, tuple) else row["password_hash"]
        needs_migration = False
        if not isinstance(stored_hash, str):
            needs_migration = True
        else:
            try:
                # Validate bcrypt hash format by attempting check.
                bcrypt.checkpw(b"__probe__", stored_hash.encode("utf-8"))
            except ValueError:
                needs_migration = True

        if needs_migration:
            cursor.execute(
                "UPDATE users SET password_hash = ?, role = 'admin', updated_at = CURRENT_TIMESTAMP WHERE username = ?",
                (admin_hash, admin_username),
            )
    
    conn.commit()
    conn.close()


@contextmanager
def get_db_connection() -> Generator[sqlite3.Connection, None, None]:
    """Context manager for database connections."""
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row  # Enable column access by name
    try:
        yield conn
    finally:
        conn.close()