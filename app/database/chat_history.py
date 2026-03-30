"""
CRUD operations for chat_sessions table.
Mỗi session trò chuyện được lưu riêng biệt theo session_id.
Không ghi đè giữa các session — mỗi robot có thể có nhiều session.
Trong cùng 1 session thì UPDATE messages (vì handler gọi save sau mỗi lượt hội thoại).
"""
import json
import logging
from datetime import datetime

from app.database.connection import get_db_connection

logger = logging.getLogger(__name__)


def save_chat_session(robot_mac: str, session_id: str, messages: list[dict]) -> None:
    """Lưu/cập nhật session chat.

    - Nếu session_id đã tồn tại → UPDATE messages (cùng session, thêm tin nhắn mới).
    - Nếu session_id chưa có → INSERT mới (session mới, không ghi đè session cũ).
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        messages_json = json.dumps(messages, ensure_ascii=False)
        now = datetime.utcnow().isoformat()

        cursor.execute(
            "SELECT id FROM chat_sessions WHERE session_id = ?",
            (session_id,),
        )
        existing = cursor.fetchone()

        if existing:
            cursor.execute(
                """UPDATE chat_sessions
                   SET messages = ?, updated_at = ?
                   WHERE session_id = ?""",
                (messages_json, now, session_id),
            )
        else:
            cursor.execute(
                """INSERT INTO chat_sessions (robot_mac, session_id, messages, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (robot_mac, session_id, messages_json, now, now),
            )
        conn.commit()
    logger.debug("[%s] Chat session %s saved (%d messages)", robot_mac, session_id, len(messages))


def get_chat_sessions_for_user(username: str) -> list[dict]:
    """Lấy tất cả chat sessions của các robot thuộc user.

    Returns:
        List of dicts, mỗi dict chứa thông tin robot + session chat.
        Sắp xếp theo thời gian mới nhất trước.
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT r.mac_address, r.name, r.robot_id, r.is_online,
                      cs.session_id, cs.messages, cs.created_at, cs.updated_at
               FROM chat_sessions cs
               INNER JOIN robots r ON cs.robot_mac = r.mac_address
               WHERE r.owner_username = ?
               ORDER BY cs.updated_at DESC""",
            (username,),
        )
        rows = cursor.fetchall()
        result = []
        for row in rows:
            try:
                messages = json.loads(row["messages"]) if row["messages"] else []
            except (json.JSONDecodeError, TypeError):
                messages = []

            result.append({
                "robot_mac": row["mac_address"],
                "robot_name": row["name"] or row["robot_id"],
                "robot_id": row["robot_id"],
                "is_online": bool(row["is_online"]),
                "session_id": row["session_id"],
                "messages": messages,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            })
        return result
