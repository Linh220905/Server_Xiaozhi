"""Demo flow for practicing physical vocabulary flash cards."""

from __future__ import annotations

from typing import Any
import json
import re
import unicodedata

from app.services.llm import LLMService


DEMO_FLASHCARD_WORDS: list[dict[str, Any]] = [
    {"word": "approve", "meaning_vi": "chấp thuận", "accepted_answers": ["approve"]},
    {"word": "college", "meaning_vi": "trường cao đẳng/đại học", "accepted_answers": ["college"]},
    {"word": "cost", "meaning_vi": "chi phí", "accepted_answers": ["cost"]},
    {"word": "doctor", "meaning_vi": "bác sĩ", "accepted_answers": ["doctor"]},
    {"word": "drink", "meaning_vi": "uống/đồ uống", "accepted_answers": ["drink"]},
    {"word": "england", "meaning_vi": "nước Anh", "accepted_answers": ["england"]},
    {"word": "hard", "meaning_vi": "khó/chăm chỉ", "accepted_answers": ["hard"]},
    {"word": "history", "meaning_vi": "lịch sử", "accepted_answers": ["history"]},
    {"word": "medical", "meaning_vi": "thuộc y tế", "accepted_answers": ["medical"]},
    {"word": "plane", "meaning_vi": "máy bay", "accepted_answers": ["plane"]},
    {"word": "read", "meaning_vi": "đọc", "accepted_answers": ["read"]},
    {"word": "run", "meaning_vi": "chạy", "accepted_answers": ["run"]},
    {"word": "serve", "meaning_vi": "phục vụ", "accepted_answers": ["serve"]},
    {"word": "simple", "meaning_vi": "đơn giản", "accepted_answers": ["simple"]},
    {"word": "somtimes", "meaning_vi": "thỉnh thoảng", "accepted_answers": ["somtimes", "sometimes"]},
    {"word": "support", "meaning_vi": "hỗ trợ", "accepted_answers": ["support"]},
    {"word": "test", "meaning_vi": "bài kiểm tra/kiểm tra", "accepted_answers": ["test"]},
    {"word": "training", "meaning_vi": "sự đào tạo/luyện tập", "accepted_answers": ["training"]},
    {"word": "view", "meaning_vi": "quan điểm/tầm nhìn", "accepted_answers": ["view"]},
    {"word": "woman", "meaning_vi": "phụ nữ", "accepted_answers": ["woman"]},
    {"word": "activity", "meaning_vi": "hoạt động", "accepted_answers": ["activity"]},
    {"word": "apple", "meaning_vi": "quả táo", "accepted_answers": ["apple"]},
    {"word": "equipment", "meaning_vi": "thiết bị", "accepted_answers": ["equipment"]},
    {"word": "family", "meaning_vi": "gia đình", "accepted_answers": ["family"]},
    {"word": "friend", "meaning_vi": "bạn bè", "accepted_answers": ["friend"]},
    {"word": "happy", "meaning_vi": "vui vẻ", "accepted_answers": ["happy"]},
    {"word": "intelligent", "meaning_vi": "thông minh", "accepted_answers": ["intelligent"]},
    {"word": "school", "meaning_vi": "trường học", "accepted_answers": ["school"]},
    {"word": "sister", "meaning_vi": "chị/em gái", "accepted_answers": ["sister"]},
    {"word": "teacher", "meaning_vi": "giáo viên", "accepted_answers": ["teacher"]},
]


FLASHCARD_EVALUATION_PROMPT = """Bạn là giáo viên tiếng Anh kiểm tra học sinh đọc từ vựng trên flash card vật lí.

Bạn nhận:
- candidate_words: danh sách từ vựng có trong bộ flash card.
- student_stt: nội dung STT nghe được từ học sinh.

Nhiệm vụ:
- Xác định học sinh có đọc đúng một từ nào trong candidate_words không.
- Chấp nhận lỗi viết hoa/thường, dấu câu, và STT có thêm vài từ đệm như "là", "con đọc là".
- Nếu STT nghe gần âm nhưng chưa chắc đúng, trả is_correct=false và feedback chung, không nói học sinh sai từ cụ thể nào.
- Phản hồi ngắn, thân thiện, bằng tiếng Việt cho trẻ em.

Chỉ trả về 1 JSON object, không markdown:
{
  "is_correct": true|false,
  "heard_word": "",
  "matched_word": "",
  "confidence": 0.0,
  "feedback_vi": ""
}
"""


