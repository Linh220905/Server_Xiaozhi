from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from app.database.connection import get_db_connection

LearningMode = Literal["vocabulary", "conversation"]
FLASHCARD_BASE_PATH = "/api/learning/flashcard"
DEFAULT_VOCAB_SEED_PATH = Path(__file__).parent.parent / "data" / "default_vocab_topics.json"

def _load_default_vocab_topics() -> list[dict]:
    if not DEFAULT_VOCAB_SEED_PATH.exists():
        return []
    try:
        data = json.loads(DEFAULT_VOCAB_SEED_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


CONVERSATION_TOPICS: list[dict] = [
    {
        "id": "greet",
        "icon": "👋",
        "title": "Chào hỏi cơ bản",
        "desc": "Làm quen và tự giới thiệu bản thân",
        "tags": ["Hàng ngày", "Cơ bản"],
        "category": "daily",
        "script": [
            {"speaker": "A", "en": "Hi there! Nice to meet you.", "vi": "Xin chào! Rất vui được gặp bạn."},
            {"speaker": "B", "en": "Nice to meet you too. I'm Linh.", "vi": "Mình cũng rất vui được gặp bạn. Mình là Linh."},
            {"speaker": "A", "en": "Are you new here?", "vi": "Bạn mới ở đây à?"},
        ],
    },
    {
        "id": "airport",
        "icon": "🛫",
        "title": "Tại sân bay",
        "desc": "Check-in và hỏi thông tin chuyến bay",
        "tags": ["Du lịch", "Trung cấp"],
        "category": "travel",
        "script": [
            {"speaker": "A", "en": "Passport and ticket, please.", "vi": "Hộ chiếu và vé của bạn, vui lòng."},
            {"speaker": "B", "en": "Here you are. Is there a window seat?", "vi": "Đây ạ. Còn ghế cạnh cửa sổ không?"},
            {"speaker": "A", "en": "Yes, seat 14A for you.", "vi": "Có, ghế 14A cho bạn."},
        ],
    },
    {
        "id": "hotel",
        "icon": "🏨",
        "title": "Khách sạn",
        "desc": "Đặt phòng và check-in",
        "tags": ["Du lịch", "Trung cấp"],
        "category": "travel",
        "script": [
            {"speaker": "A", "en": "I have a reservation under Nguyen.", "vi": "Tôi có đặt phòng dưới tên Nguyễn."},
            {"speaker": "B", "en": "Sure. Can I see your ID?", "vi": "Vâng. Cho tôi xin giấy tờ tùy thân được không?"},
            {"speaker": "A", "en": "Is breakfast included?", "vi": "Bữa sáng có bao gồm không?"},
        ],
    },
    {
        "id": "restaurant",
        "icon": "🍽️",
        "title": "Nhà hàng",
        "desc": "Gọi món và thanh toán",
        "tags": ["Hàng ngày", "Cơ bản"],
        "category": "daily",
        "script": [
            {"speaker": "A", "en": "Table for two, please.", "vi": "Cho tôi bàn hai người."},
            {"speaker": "B", "en": "Would you like to see the menu?", "vi": "Bạn muốn xem thực đơn không?"},
            {"speaker": "A", "en": "Yes, and I'd like sparkling water.", "vi": "Có, và cho tôi nước có ga."},
        ],
    },
    {
        "id": "interview",
        "icon": "💼",
        "title": "Phỏng vấn",
        "desc": "Trả lời câu hỏi phỏng vấn công việc",
        "tags": ["Công việc", "Nâng cao"],
        "category": "work",
        "script": [
            {"speaker": "A", "en": "Tell me about yourself.", "vi": "Hãy giới thiệu về bản thân bạn."},
            {"speaker": "B", "en": "I have three years of software experience.", "vi": "Tôi có ba năm kinh nghiệm phần mềm."},
            {"speaker": "A", "en": "What is your greatest strength?", "vi": "Điểm mạnh lớn nhất của bạn là gì?"},
        ],
    },
    {
        "id": "shopping",
        "icon": "🛒",
        "title": "Mua sắm",
        "desc": "Hỏi giá, thử đồ, thanh toán",
        "tags": ["Hàng ngày", "Cơ bản"],
        "category": "daily",
        "script": [
            {"speaker": "A", "en": "How much is this jacket?", "vi": "Áo khoác này bao nhiêu tiền?"},
            {"speaker": "B", "en": "It's 89 dollars.", "vi": "89 đô la."},
            {"speaker": "A", "en": "Can I try it on?", "vi": "Tôi có thể mặc thử không?"},
        ],
    },
]


def _strip_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFD", text or "")
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


def normalize_text(text: str) -> str:
    value = _strip_accents((text or "").lower())
    value = re.sub(r"[^a-z0-9\s]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def get_learning_payload() -> dict:
    vocabulary = _build_vocab_topics_with_images()
    vocabulary_map = {topic["id"]: topic for topic in vocabulary}
    return {
        "vocabulary": vocabulary,
        "vocabulary_map": vocabulary_map,
        "conversation": CONVERSATION_TOPICS,
    }


def build_flashcard_image_url(topic_id: str, word: str, meaning: str) -> str:
    return (
        f"{FLASHCARD_BASE_PATH}?topic_id={quote(topic_id)}"
        f"&word={quote(word)}&meaning={quote(meaning)}"
        "&fmt=png&w=320&h=240"
    )


def _seed_default_vocab_if_empty() -> None:
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(1) AS n FROM vocab_topics")
        row = cur.fetchone()
        total_topics = int(row["n"] if row and "n" in row.keys() else 0)
        if total_topics > 0:
            return

        for topic in _load_default_vocab_topics():
            topic_id = str(topic.get("id") or "").strip()
            cur.execute(
                """INSERT OR IGNORE INTO vocab_topics
                   (topic_id, icon, name, level, category, count)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    topic_id,
                    str(topic.get("icon") or ""),
                    str(topic.get("name") or ""),
                    str(topic.get("level") or topic.get("category") or "beginner"),
                    str(topic.get("category") or topic.get("level") or "beginner"),
                    int(topic.get("count") or len(topic.get("words") or [])),
                ),
            )

            for idx, item in enumerate(topic.get("words") or [], start=1):
                word = str(item.get("word") or "").strip()
                meaning = str(item.get("meaning") or "").strip()
                if not word or not meaning:
                    continue
                custom_image_url = str(item.get("image_url") or "").strip() or None
                cur.execute(
                    """INSERT INTO vocab_words
                       (topic_id, word, meaning, image_url, sort_order)
                       VALUES (?, ?, ?, ?, ?)""",
                    (topic_id, word, meaning, custom_image_url, idx),
                )
        conn.commit()


def _build_vocab_topics_with_images() -> list[dict]:
    _seed_default_vocab_if_empty()
    topics: list[dict] = []
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT topic_id, icon, name, level, category, count
               FROM vocab_topics
               ORDER BY id ASC"""
        )
        topic_rows = cur.fetchall()

        for topic_row in topic_rows:
            topic_id = str(topic_row["topic_id"] or "")
            cur.execute(
                """SELECT word, meaning, image_url, sort_order
                   FROM vocab_words
                   WHERE topic_id = ?
                   ORDER BY sort_order ASC, id ASC""",
                (topic_id,),
            )
            word_rows = cur.fetchall()

            words: list[dict] = []
            for row in word_rows:
                word = str(row["word"] or "")
                meaning = str(row["meaning"] or "")
                custom_image_url = str(row["image_url"] or "").strip()
                words.append(
                    {
                        "word": word,
                        "meaning": meaning,
                        "image_url": custom_image_url or build_flashcard_image_url(topic_id, word, meaning),
                    }
                )

            topics.append(
                {
                    "id": topic_id,
                    "icon": str(topic_row["icon"] or ""),
                    "name": str(topic_row["name"] or ""),
                    "count": len(words) if words else int(topic_row["count"] or 0),
                    "level": str(topic_row["level"] or "beginner"),
                    "category": str(topic_row["category"] or "beginner"),
                    "words": words,
                }
            )
    return topics


def get_topic_by_id(mode: LearningMode | str, topic_id: str) -> dict | None:
    topic_id_clean = (topic_id or "").strip().lower()
    source = _build_vocab_topics_with_images() if mode == "vocabulary" else CONVERSATION_TOPICS
    for topic in source:
        if str(topic.get("id", "")).strip().lower() == topic_id_clean:
            return topic
    return None


def _topic_aliases() -> dict[str, list[str]]:
    return {
        "travel": ["du lich", "san bay", "khach san"],
        "work": ["cong viec", "van phong", "phong van"],
        "food": ["am thuc", "nha hang", "do an"],
        "health": ["y te", "benh vien", "suc khoe"],
        "technology": ["cong nghe", "ky thuat", "it"],
        "education": ["giao duc", "hoc tap", "truong hoc"],
        "greet": ["chao hoi", "lam quen"],
        "airport": ["san bay"],
        "hotel": ["khach san"],
        "restaurant": ["nha hang"],
        "interview": ["phong van"],
        "shopping": ["mua sam", "cua hang"],
    }


def _find_topic_in_list(topics: list[dict], text: str) -> dict | None:
    cleaned = normalize_text(text)
    aliases = _topic_aliases()
    for topic in topics:
        topic_id = str(topic.get("id", ""))
        name = normalize_text(str(topic.get("name") or topic.get("title") or ""))
        search_terms = [topic_id, name, *aliases.get(topic_id, [])]
        if any(term and term in cleaned for term in search_terms):
            return topic
    return None


def find_topic(mode: LearningMode | str, text: str) -> dict | None:
    if mode == "vocabulary":
        return _find_topic_in_list(_build_vocab_topics_with_images(), text)
    if mode == "conversation":
        return _find_topic_in_list(CONVERSATION_TOPICS, text)
    return None


def build_mode_suggestion(mode: LearningMode) -> str:
    if mode == "vocabulary":
        vocab_topics = _build_vocab_topics_with_images()
        picked_names = [
            (str(t.get("name") or "").strip() or str(t.get("id") or "").strip())
            for t in vocab_topics[:5]
        ]
        picked_names = [name for name in picked_names if name]
        names = ", ".join(picked_names) if picked_names else "du lịch, công nghệ, công việc"
        return (
            "Mình sẵn sàng học từ vựng theo chủ đề. "
            f"Bạn muốn học chủ đề nào: {names}? "
            "Bạn chỉ cần nói ví dụ: học chủ đề du lịch."
        )
    names = ", ".join(t["title"] for t in CONVERSATION_TOPICS[:5])
    return (
        "Mình sẵn sàng luyện hội thoại theo chủ đề. "
        f"Bạn muốn luyện chủ đề nào: {names}? "
        "Bạn có thể nói: luyện hội thoại chủ đề sân bay."
    )


def build_vocab_lesson(topic: dict) -> str:
    words = topic.get("words") or []
    picked = words[:5]
    parts = [f"{item.get('word')} là {item.get('meaning')}" for item in picked]
    joined = "; ".join(parts)
    return (
        f"Bắt đầu học từ vựng chủ đề {topic.get('name')}. "
        f"Hôm nay mình học nhanh 5 từ: {joined}. "
        "Bạn muốn mình nhắc lại hoặc kiểm tra nhanh không?"
    )


def build_vocab_lesson_steps(topic: dict, max_words: int = 5, start_index: int = 0) -> list[dict]:
    all_words = topic.get("words") or []
    start = max(0, min(start_index, len(all_words)))
    words = all_words[start : start + max_words]
    steps: list[dict] = []
    if start == 0:
        steps.append(
            {
                "speech": f"Bắt đầu chủ đề {topic.get('name')}. Mình sẽ dạy chậm từng từ để bạn đọc theo.",
            }
        )
    elif words:
        steps.append(
            {
                "speech": f"Mình học tiếp chủ đề {topic.get('name')} nhé.",
            }
        )

    for offset, item in enumerate(words):
        idx = start + offset + 1
        word = str(item.get("word") or "")
        meaning = str(item.get("meaning") or "")
        steps.append(
            {
                "flashcard": {
                    "mode": "vocabulary",
                    "topic_id": topic.get("id"),
                    "topic_name": topic.get("name"),
                    "index": idx,
                    "total": len(all_words),
                    "word": word,
                    "meaning": meaning,
                    "image_url": item.get("image_url")
                    or build_flashcard_image_url(str(topic.get("id") or ""), word, meaning),
                },
                "speech": f"Từ số {idx}. {word}. Nghĩa tiếng Việt là {meaning}. Bạn hãy đọc theo mình: {word}.",
            }
        )

    next_index = start + len(words)
    if next_index >= len(all_words):
        steps.append({"speech": "Bạn làm rất tốt. Mình đã học hết từ vựng của chủ đề này rồi."})
    else:
        steps.append({"speech": "Bạn làm rất tốt. Khi sẵn sàng, hãy nói học tiếp để mình dạy nhóm từ tiếp theo."})
    return steps


def build_conversation_lesson(topic: dict) -> str:
    script = topic.get("script") or []
    sample = script[:2]
    lines = []
    for item in sample:
        en = item.get("en") or ""
        vi = item.get("vi") or ""
        lines.append(f"{en} - nghĩa là {vi}")
    joined = "; ".join(lines)
    return (
        f"Bắt đầu luyện hội thoại chủ đề {topic.get('title')}. "
        f"Mẫu câu mở đầu: {joined}. "
        "Giờ bạn thử đọc lại câu đầu tiên nhé."
    )