def flashcard_count() -> int:
    return len(DEMO_FLASHCARD_WORDS)


def get_flashcard(index: int) -> dict[str, Any] | None:
    if 0 <= index < len(DEMO_FLASHCARD_WORDS):
        return DEMO_FLASHCARD_WORDS[index]
    return None


def get_flashcard_by_word(word: str) -> dict[str, Any] | None:
    normalized_word = _normalize_text(word)
    for card in DEMO_FLASHCARD_WORDS:
        if _normalize_text(str(card.get("word") or "")) == normalized_word:
            return card
    return None


def build_flashcard_start_reply() -> str:
    return (
        "Bạn hãy lấy flash card ra để chúng mình cùng luyện tập nào. "
        "Bạn hãy chọn một thẻ bất kỳ và đọc từ tiếng Anh trên thẻ nhé."
    )


def build_next_card_prompt() -> str:
    return "Bạn chọn thẻ tiếp theo và đọc từ tiếng Anh trên thẻ nhé."


def build_finish_reply() -> str:
    return f"Mình đã luyện xong {flashcard_count()} từ hôm nay rồi. Bạn làm tốt lắm."


def _normalize_text(text: str) -> str:
    lowered = (text or "").strip().lower()
    normalized = unicodedata.normalize("NFD", lowered)
    without_marks = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
    without_marks = without_marks.replace("đ", "d")
    return re.sub(r"[^a-z0-9 ]+", " ", without_marks).strip()


def _fallback_evaluate(student_text: str, cards: list[dict[str, Any]]) -> dict[str, Any]:
    normalized = _normalize_text(student_text)
    matched_card = None
    for card in cards:
        accepted = [
            _normalize_text(str(answer))
            for answer in card.get("accepted_answers", [])
            if str(answer).strip()
        ]
        target = _normalize_text(str(card.get("word") or ""))
        if target and target not in accepted:
            accepted.append(target)
        if any(answer and re.search(rf"\b{re.escape(answer)}\b", normalized) for answer in accepted):
            matched_card = card
            break

    is_correct = matched_card is not None
    return {
        "is_correct": is_correct,
        "heard_word": student_text.strip(),
        "matched_word": str(matched_card.get("word") or "").strip() if matched_card else "",
        "confidence": 0.75 if is_correct else 0.35,
        "feedback_vi": (
            "Đúng rồi, Bạn đọc tốt lắm."
            if is_correct
            else "Mình chưa nghe rõ từ trên thẻ. Bạn thử đọc lại chậm hơn nhé."
        ),
    }


def _parse_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "đúng", "dung"}:
            return True
        if normalized in {"false", "0", "no", "sai"}:
            return False
    return default


def _parse_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


async def evaluate_flashcard_answer(
    llm: LLMService,
    *,
    student_text: str,
    cards: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    cards = cards or DEMO_FLASHCARD_WORDS
    payload = {
        "candidate_words": [
            {
                "word": card.get("word"),
                "accepted_answers": card.get("accepted_answers") or [card.get("word")],
            }
            for card in cards
        ],
        "student_stt": student_text,
    }
    try:
        data = await llm.chat_json(
            json.dumps(payload, ensure_ascii=False),
            system_prompt=FLASHCARD_EVALUATION_PROMPT,
            max_tokens=180,
            temperature=0.0,
        )
    except Exception:
        data = None

    if not isinstance(data, dict):
        return _fallback_evaluate(student_text, cards)

    fallback = _fallback_evaluate(student_text, cards)
    fallback_confidence = _parse_float(fallback.get("confidence"), 0.75 if fallback.get("is_correct") else 0.35)
    return {
        "is_correct": _parse_bool(data.get("is_correct"), bool(fallback.get("is_correct"))),
        "heard_word": str(data.get("heard_word") or fallback.get("heard_word") or student_text).strip(),
        "matched_word": str(data.get("matched_word") or fallback.get("matched_word") or "").strip().lower(),
        "confidence": _parse_float(data.get("confidence"), fallback_confidence),
        "feedback_vi": str(data.get("feedback_vi") or fallback.get("feedback_vi") or "").strip(),
    }
